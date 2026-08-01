from __future__ import annotations

"""Tests for alerting.dispatch.Dispatcher -- fail-soft, per-channel, per-event.
Uses typed fake senders (implementing the EmailSender/SmsSender Protocols) and
monkeypatched recipient env. No real senders, no network."""

import dataclasses
from datetime import datetime, timezone

import pytest

from alerting.dispatch import Dispatcher, DispatchResult
from config import AppConfig, load_config
from errors import AlertError
from models import AlertChannel, Confidence, DetectedEvent, EventType, Priority
from tests.conftest import SAMPLE_CONFIG


# --------------------------------------------------------------------------- #
# Typed fakes
# --------------------------------------------------------------------------- #


class FakeEmailSender:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def send(self, subject: str, body: str, to_addr: str) -> None:
        self.calls.append((subject, body, to_addr))
        if self._fail:
            raise AlertError("email send failed")


class FakeSmsSender:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    def send(self, body: str, to_phone: str) -> None:
        self.calls.append((body, to_phone))
        if self._fail:
            raise AlertError("sms send failed")


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def make_event(
    event_type: EventType = EventType.FILING_13F,
    *,
    title: str = "T",
    url: str = "https://ex.example/i",
    payload: dict[str, str] | None = None,
) -> DetectedEvent:
    return DetectedEvent(
        event_type=event_type,
        entity_key="atreides",
        source="SEC EDGAR",
        title=title,
        url=url,
        identifier="id-1",
        published=datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc),
        priority=Priority.HIGH,
        confidence=Confidence.HIGH,
        payload=payload if payload is not None else {},
    )


@pytest.fixture
def config() -> AppConfig:
    return load_config(SAMPLE_CONFIG)


@pytest.fixture
def env_recipients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_EMAIL", "to@example.com")
    monkeypatch.setenv("ALERT_PHONE", "+15559998888")


def _config_with_routing(
    config: AppConfig, channels: tuple[AlertChannel, ...]
) -> AppConfig:
    """Return a copy whose alert_routing maps EVERY event type to ``channels``."""
    routing = {et: channels for et in EventType}
    return dataclasses.replace(config, alert_routing=routing)


# --------------------------------------------------------------------------- #
# Deterministic order
# --------------------------------------------------------------------------- #


def test_deterministic_channel_order(
    config: AppConfig, env_recipients: None
) -> None:
    # Config tuple order is (SMS, EMAIL); dispatch must still be (EMAIL, SMS).
    cfg = _config_with_routing(config, (AlertChannel.SMS, AlertChannel.EMAIL))
    email, sms = FakeEmailSender(), FakeSmsSender()
    result = Dispatcher(email, sms).dispatch_event(make_event(), cfg)
    assert result.channels_attempted == (AlertChannel.EMAIL, AlertChannel.SMS)
    assert result.channels_sent == (AlertChannel.EMAIL, AlertChannel.SMS)
    assert result.errors == {}
    assert result.event_error is None
    assert result.skipped is False


# --------------------------------------------------------------------------- #
# Fail-soft matrix
# --------------------------------------------------------------------------- #


def test_email_succeeds_sms_fails(
    config: AppConfig, env_recipients: None
) -> None:
    cfg = _config_with_routing(config, (AlertChannel.EMAIL, AlertChannel.SMS))
    email, sms = FakeEmailSender(), FakeSmsSender(fail=True)
    result = Dispatcher(email, sms).dispatch_event(make_event(), cfg)
    assert AlertChannel.EMAIL in result.channels_sent
    assert AlertChannel.SMS in result.channels_attempted
    assert AlertChannel.SMS not in result.channels_sent
    assert result.errors == {AlertChannel.SMS: "sms send failed"}


def test_sms_succeeds_email_fails(
    config: AppConfig, env_recipients: None
) -> None:
    cfg = _config_with_routing(config, (AlertChannel.EMAIL, AlertChannel.SMS))
    email, sms = FakeEmailSender(fail=True), FakeSmsSender()
    result = Dispatcher(email, sms).dispatch_event(make_event(), cfg)
    assert AlertChannel.SMS in result.channels_sent
    assert AlertChannel.EMAIL in result.channels_attempted
    assert AlertChannel.EMAIL not in result.channels_sent
    assert result.errors == {AlertChannel.EMAIL: "email send failed"}
    # email failure did NOT skip sms
    assert sms.calls


