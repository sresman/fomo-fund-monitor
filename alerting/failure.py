from __future__ import annotations

"""Sanitized rendering of a send failure's underlying cause.

The alerting senders catch a transport exception and re-raise it as an
``AlertError``. Historically the wrapper message was a bare constant
("email send failed"), and the cause survived only as ``__cause__`` -- which the
orchestrator never read. An operator therefore could not tell an SMTP auth
rejection from a DNS failure from a refused recipient.

``describe_failure`` renders ``"ExceptionClassName: message"`` so the class name
(the single most diagnostic token) always survives, while guaranteeing no
credential VALUE is interpolated: every secret the caller passes is substituted
with ``REDACTED_PLACEHOLDER`` BEFORE the length cap is applied, so a truncation
can never leave a partial secret in the output.

Callers pass every value that could plausibly be echoed back by the remote
service -- credentials AND the recipient address/number, which are themselves
stored as deployment secrets.
"""

from typing import Iterable

from constants import ALERT_FAILURE_DETAIL_MAX_CHARS, REDACTED_PLACEHOLDER

_TRUNCATION_ELLIPSIS = "…"


def describe_failure(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """Render ``exc`` as a sanitized, length-capped diagnostic string.

    The exception class name is ALWAYS present and is never truncated away. The
    message body has every non-empty entry of ``secrets`` replaced with
    ``REDACTED_PLACEHOLDER`` first, then is capped at
    ``ALERT_FAILURE_DETAIL_MAX_CHARS``. An exception with an empty message
    renders as just its class name.
    """
    label = type(exc).__name__
    detail = str(exc).strip()
    if not detail:
        return label

    # Redact BEFORE capping: capping first could slice a secret in half and
    # leave the surviving fragment in the message.
    for secret in secrets:
        cleaned = secret.strip()
        if cleaned:
            detail = detail.replace(cleaned, REDACTED_PLACEHOLDER)

    if len(detail) > ALERT_FAILURE_DETAIL_MAX_CHARS:
        room = ALERT_FAILURE_DETAIL_MAX_CHARS - len(_TRUNCATION_ELLIPSIS)
        detail = detail[:room].rstrip() + _TRUNCATION_ELLIPSIS
    return f"{label}: {detail}"
