from __future__ import annotations

"""Tests for dispatch_bridge.py -- the repository_dispatch bridge.

No network: a fake ``requests.Session`` returns canned responses (or raises
``requests.RequestException``). The injected ``sleep`` is a no-op recorder so
backoff never waits real time and retries are asserted by attempt count. The PAT
is set/cleared via ``monkeypatch.setenv``/``delenv`` at CALL time.
"""

import json
from datetime import datetime, timezone
from typing import Callable

import pytest
import requests

import constants
from dispatch_bridge import (
    RequestsDispatchBridge,
    build_bridge_payload,
)
from errors import DispatchBridgeAuthError, DispatchBridgeError
from models import Confidence, DetectedEvent, EventType, Priority

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
PAT_ENV = constants.ENV_DISPATCH_GITHUB_PAT


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    """Records POST calls and returns/raises per a queued script.

    ``script`` is a list of ints (status codes) or exceptions; each POST pops the
    next. Records (url, headers, data) per call for assertions.
    """

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        data: object = None,
        headers: object = None,
        timeout: object = None,
    ) -> FakeResponse:
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "timeout": timeout}
        )
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, int)
        return FakeResponse(item)


def _sleep_recorder() -> tuple[Callable[[float], None], list[float]]:
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)

    return _sleep, slept


def _make_event() -> DetectedEvent:
    return DetectedEvent(
        event_type=EventType.FILING_13F,
        entity_key="atreides",
        source="SEC EDGAR",
        title="Atreides 13F-HR filed",
        url="https://sec.gov/x-index.htm",
        identifier="0001777813-26-000001",
        published=datetime(2026, 7, 21, 20, 0, tzinfo=timezone.utc),
        priority=Priority.HIGH,
        confidence=Confidence.HIGH,
        payload={"filing_type": "13F-HR"},
    )


# --------------------------------------------------------------------------- #
# pat_present
# --------------------------------------------------------------------------- #


def test_pat_present_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    bridge = RequestsDispatchBridge(session=FakeSession([]), sleep=lambda _s: None)
    assert bridge.pat_present() is True


def test_pat_present_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAT_ENV, raising=False)
    bridge = RequestsDispatchBridge(session=FakeSession([]), sleep=lambda _s: None)
    assert bridge.pat_present() is False


def test_pat_present_false_when_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PAT_ENV, "   \t ")
    bridge = RequestsDispatchBridge(session=FakeSession([]), sleep=lambda _s: None)
    assert bridge.pat_present() is False


# --------------------------------------------------------------------------- #
# fire -- success
# --------------------------------------------------------------------------- #