def test_missing_email_recipient_does_not_abort_sms(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALERT_EMAIL", raising=False)
    monkeypatch.setenv("ALERT_PHONE", "+15559998888")
    cfg = _config_with_routing(config, (AlertChannel.EMAIL, AlertChannel.SMS))
    email, sms = FakeEmailSender(), FakeSmsSender()
    result = Dispatcher(email, sms).dispatch_event(make_event(), cfg)
    assert AlertChannel.EMAIL in result.errors
    assert "ALERT_EMAIL" in result.errors[AlertChannel.EMAIL]
    assert AlertChannel.SMS in result.channels_sent
    assert email.calls == []  # sender never invoked (recipient resolution failed)


def test_disabled_sender_silently_dropped(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Email routed but sender=None -> absent, not an error; SMS fires.
    monkeypatch.setenv("ALERT_PHONE", "+15559998888")
    monkeypatch.delenv("ALERT_EMAIL", raising=False)  # must not be needed
    cfg = _config_with_routing(config, (AlertChannel.EMAIL, AlertChannel.SMS))
    sms = FakeSmsSender()
    result = Dispatcher(None, sms).dispatch_event(make_event(), cfg)
    assert AlertChannel.EMAIL not in result.channels_attempted
    assert AlertChannel.EMAIL not in result.errors
    assert result.channels_sent == (AlertChannel.SMS,)


def test_email_only_dispatcher_requires_no_sms_env(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SMS routed but sms_sender=None -> no TWILIO_*/ALERT_PHONE ever read.
    monkeypatch.setenv("ALERT_EMAIL", "to@example.com")
    monkeypatch.delenv("ALERT_PHONE", raising=False)
    cfg = _config_with_routing(config, (AlertChannel.EMAIL, AlertChannel.SMS))
    email = FakeEmailSender()
    result = Dispatcher(email, None).dispatch_event(make_event(), cfg)
    assert result.channels_sent == (AlertChannel.EMAIL,)
    assert result.errors == {}
    assert AlertChannel.SMS not in result.channels_attempted


# --------------------------------------------------------------------------- #
# event_error / dry-run
# --------------------------------------------------------------------------- #


def test_event_error_on_formatting_failure(
    config: AppConfig, env_recipients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(event: DetectedEvent, cfg: AppConfig) -> object:
        raise ValueError("template blew up")

    monkeypatch.setattr("alerting.dispatch.build_alert", _boom)
    email, sms = FakeEmailSender(), FakeSmsSender()
    result = Dispatcher(email, sms).dispatch_event(make_event(), config)
    assert result.event_error == "template blew up"
    assert result.channels_attempted == ()
    assert result.channels_sent == ()
    assert result.errors == {}
    assert result.skipped is False
    assert email.calls == []
    assert sms.calls == []


def test_dry_run_attempts_nothing(config: AppConfig) -> None:
    # No env set at all; dry-run must not read any.
    email, sms = FakeEmailSender(), FakeSmsSender()
    result = Dispatcher(email, sms, dry_run=True).dispatch_event(
        make_event(), config
    )
    assert result.skipped is True
    assert result.channels_attempted == ()
    assert result.channels_sent == ()
    assert result.errors == {}
    assert result.event_error is None
    assert email.calls == []
    assert sms.calls == []


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #


def test_dispatch_events_continues_after_event_error(
    config: AppConfig, env_recipients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alerting.formatting import build_alert as real_build

    calls: list[int] = []

    def _sometimes_boom(event: DetectedEvent, cfg: AppConfig) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("first event bad")
        return real_build(event, cfg)

    monkeypatch.setattr("alerting.dispatch.build_alert", _sometimes_boom)
    email, sms = FakeEmailSender(), FakeSmsSender()
    events = [make_event(), make_event(EventType.GOOGLE_NEWS)]
    results = Dispatcher(email, sms).dispatch_events(events, config)
    assert len(results) == 2
    assert results[0].event_error == "first event bad"
    assert results[1].event_error is None
    assert AlertChannel.EMAIL in results[1].channels_sent


def test_dispatch_events_preserves_order_and_length(
    config: AppConfig, env_recipients: None
) -> None:
    email, sms = FakeEmailSender(), FakeSmsSender()
    events = [
        make_event(EventType.FILING_13F),
        make_event(EventType.GOOGLE_NEWS),
        make_event(EventType.WEBSITE_DIFF),
    ]
    results = Dispatcher(email, sms).dispatch_events(events, config)
    assert [r.event.event_type for r in results] == [
        EventType.FILING_13F,
        EventType.GOOGLE_NEWS,
        EventType.WEBSITE_DIFF,
    ]


def test_result_is_frozen() -> None:
    result = DispatchResult(
        event=make_event(),
        channels_attempted=(),
        channels_sent=(),
        errors={},
        event_error=None,
        skipped=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.skipped = False  # type: ignore[misc]  # asserting frozen
