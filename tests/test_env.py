from __future__ import annotations

"""Tests for alerting.env -- credential/recipient resolution."""

import pytest

from alerting.env import (
    EmailCredentials,
    SmsCredentials,
    resolve_email_credentials,
    resolve_recipient,
    resolve_sms_credentials,
)
from errors import AlertError


def test_resolve_email_credentials_success_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_USER", "  me@example.com  ")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "\tsecretpw\n")
    creds = resolve_email_credentials()
    assert creds == EmailCredentials(user="me@example.com", app_password="secretpw")


def test_resolve_sms_credentials_success_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWILIO_SID", " ACxxx ")
    monkeypatch.setenv("TWILIO_AUTH", " authtoken ")
    monkeypatch.setenv("TWILIO_FROM", " +15550001111 ")
    creds = resolve_sms_credentials()
    assert creds == SmsCredentials(
        sid="ACxxx", auth="authtoken", from_="+15550001111"
    )


def test_resolve_recipient_success_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALERT_EMAIL", "  to@example.com  ")
    assert resolve_recipient("ALERT_EMAIL") == "to@example.com"


def test_whitespace_only_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_USER", "   ")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    with pytest.raises(AlertError) as excinfo:
        resolve_email_credentials()
    assert "GMAIL_USER" in str(excinfo.value)
    assert "GMAIL_APP_PASSWORD" not in str(excinfo.value)


def test_missing_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALERT_PHONE", raising=False)
    with pytest.raises(AlertError) as excinfo:
        resolve_recipient("ALERT_PHONE")
    assert "ALERT_PHONE" in str(excinfo.value)


def test_all_missing_collected_in_requested_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All three Twilio vars absent -> single error listing ALL in requested order.
    monkeypatch.delenv("TWILIO_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH", raising=False)
    monkeypatch.delenv("TWILIO_FROM", raising=False)
    with pytest.raises(AlertError) as excinfo:
        resolve_sms_credentials()
    msg = str(excinfo.value)
    # Requested order is (SID, AUTH, FROM).
    assert msg.index("TWILIO_SID") < msg.index("TWILIO_AUTH") < msg.index(
        "TWILIO_FROM"
    )


def test_partial_missing_lists_only_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWILIO_SID", "ACxxx")
    monkeypatch.delenv("TWILIO_AUTH", raising=False)
    monkeypatch.setenv("TWILIO_FROM", "+15550001111")
    with pytest.raises(AlertError) as excinfo:
        resolve_sms_credentials()
    msg = str(excinfo.value)
    assert "TWILIO_AUTH" in msg
    assert "TWILIO_SID" not in msg
    assert "TWILIO_FROM" not in msg
