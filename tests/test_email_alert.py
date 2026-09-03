from __future__ import annotations

"""Tests for alerting.email_alert.GmailSender. Mocks smtplib.SMTP_SSL at the
client-method level -- no sockets."""

import smtplib
from email.message import EmailMessage
from types import TracebackType

import pytest

from alerting import email_alert
from alerting.email_alert import GmailSender
from alerting.env import EmailCredentials
from constants import (
    GMAIL_SMTP_HOST,
    GMAIL_SMTP_PORT,
    REDACTED_PLACEHOLDER,
    SMTP_TIMEOUT_SECONDS,
)
from errors import AlertError

_CREDS = EmailCredentials(user="me@example.com", app_password="secretpw")


class _FakeSMTP:
    """Records login/send_message calls; usable as a context manager."""

    def __init__(self) -> None:
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def test_send_calls_login_and_send_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSMTP()
    captured: dict[str, object] = {}

    def _factory(host: str, port: int, timeout: float) -> _FakeSMTP:
        captured["host"] = host
        captured["port"] = port
        captured["timeout"] = timeout
        return fake

    monkeypatch.setattr(smtplib, "SMTP_SSL", _factory)
    GmailSender(creds=_CREDS).send("Subject — é", "Body with em—dash", "to@example.com")

    assert captured == {
        "host": GMAIL_SMTP_HOST,
        "port": GMAIL_SMTP_PORT,
        "timeout": SMTP_TIMEOUT_SECONDS,
    }
    assert fake.login_args == ("me@example.com", "secretpw")
    assert len(fake.sent) == 1
    msg = fake.sent[0]
    assert msg["Subject"] == "Subject — é"
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "to@example.com"
    # UTF-8 body round-trips the em dash.
    assert "em—dash" in msg.get_content()


def test_header_injection_wrapped_as_alert_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: _FakeSMTP())
    with pytest.raises(AlertError) as excinfo:
        GmailSender(creds=_CREDS).send(
            "Subject\r\nBcc: evil@example.com", "body", "to@example.com"
        )
    # No credentials leaked into the message.
    assert "secretpw" not in str(excinfo.value)


def test_smtp_exception_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingSMTP(_FakeSMTP):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPException("auth failed")

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: _RaisingSMTP())
    with pytest.raises(AlertError) as excinfo:
        GmailSender(creds=_CREDS).send("s", "b", "to@example.com")
    assert "secretpw" not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, smtplib.SMTPException)


def test_os_error_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def _factory(*a: object, **k: object) -> _FakeSMTP:
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP_SSL", _factory)
    with pytest.raises(AlertError) as excinfo:
        GmailSender(creds=_CREDS).send("s", "b", "to@example.com")
    assert isinstance(excinfo.value.__cause__, OSError)


def test_failure_message_names_the_underlying_exception_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason must survive the AlertError wrap. Previously the wrapper was
    the bare string "email send failed" and the cause lived only on __cause__,
    which the orchestrator never read -- so a 20-day auth outage was
    indistinguishable from a transient DNS blip."""
    def _factory(*a: object, **k: object) -> _FakeSMTP:
        raise smtplib.SMTPAuthenticationError(
            535, b"5.7.8 Username and Password not accepted"
        )

    monkeypatch.setattr(smtplib, "SMTP_SSL", _factory)
    with pytest.raises(AlertError) as excinfo:
        GmailSender(creds=_CREDS).send("s", "b", "to@example.com")
    message = str(excinfo.value)
    assert message.startswith("email send failed: ")
    assert "SMTPAuthenticationError" in message
    assert "535" in message
    assert isinstance(excinfo.value.__cause__, smtplib.SMTPAuthenticationError)


@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("secretpw", "app password"),
        ("me@example.com", "gmail user"),
        ("to@example.com", "recipient"),
    ],
)
def test_failure_message_never_leaks_a_credential(
    monkeypatch: pytest.MonkeyPatch, secret: str, label: str
) -> None:
    """Every value SMTP could echo back is redacted -- credentials AND the
    recipient address, which is itself a deployment secret."""
    def _factory(*a: object, **k: object) -> _FakeSMTP:
        raise smtplib.SMTPDataError(550, f"rejected: {secret}".encode())

    monkeypatch.setattr(smtplib, "SMTP_SSL", _factory)
    with pytest.raises(AlertError) as excinfo:
        GmailSender(creds=_CREDS).send("s", "b", "to@example.com")
    message = str(excinfo.value)
    assert secret not in message, f"{label} leaked into the error message"
    assert REDACTED_PLACEHOLDER in message
    assert "SMTPDataError" in message  # ... but the class still survives


def test_missing_env_raises_alert_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # No injected creds; env missing -> resolve_email_credentials raises AlertError.
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    # SMTP_SSL should never be reached, but stub it to be safe.
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: _FakeSMTP())
    with pytest.raises(AlertError) as excinfo:
        GmailSender().send("s", "b", "to@example.com")
    assert "GMAIL_USER" in str(excinfo.value)


def test_preexisting_alert_error_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pre-existing AlertError (from resolve_*) is re-raised unchanged (no
    # double-wrap into "email send failed").
    original = AlertError("missing required environment variable(s): GMAIL_USER")

    def _boom() -> EmailCredentials:
        raise original

    monkeypatch.setattr(email_alert, "resolve_email_credentials", _boom)
    with pytest.raises(AlertError) as excinfo:
        GmailSender().send("s", "b", "to@example.com")
    assert excinfo.value is original
