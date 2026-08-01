from __future__ import annotations

"""``repository_dispatch`` bridge to the celeb-pm repo (Prompt 6).

The orchestrator fires a GitHub ``repository_dispatch`` event for each COMMITTED
detected event so the downstream ``celeb-pm`` repo can react (via its own
``on: repository_dispatch`` workflow). This module is intentionally decoupled
from the alerting layer -- a bridge failure NEVER affects alerting, mark-seen,
``record_run``, or the run's exit code.

Design mirrors the other concrete HTTP clients (``EdgarHttpClient`` /
``RequestsFeedClient``): the ``requests.Session`` and ``sleep`` are injectable so
tests touch no network and never wait real time; the PAT is resolved from
``os.environ`` AT CALL TIME (never at import) and ``.strip()``-ed so a
whitespace-only value counts as absent.

Retry model: exactly ONE retry after the first attempt (GitHub 5xx / 502s
happen), with a backoff slept via the injected ``sleep``. Retries fire on
transport faults (``requests.RequestException`` incl. ``Timeout`` /
``ConnectionError``) and 5xx responses. A permanent 4xx (or an exhausted retry)
raises ``DispatchBridgeError``; 401/403 raise ``DispatchBridgeAuthError`` so the
orchestrator can short-circuit all remaining fires for the run. Error messages
are length-capped and NEVER contain the PAT.
"""

import json
import os
import time
from datetime import datetime
from typing import Callable, Protocol, cast, runtime_checkable

import requests

import constants
from errors import DispatchBridgeAuthError, DispatchBridgeError
from models import DetectedEvent


class ResponseLike(Protocol):
    """The single response attribute the bridge reads."""

    @property
    def status_code(self) -> int: ...


class SessionLike(Protocol):
    """The subset of ``requests.Session`` the bridge uses. A Protocol seam so
    tests inject a fake session (no network) exactly like the other concrete
    HTTP clients do with their getter seams."""

    def post(
        self,
        url: str,
        *,
        data: object = ...,
        headers: object = ...,
        timeout: object = ...,
    ) -> ResponseLike: ...


@runtime_checkable
class DispatchBridge(Protocol):
    """Bridge seam the orchestrator depends on (DI-friendly, network-free in
    tests). ``fire`` performs one ``repository_dispatch`` POST; ``pat_present``
    reports whether a usable PAT exists in the environment RIGHT NOW (call
    time), so the orchestrator can skip firing entirely with a single log line
    when the PAT is absent."""

    def fire(
        self, repo: str, event_type: str, payload: dict[str, object]
    ) -> None: ...

    def pat_present(self) -> bool: ...


def _cap(message: str) -> str:
    """Length-cap an error/detail string. NEVER carries the PAT (callers only
    ever pass status/exception text, never a header value)."""
    limit = constants.DISPATCH_ERROR_MAX_CHARS
    if len(message) <= limit:
        return message
    return message[: limit - 1] + constants.SMS_TRUNCATION_ELLIPSIS


