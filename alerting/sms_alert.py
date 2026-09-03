from __future__ import annotations

"""Twilio SMS sender.

``SmsSender`` Protocol + concrete ``TwilioSender``. The ``twilio`` import is
DEFERRED into the default ``client_factory`` so importing ``alerting`` /
``sms_alert`` never requires ``twilio`` at collection time. Concrete
``ClientLike`` / ``MessagesLike`` Protocols type the injection seam; the whole
client I/O boundary is wrapped in a broad, import-safe catch re-raised as
``AlertError`` (with an explicit ``ModuleNotFoundError`` arm for a missing
``twilio``). The wrapper message names the underlying exception CLASS and
carries a sanitized rendering of its message (``describe_failure``) so the cause
is not lost; credential values and the destination number are redacted. The ``body[:SMS_MAX_LENGTH]`` slice is a last-resort hard cap --
real, URL-preserving truncation lives in ``formatting.sms_body``.
"""

from typing import Callable, Protocol, cast

from constants import SMS_MAX_LENGTH, TWILIO_HTTP_TIMEOUT_SECONDS
from errors import AlertError

from alerting.env import SmsCredentials, resolve_sms_credentials
from alerting.failure import describe_failure


class MessagesLike(Protocol):
    def create(self, *, body: str, from_: str, to: str) -> object: ...


class ClientLike(Protocol):
    @property
    def messages(self) -> MessagesLike: ...


class SmsSender(Protocol):
    def send(self, body: str, to_phone: str) -> None: ...


def _default_client_factory(sid: str, auth: str) -> ClientLike:
    """Build a real Twilio ``Client`` behind ``ClientLike``.

    Deferred imports so ``import sms_alert`` never needs ``twilio`` at
    collection. The SDK ``Client`` takes NO ``timeout=`` directly -- the HTTP
    timeout must be set on a ``TwilioHttpClient`` passed via ``http_client=``.
    """
    from twilio.http.http_client import (  # type: ignore[import-untyped]  # no stubs
        TwilioHttpClient,
    )
    from twilio.rest import Client  # type: ignore[import-untyped]  # no stubs

    client = Client(
        sid,
        auth,
        http_client=TwilioHttpClient(timeout=TWILIO_HTTP_TIMEOUT_SECONDS),
    )
    # The real Client structurally satisfies ClientLike; cast the untyped SDK
    # object across the seam so callers stay statically typed.
    return cast("ClientLike", client)


class TwilioSender:
    """Concrete Twilio SMS sender.

    Resolves credentials lazily (at send time) unless injected; a fresh
    ``Client`` is constructed per send (fine at our volume). Injection of
    ``creds`` / ``client_factory`` is for TEST / advanced use; production leaves
    both defaulted.
    """

    def __init__(
        self,
        creds: SmsCredentials | None = None,
        client_factory: Callable[[str, str], ClientLike] | None = None,
    ) -> None:
        self._creds = creds
        self._factory = client_factory or _default_client_factory

    def send(self, body: str, to_phone: str) -> None:
        # Bound before the try so the failure handler can always build the
        # redaction set, even if credential resolution itself blew up.
        creds: SmsCredentials | None = None
        try:
            creds = self._creds or resolve_sms_credentials()
            client = self._factory(creds.sid, creds.auth)  # deferred import here
            client.messages.create(
                body=body[:SMS_MAX_LENGTH],  # last-resort hard cap; normally a
                from_=creds.from_,           # no-op (sms_body already fits)
                to=to_phone,
            )
        except AlertError:
            raise  # pre-existing AlertError (e.g. from resolve_sms_credentials)
        except ModuleNotFoundError as exc:
            raise AlertError(
                f"twilio not installed: {describe_failure(exc)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- scoped to client I/O boundary
            # Covers TwilioException / OSError / requests ConnectionError|Timeout.
            # There is no logic between the boundary calls, so this is not masking
            # a logic bug. Name the cause's CLASS and carry its sanitized message;
            # every credential value plus the destination number is redacted
            # before the message is capped.
            secrets = [to_phone]
            if creds is not None:
                secrets += [creds.auth, creds.sid, creds.from_]
            raise AlertError(
                f"sms send failed: {describe_failure(exc, secrets)}"
            ) from exc
