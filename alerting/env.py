from __future__ import annotations

"""Env-var resolution for the alerting layer.

Reads Gmail / Twilio / recipient env vars from ``os.environ`` **at send time**
(never at import time). All returned values are ``.strip()``'d; a whitespace-only
value is treated as MISSING. When one or more required vars are missing, a single
``AlertNotConfiguredError`` naming every missing var (in the requested order) is
raised.

No shape/format validation (no E.164, no email regex) -- presence + non-empty
only; correctness is delegated to SMTP / Twilio at send time.

The raised type is ``AlertNotConfiguredError`` (an ``AlertError`` subclass), NOT
a plain ``AlertError``: "no credentials set" means the channel was never going
to send and must be SKIPPED by the dispatcher, whereas a channel that is
configured and then fails is a genuine delivery failure. Only the missing var
NAMES are reported -- never a value.
"""

import os
from dataclasses import dataclass

from constants import (
    ENV_GMAIL_APP_PASSWORD,
    ENV_GMAIL_USER,
    ENV_TWILIO_AUTH,
    ENV_TWILIO_FROM,
    ENV_TWILIO_SID,
)
from errors import AlertNotConfiguredError


@dataclass(frozen=True)
class EmailCredentials:
    """Gmail SMTP credentials, resolved (stripped) from env."""

    user: str  # from GMAIL_USER
    app_password: str  # from GMAIL_APP_PASSWORD


@dataclass(frozen=True)
class SmsCredentials:
    """Twilio credentials, resolved (stripped) from env."""

    sid: str  # from TWILIO_SID
    auth: str  # from TWILIO_AUTH
    from_: str  # from TWILIO_FROM


def _require_env(names: tuple[str, ...]) -> dict[str, str]:
    """Read ``os.environ`` for each name in ``names``.

    A value is MISSING if the key is absent OR strips to empty (whitespace-only
    ⇒ missing). Present values are returned ``.strip()``'d. ALL missing names are
    collected in the SAME ORDER they were requested and reported in ONE
    ``AlertNotConfiguredError``. Never reads env at import time.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            missing.append(name)
            continue
        stripped = raw.strip()
        if stripped == "":
            missing.append(name)
            continue
        resolved[name] = stripped
    if missing:
        raise AlertNotConfiguredError(
            "missing required environment variable(s): " + ", ".join(missing)
        )
    return resolved


def resolve_email_credentials() -> EmailCredentials:
    """Resolve Gmail credentials from env (stripped). Raises
    ``AlertNotConfiguredError`` listing all missing vars."""
    env = _require_env((ENV_GMAIL_USER, ENV_GMAIL_APP_PASSWORD))
    return EmailCredentials(
        user=env[ENV_GMAIL_USER],
        app_password=env[ENV_GMAIL_APP_PASSWORD],
    )


def resolve_sms_credentials() -> SmsCredentials:
    """Resolve Twilio credentials from env (stripped). Raises
    ``AlertNotConfiguredError`` listing all missing vars."""
    env = _require_env((ENV_TWILIO_SID, ENV_TWILIO_AUTH, ENV_TWILIO_FROM))
    return SmsCredentials(
        sid=env[ENV_TWILIO_SID],
        auth=env[ENV_TWILIO_AUTH],
        from_=env[ENV_TWILIO_FROM],
    )


def resolve_recipient(env_name: str) -> str:
    """Resolve a single recipient env var (e.g. ``ALERT_EMAIL`` / ``ALERT_PHONE``)
    from env, stripped. Raises ``AlertNotConfiguredError`` if absent or
    whitespace-only."""
    return _require_env((env_name,))[env_name]
