from __future__ import annotations

"""Dispatch seam -- the public entry point of the alerting layer.

``Dispatcher`` holds constructor-injected email + SMS senders (or ``None`` =
that channel disabled). ``dispatch_event`` formats via ``formatting``, looks up
routing, resolves recipients from env AT SEND TIME (only for channels that will
actually fire), and attempts each routed+enabled channel INDEPENDENTLY in a
canonical EMAIL->SMS order. Both ``dispatch_event`` and ``dispatch_events``
NEVER raise -- outcomes are reported in ``DispatchResult``.

Fail-soft model: a per-channel failure is recorded in ``errors`` and does not
suppress the other channel; a formatting/``build_alert`` failure is recorded in
``event_error`` (no channels attempted).

A routed channel resolves to exactly ONE of three outcomes, and the THREE-way
split is load-bearing for the orchestrator's dedupe commit:

  * SENT      -- listed in ``channels_sent``.
  * SKIPPED   -- listed in ``channels_skipped`` with a reason in
                 ``skipped_reasons``. Either the sender is ``None`` (channel
                 disabled by construction) or the channel has no credentials /
                 recipient configured (``AlertNotConfiguredError``). NOT counted
                 as attempted and NOT an error: nothing was ever going to be
                 sent, so treating it as a failure would block the dedupe commit
                 forever and re-alert on an already-delivered channel.
  * FAILED    -- listed in ``channels_attempted`` with the reason in ``errors``.
                 The channel WAS configured, was tried, and the send failed.
                 This is what holds back the commit so the event re-fires.
"""

from dataclasses import dataclass
from functools import partial
from typing import Callable, Sequence

from config import AppConfig
from errors import AlertError, AlertNotConfiguredError
from models import Alert, AlertChannel, DetectedEvent

from alerting.email_alert import EmailSender
from alerting.env import resolve_recipient
from alerting.formatting import build_alert, sms_body
from alerting.sms_alert import SmsSender

# Canonical, deterministic dispatch order (independent of config tuple order).
_CHANNEL_ORDER: tuple[AlertChannel, ...] = (AlertChannel.EMAIL, AlertChannel.SMS)

# Skip reason when the Dispatcher was constructed with no sender for a channel
# (as opposed to a sender that exists but has no credentials configured).
REASON_SENDER_DISABLED: str = "channel disabled (no sender configured)"


@dataclass(frozen=True)
class DispatchResult:
    event: DetectedEvent
    # Channels that were configured and actually tried (sent OR failed).
    channels_attempted: tuple[AlertChannel, ...]
    channels_sent: tuple[AlertChannel, ...]
    # Routed but never tried: sender is None, or no credentials/recipient set.
    channels_skipped: tuple[AlertChannel, ...]
    errors: dict[AlertChannel, str]  # empty dict = all ATTEMPTED channels OK
    # Why each skipped channel was skipped (never contains a credential value).
    skipped_reasons: dict[AlertChannel, str]
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
                channels_skipped=(),
                errors={},
                skipped_reasons={},
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
                channels_skipped=(),
                errors={},
                skipped_reasons={},
                event_error=str(exc),
                skipped=False,
            )

        routed = frozenset(alert.channels)
        attempted: list[AlertChannel] = []
        sent: list[AlertChannel] = []
        channels_skipped: list[AlertChannel] = []
        errors: dict[AlertChannel, str] = {}
        skipped_reasons: dict[AlertChannel, str] = {}

        for channel in _CHANNEL_ORDER:
            if channel not in routed:
                continue
            # Bind this channel's send as a zero-arg callable, or skip it when
            # the Dispatcher holds no sender for it. Unknown channels never
            # appear in _CHANNEL_ORDER, so the else-branch is exhaustive.
            send: Callable[[], None]
            if channel is AlertChannel.EMAIL:
                email_sender = self._email_sender
                if email_sender is None:
                    channels_skipped.append(channel)
                    skipped_reasons[channel] = REASON_SENDER_DISABLED
                    continue
                send = partial(self._send_email, email_sender, alert, config)
            else:
                sms_sender = self._sms_sender
                if sms_sender is None:
                    channels_skipped.append(channel)
                    skipped_reasons[channel] = REASON_SENDER_DISABLED
                    continue
                send = partial(self._send_sms, sms_sender, event, config)

            # Each channel is isolated so one channel's failure never suppresses
            # the other. AlertNotConfiguredError is caught FIRST (it subclasses
            # AlertError) and routed to SKIPPED, not to errors -- see the module
            # docstring for why that distinction matters to the dedupe commit.
            not_configured: str | None = None
            failure: str | None = None
            try:
                send()
            except AlertNotConfiguredError as exc:
                not_configured = str(exc)
            except AlertError as exc:
                failure = str(exc)
            except Exception as exc:  # noqa: BLE001 -- stray-error backstop
                failure = str(exc)

            if not_configured is not None:
                channels_skipped.append(channel)
                skipped_reasons[channel] = not_configured
            elif failure is not None:
                attempted.append(channel)
                errors[channel] = failure
            else:
                attempted.append(channel)
                sent.append(channel)

        return DispatchResult(
            event=event,
            channels_attempted=tuple(attempted),
            channels_sent=tuple(sent),
            channels_skipped=tuple(channels_skipped),
            errors=errors,
            skipped_reasons=skipped_reasons,
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
                        channels_skipped=(),
                        errors={},
                        skipped_reasons={},
                        event_error=str(exc),
                        skipped=False,
                    )
                )
        return results

    # -- internals -------------------------------------------------------- #
    # Recipient env resolution happens HERE, only for a firing channel, so an
    # email-only routing never requires ALERT_PHONE. A missing recipient raises
    # AlertNotConfiguredError -> that channel is SKIPPED (not failed) by the
    # per-channel handler above.

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