def test_fire_success_204(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    session = FakeSession([204])
    sleep, slept = _sleep_recorder()
    bridge = RequestsDispatchBridge(session=session, sleep=sleep)
    bridge.fire("owner/name", "fomo_monitor_event", {"schema_version": "1"})
    assert len(session.calls) == 1
    assert slept == []  # no retry, no sleep
    call = session.calls[0]
    assert call["url"] == constants.GITHUB_DISPATCHES_URL.format(owner_repo="owner/name")
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer ghp_token"
    assert headers["X-GitHub-Api-Version"] == constants.GITHUB_API_VERSION
    body = json.loads(str(call["data"]))
    assert body["event_type"] == "fomo_monitor_event"
    assert body["client_payload"] == {"schema_version": "1"}


# --------------------------------------------------------------------------- #
# fire -- retry then success
# --------------------------------------------------------------------------- #


def test_fire_one_retry_on_5xx_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    session = FakeSession([502, 204])
    sleep, slept = _sleep_recorder()
    bridge = RequestsDispatchBridge(session=session, sleep=sleep)
    bridge.fire("owner/name", "evt", {})
    assert len(session.calls) == 2  # first 502, retried once -> 204
    assert slept == [constants.DISPATCH_RETRY_BACKOFF_SECONDS * 1]


def test_fire_retry_on_timeout_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    session = FakeSession([requests.Timeout("timed out"), 204])
    sleep, slept = _sleep_recorder()
    bridge = RequestsDispatchBridge(session=session, sleep=sleep)
    bridge.fire("owner/name", "evt", {})
    assert len(session.calls) == 2
    assert slept == [constants.DISPATCH_RETRY_BACKOFF_SECONDS * 1]


def test_fire_exhausted_retry_raises_bridge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    session = FakeSession([503, 503])  # 1 + DISPATCH_RETRY_ATTEMPTS attempts
    sleep, slept = _sleep_recorder()
    bridge = RequestsDispatchBridge(session=session, sleep=sleep)
    with pytest.raises(DispatchBridgeError) as ei:
        bridge.fire("owner/name", "evt", {})
    assert not isinstance(ei.value, DispatchBridgeAuthError)
    assert len(session.calls) == 1 + constants.DISPATCH_RETRY_ATTEMPTS
    # PAT never leaks into the message.
    assert "ghp_token" not in str(ei.value)


# --------------------------------------------------------------------------- #
# fire -- permanent failures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [401, 403])
def test_fire_auth_status_raises_auth_error_no_retry(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    session = FakeSession([status, 204])  # would succeed on retry IF it retried
    sleep, slept = _sleep_recorder()
    bridge = RequestsDispatchBridge(session=session, sleep=sleep)
    with pytest.raises(DispatchBridgeAuthError) as ei:
        bridge.fire("owner/name", "evt", {})
    assert len(session.calls) == 1  # 401/403 is permanent -> no retry
    assert slept == []
    assert "ghp_token" not in str(ei.value)


@pytest.mark.parametrize("status", [404, 422, 400])
def test_fire_other_4xx_raises_bridge_error_no_retry(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    session = FakeSession([status, 204])
    sleep, slept = _sleep_recorder()
    bridge = RequestsDispatchBridge(session=session, sleep=sleep)
    with pytest.raises(DispatchBridgeError) as ei:
        bridge.fire("owner/name", "evt", {})
    assert not isinstance(ei.value, DispatchBridgeAuthError)
    assert len(session.calls) == 1
    assert slept == []


def test_fire_pat_absent_raises_auth_error_single_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PAT_ENV, raising=False)
    session = FakeSession([204])
    bridge = RequestsDispatchBridge(session=session, sleep=lambda _s: None)
    with pytest.raises(DispatchBridgeAuthError):
        bridge.fire("owner/name", "evt", {})
    assert session.calls == []  # never POSTed without a PAT


def test_fire_error_message_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PAT_ENV, "ghp_token")
    long_msg = "x" * (constants.DISPATCH_ERROR_MAX_CHARS + 500)
    session = FakeSession([requests.ConnectionError(long_msg)] * (
        1 + constants.DISPATCH_RETRY_ATTEMPTS
    ))
    bridge = RequestsDispatchBridge(session=session, sleep=lambda _s: None)
    with pytest.raises(DispatchBridgeError) as ei:
        bridge.fire("owner/name", "evt", {})
    assert len(str(ei.value)) <= constants.DISPATCH_ERROR_MAX_CHARS + 200


# --------------------------------------------------------------------------- #
# build_bridge_payload -- nested envelope
# --------------------------------------------------------------------------- #


def test_build_payload_nested_shape() -> None:
    event = _make_event()
    payload = build_bridge_payload(event, "edgar", NOW)
    assert payload["schema_version"] == constants.DISPATCH_PAYLOAD_SCHEMA_VERSION
    inner = payload["event"]
    assert isinstance(inner, dict)
    assert inner["event_type"] == "filing_13f"
    assert inner["entity_key"] == "atreides"
    assert inner["source"] == "SEC EDGAR"
    assert inner["identifier"] == "0001777813-26-000001"
    assert inner["monitor"] == "edgar"
    assert inner["priority"] == "high"
    assert inner["confidence"] == "high"
    assert inner["published"] == event.published.isoformat()  # type: ignore[union-attr]
    assert inner["detected_at"] == NOW.isoformat()
    assert inner["local_alert_error"] == ""
    # JSON-serializable end-to-end.
    json.dumps(payload)


def test_build_payload_published_none_becomes_empty() -> None:
    event = _make_event()
    event = DetectedEvent(
        event_type=event.event_type,
        entity_key=event.entity_key,
        source=event.source,
        title=event.title,
        url=event.url,
        identifier=event.identifier,
        published=None,
        priority=event.priority,
        confidence=event.confidence,
        payload={},
    )
    payload = build_bridge_payload(event, "edgar", NOW)
    inner = payload["event"]
    assert isinstance(inner, dict)
    assert inner["published"] == ""


def test_build_payload_local_alert_error_carried() -> None:
    event = _make_event()
    event = DetectedEvent(
        event_type=event.event_type,
        entity_key=event.entity_key,
        source=event.source,
        title=event.title,
        url=event.url,
        identifier=event.identifier,
        published=event.published,
        priority=event.priority,
        confidence=event.confidence,
        payload={"local_alert_error": "sms failed"},
    )
    payload = build_bridge_payload(event, "cnbc", NOW)
    inner = payload["event"]
    assert isinstance(inner, dict)
    assert inner["local_alert_error"] == "sms failed"
