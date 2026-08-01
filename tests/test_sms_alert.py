from __future__ import annotations

"""Tests for alerting.sms_alert.TwilioSender. Uses an injected fake client
factory -- no twilio import, no network."""

import sys

import pytest

from alerting.env import SmsCredentials
from alerting.sms_alert import ClientLike, MessagesLike, TwilioSender
from constants import SMS_MAX_LENGTH
from errors import AlertError

_CREDS = SmsCredentials(sid="ACxxx", auth="authtoken", from_="+15550001111")


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, *, body: str, from_: str, to: str) -> object:
        self.calls.append({"body": body, "from_": from_, "to": to})
        return object()


class _FakeClient:
    def __init__(self) -> None:
        self._messages = _FakeMessages()

    @property
    def messages(self) -> MessagesLike:
        return self._messages


def test_send_calls_messages_create() -> None:
    fake = _FakeClient()
    factory_args: list[tuple[str, str]] = []

    def _factory(sid: str, auth: str) -> ClientLike:
        factory_args.append((sid, auth))
        return fake

    TwilioSender(creds=_CREDS, client_factory=_factory).send(
        "hello there", "+15559998888"
    )

    assert factory_args == [("ACxxx", "authtoken")]
    messages = fake._messages
    assert messages.calls == [
        {"body": "hello there", "from_": "+15550001111", "to": "+15559998888"}
    ]


def test_hard_cap_is_noop_within_limit() -> None:
    fake = _FakeClient()
    body = "x" * (SMS_MAX_LENGTH - 1)
    TwilioSender(creds=_CREDS, client_factory=lambda s, a: fake).send(
        body, "+15559998888"
    )
    assert fake._messages.calls[0]["body"] == body


def test_hard_cap_truncates_over_limit() -> None:
    fake = _FakeClient()
    body = "y" * (SMS_MAX_LENGTH + 50)
    TwilioSender(creds=_CREDS, client_factory=lambda s, a: fake).send(
        body, "+15559998888"
    )
    assert len(fake._messages.calls[0]["body"]) == SMS_MAX_LENGTH


def test_import_isolation_never_touches_real_twilio() -> None:
    # Importing the module does not import twilio; sending via an injected fake
    # factory also never imports twilio.
    import alerting  # noqa: F401
    import alerting.sms_alert  # noqa: F401

    before = "twilio" in sys.modules
    fake = _FakeClient()
    TwilioSender(creds=_CREDS, client_factory=lambda s, a: fake).send(
        "hi", "+15559998888"
    )
    after = "twilio" in sys.modules
    # Whatever the baseline, the injected-factory send introduced no twilio import.
    assert after == before


def test_missing_env_raises_alert_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWILIO_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH", raising=False)
    monkeypatch.delenv("TWILIO_FROM", raising=False)
    # No injected creds -> resolve_sms_credentials raises AlertError; factory
    # never reached.
    with pytest.raises(AlertError) as excinfo:
        TwilioSender(client_factory=lambda s, a: _FakeClient()).send(
            "hi", "+15559998888"
        )
    assert "TWILIO_SID" in str(excinfo.value)


def test_twilio_like_error_wrapped() -> None:
    class _Boom(Exception):
        pass

    def _factory(sid: str, auth: str) -> ClientLike:
        raise _Boom("twilio rejected the request")

    with pytest.raises(AlertError) as excinfo:
        TwilioSender(creds=_CREDS, client_factory=_factory).send(
            "hi", "+15559998888"
        )
    assert "sms send failed" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, _Boom)


def test_os_error_wrapped() -> None:
    def _factory(sid: str, auth: str) -> ClientLike:
        raise OSError("connection reset")

    with pytest.raises(AlertError) as excinfo:
        TwilioSender(creds=_CREDS, client_factory=_factory).send(
            "hi", "+15559998888"
        )
    assert isinstance(excinfo.value.__cause__, OSError)


def test_module_not_found_maps_to_not_installed() -> None:
    def _factory(sid: str, auth: str) -> ClientLike:
        raise ModuleNotFoundError("No module named 'twilio'")

    with pytest.raises(AlertError) as excinfo:
        TwilioSender(creds=_CREDS, client_factory=_factory).send(
            "hi", "+15559998888"
        )
    assert str(excinfo.value) == "twilio not installed"
    assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


def test_preexisting_alert_error_reraised() -> None:
    original = AlertError("missing required environment variable(s): TWILIO_SID")

    def _factory(sid: str, auth: str) -> ClientLike:
        raise original

    with pytest.raises(AlertError) as excinfo:
        TwilioSender(creds=_CREDS, client_factory=_factory).send(
            "hi", "+15559998888"
        )
    assert excinfo.value is original
