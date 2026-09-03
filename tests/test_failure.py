from __future__ import annotations

"""Tests for ``alerting/failure.py`` -- sanitized rendering of a send failure.

The contract these guard: the exception CLASS always survives (it is the single
most diagnostic token when triaging an alerting outage), and no credential VALUE
ever appears in the rendered string -- including when the message is long enough
to be truncated.
"""

import smtplib

import pytest

from alerting.failure import describe_failure
from constants import ALERT_FAILURE_DETAIL_MAX_CHARS, REDACTED_PLACEHOLDER


def test_class_name_and_message_both_present() -> None:
    rendered = describe_failure(OSError("connection refused"))
    assert rendered == "OSError: connection refused"


def test_empty_message_renders_class_name_alone() -> None:
    assert describe_failure(ValueError()) == "ValueError"


def test_real_smtp_auth_exception_keeps_its_class() -> None:
    exc = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    rendered = describe_failure(exc)
    assert rendered.startswith("SMTPAuthenticationError:")
    assert "535" in rendered


@pytest.mark.parametrize(
    "secret",
    ["hunter2-app-password", "sender@example.com", "+15559998888"],
)
def test_secret_values_are_redacted(secret: str) -> None:
    exc = OSError(f"rejected credential {secret} at handshake")
    rendered = describe_failure(exc, [secret])
    assert secret not in rendered
    assert REDACTED_PLACEHOLDER in rendered
    assert "OSError" in rendered


def test_all_secrets_redacted_in_one_pass() -> None:
    exc = OSError("user=u@example.com pass=s3cret to=+15551234567")
    rendered = describe_failure(exc, ["u@example.com", "s3cret", "+15551234567"])
    for secret in ("u@example.com", "s3cret", "+15551234567"):
        assert secret not in rendered
    assert rendered.count(REDACTED_PLACEHOLDER) == 3


def test_blank_and_whitespace_secrets_are_ignored() -> None:
    """An unset credential must not turn the whole message into placeholders."""
    exc = OSError("connection refused")
    rendered = describe_failure(exc, ["", "   "])
    assert rendered == "OSError: connection refused"


def test_long_message_is_capped() -> None:
    exc = OSError("x" * (ALERT_FAILURE_DETAIL_MAX_CHARS * 3))
    rendered = describe_failure(exc)
    # Class name + ": " + capped detail.
    assert len(rendered) <= len("OSError: ") + ALERT_FAILURE_DETAIL_MAX_CHARS
    assert rendered.startswith("OSError: ")


def test_secret_is_redacted_before_truncation() -> None:
    """Regression guard: capping first could slice a secret in half and leave
    the surviving fragment in the message."""
    secret = "SUPERSECRETVALUE"
    # Place the secret right at the truncation boundary.
    filler = "y" * (ALERT_FAILURE_DETAIL_MAX_CHARS - len(secret) // 2)
    exc = OSError(filler + secret + "tail")
    rendered = describe_failure(exc, [secret])
    assert secret not in rendered
    # No partial fragment of the secret survives either.
    for length in range(6, len(secret) + 1):
        assert secret[:length] not in rendered