class RequestsDispatchBridge:
    """Concrete ``repository_dispatch`` bridge over ``requests``.

    Injectable ``session`` + ``sleep`` (tests inject fakes; production defaults
    to a real session and ``time.sleep``). Total attempts =
    ``1 + DISPATCH_RETRY_ATTEMPTS``. The PAT is read from
    ``ENV_DISPATCH_GITHUB_PAT`` inside each call and stripped; a whitespace-only
    value is treated as absent (``pat_present`` -> False; ``fire`` raises
    ``DispatchBridgeAuthError`` so a misconfigured deploy is loud but isolated).
    """

    def __init__(
        self,
        session: SessionLike | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # A real ``requests.Session`` satisfies SessionLike at runtime (we call
        # ``.post`` with keywords); its typeshed signature is broader than our
        # narrow seam, so cast the default. Injected fakes match structurally.
        self._session: SessionLike = (
            session if session is not None else cast(SessionLike, requests.Session())
        )
        self._sleep = sleep

    def _resolve_pat(self) -> str:
        return os.environ.get(constants.ENV_DISPATCH_GITHUB_PAT, "").strip()

    def pat_present(self) -> bool:
        return self._resolve_pat() != ""

    def fire(
        self, repo: str, event_type: str, payload: dict[str, object]
    ) -> None:
        pat = self._resolve_pat()
        if pat == "":
            # No usable PAT: a loud, isolated auth signal (orchestrator gates on
            # pat_present() so this normally never runs, but fire() is defensive).
            raise DispatchBridgeAuthError(
                "DISPATCH_GITHUB_PAT not set; cannot POST repository_dispatch"
            )

        url = constants.GITHUB_DISPATCHES_URL.format(owner_repo=repo)
        headers = {
            "Accept": constants.GITHUB_API_ACCEPT,
            "X-GitHub-Api-Version": constants.GITHUB_API_VERSION,
            "Authorization": f"Bearer {pat}",
            "User-Agent": constants.USER_AGENT,
        }
        body = json.dumps(
            {"event_type": event_type, "client_payload": payload}
        )

        total_attempts = 1 + constants.DISPATCH_RETRY_ATTEMPTS
        last_detail = ""
        for attempt in range(total_attempts):
            if attempt > 0:
                self._sleep(constants.DISPATCH_RETRY_BACKOFF_SECONDS * attempt)
            try:
                resp = self._session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=constants.DISPATCH_HTTP_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                # Transport fault (incl. Timeout / ConnectionError): retryable.
                last_detail = _cap(f"transport error: {exc}")
                continue

            status = resp.status_code
            if 200 <= status < 300:
                return  # success (GitHub returns 204 No Content)
            if status in (401, 403):
                # Bad / expired / insufficient-scope PAT: permanent, distinct so
                # the orchestrator short-circuits remaining fires this run.
                raise DispatchBridgeAuthError(
                    _cap(
                        f"repository_dispatch to {repo} rejected: HTTP {status} "
                        f"(check PAT validity/scope)"
                    )
                )
            if 500 <= status < 600:
                # Server-side transient: retryable.
                last_detail = _cap(f"server error: HTTP {status}")
                continue
            # Any other 4xx (404 wrong repo, 422 bad event_type, ...): permanent.
            raise DispatchBridgeError(
                _cap(f"repository_dispatch to {repo} failed: HTTP {status}")
            )

        # Exhausted all attempts on a retryable fault.
        raise DispatchBridgeError(
            _cap(f"repository_dispatch to {repo} failed after "
                 f"{total_attempts} attempt(s): {last_detail}")
        )


def build_bridge_payload(
    event: DetectedEvent, monitor_name: str, now: datetime
) -> dict[str, object]:
    """Build the nested ``client_payload`` for a ``repository_dispatch``.

    Schema (versioned envelope so the receiver can branch on shape):

        {
          "schema_version": "1",
          "event": {
            "event_type", "entity_key", "source", "title", "url",
            "identifier", "published" (ISO-8601 or ""), "priority",
            "confidence", "monitor", "detected_at", "local_alert_error"
          }
        }

    All values are JSON-safe scalars. ``published`` is the event's tz-aware
    timestamp ISO-formatted, or ``""`` when unknown. ``detected_at`` is the run
    ``now``. ``local_alert_error`` records whether the local alert dispatch
    reported a problem for this event (empty string = clean); the receiver may
    surface it but it never blocks the bridge.
    """
    published_iso = event.published.isoformat() if event.published is not None else ""
    local_alert_error = event.payload.get("local_alert_error", "")
    return {
        "schema_version": constants.DISPATCH_PAYLOAD_SCHEMA_VERSION,
        "event": {
            "event_type": event.event_type.value,
            "entity_key": event.entity_key,
            "source": event.source,
            "title": event.title,
            "url": event.url,
            "identifier": event.identifier,
            "published": published_iso,
            "priority": event.priority.value,
            "confidence": event.confidence.value,
            "monitor": monitor_name,
            "detected_at": now.isoformat(),
            "local_alert_error": local_alert_error,
        },
    }
