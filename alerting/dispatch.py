from __future__ import annotations

"""Dispatch seam -- the public entry point of the alerting layer.

``Dispatcher`` holds constructor-injected email + SMS senders (or ``None`` =
that channel disabled). ``dispatch_event`` formats via ``formatting``, looks up
routing, resolves recipients from env AT SEND TIME (only for channels that will
actually fire), and attempts each routed+enabled channel INDEPENDENTLY in a
canonical EMAIL->SMS order. Both ``dispatch_event`` and ``dispatch_events``
NEVER raise -- failures are reported in ``DispatchResult``.

Fail-soft model: a per-channel failure is recorded in ``errors`` and does not
suppress the other channel; a formatting/``build_alert`` failure is recorded in
``event_error`` (no channels attempted); a routed channel whose sender is
``None`` (disabled) is silently omitted -- NOT an error (see implementation
notes for the operational significance).
"""

from dataclasses import dataclass
from typing import Sequence

from config import AppConfig
from errors import AlertError
from models import Alert, AlertChannel, DetectedEvent

from alerting.email_alert import EmailSender
from alerting.env import resolve_recipient
from alerting.formatting import build_alert, sms_body
from alerting.sms_alert import SmsSender

# Canonical, deterministic dispatch order (independent of config tuple order).
_CHANNEL_ORDER: tuple[AlertChannel, ...] = (AlertChannel.EMAIL, AlertChannel.SMS)


@dataclass(frozen=True)
class DispatchResult:
    event: DetectedEvent
    channels_attempted: tuple[AlertChannel, ...]
    channels_sent: tuple[AlertChannel, ...]
    errors: dict[AlertChannel, str]  # empty dict = all attempted channels OK
    event_error: str | None  # non-channel failure (build_alert / formatting)
    skipped: bool  # True ONLY for dry-run


class Dispatcher:
    def __init__(
        self,
        email_sender: EmailSender | None,
        sms_sender: SmsSender | None,
        *,
        dry_run: bool = False,
    ) -> None:
        self._email_sender = email_sender
        self._sms_sender = sms_sender
        self._dry_run = dry_run

    def dispatch_event(
        self, event: DetectedEvent, config: AppConfig
    ) -> DispatchResult:
        """Format + route + send a single event. NEVER raises."""
        # Dry-run short-circuit: nothing attempted, no env required, no format.
        if self._dry_run:
            return DispatchResult(
                event=event,
                channels_attempted=(),
                channels_sent=(),
                errors={},
                event_error=None,
                skipped=True,
            )

        try:
            alert = build_alert(event, config)
        except Exception as exc:  # noqa: BLE001 -- formatting failure ⇒ event_error
            return DispatchResult(
                event=event,
                channels_attempted=(),
                channels_sent=(),
                errors={},
                event_error=str(exc),
                skipped=False,
            )

        routed = frozenset(alert.channels)
        attempted: list[AlertChannel] = []
        sent: list[AlertChannel] = []
        errors: dict[AlertChannel, str] = {}

        for channel in _CHANNEL_ORDER:
            if channel not in routed:
                continue
            # Each channel handled in its own isolated try/except so one
            # channel's failure never suppresses the other. A routed channel
            # whose sender is None (disabled) is silently omitted (not attempted,
            # not an error). Unknown channels never appear in _CHANNEL_ORDER.
            if channel is AlertChannel.EMAIL:
                if self._email_sender is None:
                    continue
                attempted.append(channel)
                try:
                    self._send_email(self._email_sender, alert, config)
                except AlertError as exc:
                    errors[channel] = str(exc)
                except Exception as exc:  # noqa: BLE001 -- stray-error backstop
                    errors[channel] = str(exc)
                else:
                    sent.append(channel)
            elif channel is AlertChannel.SMS:
                if self._sms_sender is None:
                    continue
                attempted.append(channel)
                try:
                    self._send_sms(self._sms_sender, event, config)
                except AlertError as exc:
                    errors[channel] = str(exc)
                except Exception as exc:  # noqa: BLE001 -- stray-error backstop
                    errors[channel] = str(exc)
                else:
                    sent.append(channel)

        return DispatchResult(
            event=event,
            channels_attempted=tuple(attempted),
            channels_sent=tuple(sent),
            errors=errors,
            event_error=None,
            skipped=False,
        )

    def dispatch_events(
        self, events: Sequence[DetectedEvent], config: AppConfig
    ) -> list[DispatchResult]:
        """Dispatch a batch. Returns one ``DispatchResult`` per input event, in
        the same order/length. NEVER raises; one event's failure never aborts
        the rest."""
        results: list[DispatchResult] = []
        for event in events:
            try:
                results.append(self.dispatch_event(event, config))
            except Exception as exc:  # noqa: BLE001 -- backstop; should not happen
                results.append(
                    DispatchResult(
                        event=event,
                        channels_attempted=(),
                        channels_sent=(),
                        errors={},
                        event_error=str(exc),
                        skipped=False,
                    )
                )
        return results

    # -- internals -------------------------------------------------------- #
    # Recipient env resolution happens HERE, only for a firing channel, so an
    # email-only routing never requires ALERT_PHONE. A missing recipient raises
    # AlertError, caught by the per-channel handler above.

    @staticmethod
    def _send_email(
        sender: EmailSender, alert: Alert, config: AppConfig
    ) -> None:
        to_email = resolve_recipient(config.alert_recipients.email_env)
        sender.send(alert.subject, alert.body, to_email)

    @staticmethod
    def _send_sms(
        sender: SmsSender, event: DetectedEvent, config: AppConfig
    ) -> None:
        to_phone = resolve_recipient(config.alert_recipients.phone_env)
        sender.send(sms_body(event), to_phone)
