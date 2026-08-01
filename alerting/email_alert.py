from __future__ import annotations

"""Gmail SMTP email sender.

``EmailSender`` Protocol + concrete ``GmailSender`` using ``SMTP_SSL`` (implicit
TLS on port 465) with a socket timeout. Builds a UTF-8 ``EmailMessage`` whose
native header handling raises ``ValueError`` on embedded CR/LF -- this IS the
header-injection guard for all header fields. Only ``SMTPException`` / ``OSError``
/ header ``ValueError`` are wrapped in ``AlertError`` (no blanket ``except``);
credentials are never interpolated into error messages.
"""

import smtplib
from email.message import EmailMessage
from typing import Protocol

from constants import GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, SMTP_TIMEOUT_SECONDS
from errors import AlertError

from alerting.env import EmailCredentials, resolve_email_credentials


class EmailSender(Protocol):
    def send(self, subject: str, body: str, to_addr: str) -> None: ...


class GmailSender:
    """Concrete Gmail SMTP sender.

    Resolves credentials lazily (at send time) unless injected, so construction
    is side-effect free. Injection is for TEST / advanced use; production leaves
    ``creds=None`` and resolves from env at send time.
    """

    def __init__(self, creds: EmailCredentials | None = None) -> None:
        self._creds = creds

    def send(self, subject: str, body: str, to_addr: str) -> None:
        try:
            creds = self._creds or resolve_email_credentials()
            msg = EmailMessage()
            # EmailMessage natively UTF-8-encodes headers AND raises ValueError
            # on CR/LF in any header value -- the header-injection guard.
            msg["Subject"] = subject
            msg["From"] = creds.user
            msg["To"] = to_addr
            msg.set_content(body)  # UTF-8 by default (em dashes / curly quotes)
            # SMTP_SSL ⇒ implicit TLS on port 465 (constants.GMAIL_SMTP_PORT).
            # Switching to 587 would require SMTP() + starttls() instead.
            with smtplib.SMTP_SSL(
                GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS
            ) as server:
                server.login(creds.user, creds.app_password)
                server.send_message(msg)
        except AlertError:
            raise  # already ours (e.g. from resolve_email_credentials)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            # Sanitized message -- never interpolate credentials.
            raise AlertError("email send failed") from exc
