from __future__ import annotations

"""Orchestrator entrypoint for fomo-fund-monitor (Prompt 6).

``run(now, ...)`` drives one monitoring pass: build config + clients + monitor
specs, interval-gate each monitor, run it in isolation, dispatch alerts, commit
per-event dedupe state ONLY after a successful alert dispatch, then fire the
optional ``repository_dispatch`` bridge for each committed event. ``main()`` is a
thin shell that parses argv, wires the real clock + logging, and translates the
return code into a process exit code.

CLI::

    python main.py                            # one monitoring pass (the cron)
    python main.py --dry-run                  # ... sending nothing
    python main.py --replay-since 2026-08-14  # re-emit past alerts (edgar only)
    python main.py --replay-since 2026-08-14 --monitor youtube --limit 10
    python main.py --backfill-seeds --dry-run # seed pre-seed entries the seed missed

Replay lives in ``replay.py`` and is imported lazily, so a normal cron pass never
pays for it.

Design rules honored here:
  * Filing date / observation is the anchor -- monitors already stamp events;
    the orchestrator is timing-agnostic beyond ``now`` and interval gating.
  * Commit-after-dispatch (Option B monitors): an event is marked seen ONLY
    after its alert dispatch succeeds, so a failed alert re-fires next run.
    "Succeeds" means at least one channel DELIVERED and no CONFIGURED channel
    failed -- a routed channel with no credentials set is skipped, not failed,
    so an unconfigured optional channel cannot block the commit and re-alert
    forever on a channel that already delivered (see ``alert_delivered``).
    Option A monitors (conference_pages, website_diff page-hash) persist their
    own state inside the monitor; the orchestrator does NOT re-commit them.
  * Fail-soft: one monitor's failure never aborts the others; a bridge failure
    never affects alerting, mark-seen, ``record_run``, or the exit code.
  * ``last_run`` advances ONLY for a monitor whose check returned -- i.e. that
    actually observed at least one source. A run in which every query errored
    leaves the timestamp alone so the monitor retries immediately.
  * NO import-time side effects. ``logging.basicConfig`` runs INSIDE ``main()``.
  * Exit 0 for a normal run (even with per-monitor failures); exit 2 ONLY for a
    fatal config load or startup state-probe failure. A run in which any alert
    failed to DELIVER raises ``AlertDeliveryError`` out of ``run()`` after all
    monitors have finished -- uncaught by design, so the traceback (including
    the chained cause) reaches the CI log and the process exits non-zero.
  * ``dry_run=True`` short-circuits ALL commits + bridge unconditionally and
    runs monitors against a throwaway temp copy of state so the real ``state/``
    is never touched.

Heavy transport deps (``GmailSender`` / ``TwilioSender``, the concrete HTTP
clients) are imported lazily inside ``run()`` so importing this module pulls no
network stack (preserves ``test_main_importable_no_side_effects``).
"""

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, Sequence

from dotenv import load_dotenv

# Load a repo-root .env before any config/env reads (QoL for local runs).
# load_dotenv() is quiet (no stdout/stderr, no network), so it preserves the
# import-time invariants guarded by test_main_importable_no_side_effects and
# test_import_main_pulls_no_network_stack.
load_dotenv()

import constants
from config import AppConfig, load_config
from dispatch_bridge import DispatchBridge, build_bridge_payload
from errors import (
    AlertDeliveryError,
    ConfigError,
    DispatchBridgeAuthError,
    DispatchBridgeError,
    StateError,
)
from models import AlertChannel, DetectedEvent, EventType, MonitorName
from state_manager import AppearanceKind, StateStore

if TYPE_CHECKING:
    # Client Protocols imported for typing only; the concrete classes are built
    # via deferred imports inside run() so importing main pulls no network stack.
    from monitors._common import FeedClient
    from monitors.cnbc import CnbcClient
    from monitors.edgar import EdgarClient
    from monitors.youtube import YouTubeClient

logger = logging.getLogger("fomo_monitor")


# --------------------------------------------------------------------------- #
# DI Protocols -- run() depends on these small seams, not concrete classes, so
# tests inject fakes with no network / no real state. The concrete StateStore,
# Dispatcher, and RequestsDispatchBridge satisfy them structurally.
# --------------------------------------------------------------------------- #


class StoreLike(Protocol):
    """The subset of ``StateStore`` the orchestrator drives."""

    def should_run(
        self, monitor_name: str, now: datetime, intervals: dict[str, int]
    ) -> bool: ...

    def record_run(self, monitor_name: str, now: datetime) -> None: ...

    def load_last_run(self) -> dict[str, str]: ...

    def load_seen_appearances(self) -> object: ...

    def mark_filing_seen(self, entity_key: str, accession: str) -> None: ...

    def mark_appearance_seen(
        self, kind: AppearanceKind, identifier: str
    ) -> None: ...


class DispatchResultLike(Protocol):
    """The subset of ``DispatchResult`` the orchestrator reads.

    ``errors`` holds ONLY genuine send failures on channels that were configured
    and tried. A routed channel with no credentials/recipient set lands in
    ``channels_skipped`` / ``skipped_reasons`` instead, and never in ``errors``.
    ``channels_sent`` is what proves the alert actually reached someone."""

    @property
    def routed(self) -> Sequence[AlertChannel]: ...

    @property
    def channels_sent(self) -> Sequence[AlertChannel]: ...

    @property
    def channels_skipped(self) -> Sequence[AlertChannel]: ...

    @property
    def errors(self) -> Mapping[AlertChannel, str]: ...

    @property
    def skipped_reasons(self) -> Mapping[AlertChannel, str]: ...

    @property
    def event_error(self) -> str | None: ...


def alert_delivered(result: DispatchResultLike) -> bool:
    """True when an event's alert cleanly reached at least one channel.

    Three conditions, ALL required:

    1. No event-level failure (``build_alert`` / formatting).
    2. No genuine send failure on a CONFIGURED channel. A routed channel that is
       merely unconfigured is SKIPPED, not failed -- so an absent optional
       channel (e.g. SMS with no Twilio secrets) never blocks the dedupe commit
       and never causes indefinite re-alerting on an already-delivered email.
    3. At least one channel actually delivered -- UNLESS the event was routed to
       no channels at all. Those two look identical in ``channels_sent`` but mean
       opposite things:

         * routed somewhere, nothing delivered -> an alerting-layer OUTAGE.
           Deliberately NOT delivered, so the event re-fires once alerting works.
         * routed NOWHERE (``alert_routing: []``) -> a SILENT CAPTURE, on
           purpose. There was nothing to deliver, so it counts as delivered and
           the dedupe state commits; the data accrues for later analysis without
           reaching an inbox. Without this branch a silenced monitor would
           re-detect the same events forever and never commit.
    """
    if result.event_error is not None:
        return False
    if result.errors:
        return False
    if not result.routed:
        return True  # silent capture: nothing to deliver, so nothing failed
    return bool(result.channels_sent)


class DispatcherLike(Protocol):
    """The subset of ``Dispatcher`` the orchestrator drives."""

    def dispatch_events(
        self, events: Sequence[DetectedEvent], config: AppConfig
    ) -> Sequence[DispatchResultLike]: ...


# --------------------------------------------------------------------------- #
# Clients + MonitorSpec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Clients:
    """Concrete transport clients, one per family. Built lazily inside
    ``run()`` (deferred imports) and passed to ``build_monitor_specs``."""

    edgar: "EdgarClient"
    youtube: "YouTubeClient"
    feed: "FeedClient"  # shared FeedClient (podcast_rss / google_news / conference / website_diff)
    cnbc: "CnbcClient"


# A commit function: given a committed event, persist its dedupe state.
CommitFn = Callable[[DetectedEvent], None]
# A per-event retryable predicate: True => a FAILED alert leaves the event
# un-committed so it re-fires next run (Option B). False => one-shot (Option A /
# undefined shape): do not re-commit regardless.
RetryableFn = Callable[[DetectedEvent], bool]


@dataclass(frozen=True)
class MonitorSpec:
    """One monitor's wiring: its name, its bound check callable, its per-event
    commit routing, and its per-event retryable predicate."""

    name: MonitorName
    run_check: Callable[[], list[DetectedEvent]]
    commit: CommitFn
    retryable: RetryableFn


def _always_retryable(_event: DetectedEvent) -> bool:
    return True


def _never_retryable(_event: DetectedEvent) -> bool:
    return False


def _noop_commit(_event: DetectedEvent) -> None:
    """Option-A monitors persist their own state inside the monitor; there is
    nothing extra for the orchestrator to commit."""
    return None


def build_monitor_specs(
    config: AppConfig,
    store: StateStore,
    clients: Clients,
    now: datetime,
) -> list[MonitorSpec]:
    """Build the ordered list of monitor specs with REAL commit routing.

    The 5-case mark-seen table:
      1. EDGAR                         -> mark_filing_seen(entity_key, id)
      2. youtube                       -> mark_appearance_seen("youtube", id)
      3. cnbc / google_news            -> mark_appearance_seen("urls", id)
      4. podcast_rss                   -> mark_appearance_seen("rss_guids", id)
      5. conference_pages              -> no-op (Option A; monitor persisted)
         website_diff                  -> MIXED by event_type:
             LEOPOLD_POST              -> mark_appearance_seen("rss_guids", id)
             WEBSITE_DIFF              -> no-op (Option A; monitor persisted)
             (any other)               -> WARNING + no-op (undefined shape)
    """
    from monitors.cnbc import check_cnbc
    from monitors.conference_pages import check_conference_pages
    from monitors.edgar import check_edgar
    from monitors.google_news import check_google_news
    from monitors.podcast_rss import check_podcast_rss
    from monitors.website_diff import check_website_diff
    from monitors.youtube import check_youtube

    def _commit_edgar(event: DetectedEvent) -> None:
        store.mark_filing_seen(event.entity_key, event.identifier)

    def _commit_youtube(event: DetectedEvent) -> None:
        store.mark_appearance_seen("youtube", event.identifier)

    def _commit_urls(event: DetectedEvent) -> None:
        store.mark_appearance_seen("urls", event.identifier)

    def _commit_rss(event: DetectedEvent) -> None:
        store.mark_appearance_seen("rss_guids", event.identifier)

    def _commit_website_diff(event: DetectedEvent) -> None:
        # MIXED routing keyed by the event's own EventType.
        if event.event_type == EventType.LEOPOLD_POST:
            store.mark_appearance_seen("rss_guids", event.identifier)
        elif event.event_type == EventType.WEBSITE_DIFF:
            # Option A: the monitor already persisted the content hash. No-op.
            return None
        else:
            # Undefined shape for this monitor; log and do not touch state.
            logger.warning(
                "website_diff produced unexpected event_type %s for %r; "
                "not committing dedupe state",
                event.event_type.value,
                event.identifier,
            )
        return None

    def _retryable_website_diff(event: DetectedEvent) -> bool:
        # Only LEOPOLD_POST (Option B) can be re-fired on a failed alert.
        # WEBSITE_DIFF (Option A) and any unexpected type are one-shot.
        return event.event_type == EventType.LEOPOLD_POST

    return [
        MonitorSpec(
            name=MonitorName.EDGAR,
            run_check=lambda: check_edgar(config, store, clients.edgar, now),
            commit=_commit_edgar,
            retryable=_always_retryable,
        ),
        MonitorSpec(
            name=MonitorName.YOUTUBE,
            run_check=lambda: check_youtube(config, store, clients.youtube, now),
            commit=_commit_youtube,
            retryable=_always_retryable,
        ),
        MonitorSpec(
            name=MonitorName.PODCAST_RSS,
            run_check=lambda: check_podcast_rss(config, store, clients.feed, now),
            commit=_commit_rss,
            retryable=_always_retryable,
        ),
        MonitorSpec(
            name=MonitorName.GOOGLE_NEWS,
            run_check=lambda: check_google_news(config, store, clients.feed, now),
            commit=_commit_urls,
            retryable=_always_retryable,
        ),
        MonitorSpec(
            name=MonitorName.CNBC,
            run_check=lambda: check_cnbc(config, store, clients.cnbc, now),
            commit=_commit_urls,
            retryable=_always_retryable,
        ),
        MonitorSpec(
            name=MonitorName.CONFERENCE_PAGES,
            run_check=lambda: check_conference_pages(
                config, store, clients.feed, now
            ),
            commit=_noop_commit,  # Option A; monitor persisted its own snapshot.
            retryable=_never_retryable,
        ),
        MonitorSpec(
            name=MonitorName.WEBSITE_DIFF,
            run_check=lambda: check_website_diff(config, store, clients.feed, now),
            commit=_commit_website_diff,
            retryable=_retryable_website_diff,
        ),
    ]


# --------------------------------------------------------------------------- #
# Bridge firing
# --------------------------------------------------------------------------- #


class _BridgeGate:
    """Per-run bridge state: enabled?, PAT-present (probed once), and an
    auth-short-circuit flag flipped on the first 401/403 so a bad PAT does not
    produce N doomed POSTs and N identical warnings."""

    def __init__(self, enabled: bool, pat_present: bool) -> None:
        self.active = enabled and pat_present
        self.auth_failed = False


def _fire_bridge_for_event(
    bridge: DispatchBridge,
    gate: _BridgeGate,
    config: AppConfig,
    monitor_name: str,
    event: DetectedEvent,
    now: datetime,
) -> None:
    """Fire the bridge for ONE committed event, fully defensively: any failure
    (including a programming error building the payload) is caught and logged;
    it never propagates."""
    if not gate.active or gate.auth_failed:
        return
    try:
        payload = build_bridge_payload(event, monitor_name, now)
        bridge.fire(config.dispatch_bridge.repo, config.dispatch_bridge.event_type, payload)
    except DispatchBridgeAuthError as exc:
        gate.auth_failed = True  # short-circuit the rest of the run
        logger.warning(
            "repository_dispatch bridge auth failure; disabling bridge for this "
            "run: %s",
            exc,
        )
    except DispatchBridgeError as exc:
        logger.warning("repository_dispatch bridge failed for %r: %s", event.identifier, exc)
    except Exception as exc:  # noqa: BLE001 -- bridge must NEVER crash the run
        logger.warning(
            "repository_dispatch bridge unexpected error for %r: %s",
            event.identifier,
            exc,
        )


def describe_alert_failure(result: DispatchResultLike) -> str:
    """Human-readable reason an event's alert did not deliver.

    This is the line that used to be missing entirely: the dispatcher recorded
    per-channel reasons in ``errors`` and the orchestrator read that map for
    truthiness only, so a total alerting outage surfaced as a content-free
    "alert failed for <id>". Every reason is now rendered.

    Channels are ordered by value for deterministic log output.
    """
    parts: list[str] = []
    if result.event_error is not None:
        parts.append(f"event: {result.event_error}")
    for channel in sorted(result.errors, key=lambda c: c.value):
        parts.append(f"{channel.value}: {result.errors[channel]}")
    if parts:
        return "; ".join(parts)
    # No per-channel error and no event error => nothing was ever attempted:
    # every routed channel was skipped as unconfigured (see alert_delivered).
    skipped = ", ".join(
        f"{c.value} ({result.skipped_reasons.get(c, 'unknown')})"
        for c in sorted(result.channels_skipped, key=lambda c: c.value)
    )
    return f"no channel delivered; all routed channels unconfigured: {skipped or 'none routed'}"


# --------------------------------------------------------------------------- #
# Per-monitor processing
# --------------------------------------------------------------------------- #


def _process_monitor(
    spec: MonitorSpec,
    config: AppConfig,
    store: StoreLike,
    dispatcher: DispatcherLike,
    bridge: DispatchBridge,
    gate: _BridgeGate,
    intervals: dict[str, int],
    now: datetime,
    failures: list[str],
    *,
    dry_run: bool,
) -> None:
    """Run a single monitor end-to-end in isolation.

    ``should_run`` is INSIDE the try/except so a corrupt ``last_run`` timestamp
    (raising ``StateError``) skips ONLY this monitor.

    ``record_run`` is in the ``finally`` but is gated on ``observed`` -- set only
    once ``run_check()`` has RETURNED. ``last_run`` therefore advances iff the
    monitor actually observed its sources:

      * ``should_run`` failed or said "not due"  -> not recorded (never ran).
      * ``run_check()`` raised                   -> NOT recorded. A monitor
        whose source units ALL failed raises ``MonitorError`` (see
        ``monitors/_outcome.py``), so a total source outage no longer stamps a
        timestamp that claims a successful poll and then hides the monitor
        behind its own interval gate.
      * ``run_check()`` returned, dispatch/commit failed later -> RECORDED. The
        poll DID succeed; a delivery failure is handled by leaving the event
        un-committed (it re-fires) and by the end-of-run raise.

    Genuine alert-delivery failures are logged at ERROR *with their reason* and
    appended to ``failures``; ``run()`` raises once the whole pass is done. They
    are recorded here but NOT raised here, so one monitor's alerting problem
    still does not abort the remaining monitors.
    """
    name = spec.name.value
    ran = False
    observed = False
    try:
        if not store.should_run(name, now, intervals):
            logger.debug("monitor %s not due; skipping", name)
            return
        ran = True
        events = spec.run_check()
        # The check returned, so at least one source unit produced a usable
        # observation (a monitor whose units ALL failed raises MonitorError --
        # see monitors/_outcome.py). Only now may last_run advance.
        observed = True
        logger.info("monitor %s produced %d event(s)", name, len(events))

        if dry_run:
            # Short-circuit ALL commits + bridge in dry-run, unconditionally.
            return

        results = dispatcher.dispatch_events(events, config)
        # Routed-but-unconfigured channels are aggregated across the batch and
        # reported ONCE per channel below -- per-event logging would emit one
        # identical line per event (115 for a single google_news run).
        skipped_counts: dict[AlertChannel, int] = {}
        skipped_reason: dict[AlertChannel, str] = {}
        # dispatch_events returns one result per event, same order/length.
        for event, result in zip(events, results):
            for channel in result.channels_skipped:
                skipped_counts[channel] = skipped_counts.get(channel, 0) + 1
                skipped_reason.setdefault(
                    channel, result.skipped_reasons.get(channel, "unknown")
                )
            alert_ok = alert_delivered(result)
            if not alert_ok:
                # Log the REASON, always. Recorded for the end-of-run raise
                # whether or not the event goes on to be committed below.
                reason = describe_alert_failure(result)
                logger.error(
                    "monitor %s: alert delivery FAILED for %r -- %s",
                    name,
                    event.identifier,
                    reason,
                )
                failures.append(f"{name}/{event.identifier}: {reason}")

            if alert_ok or not spec.retryable(event):
                # Commit when the alert cleanly dispatched, OR when the event is
                # one-shot (Option A / undefined shape): re-firing would never
                # help, so we still commit to avoid an infinite re-alert loop.
                try:
                    spec.commit(event)
                except Exception as exc:  # noqa: BLE001 -- commit fault isolated
                    logger.error(
                        "monitor %s: failed to commit dedupe state for %r: %s",
                        name,
                        event.identifier,
                        exc,
                    )
                    continue
                _fire_bridge_for_event(bridge, gate, config, name, event, now)
            else:
                # Retryable event whose alert failed: leave un-committed so it
                # re-fires next run. Do NOT fire the bridge (nothing committed).
                logger.warning(
                    "monitor %s: %r left un-committed to retry next run",
                    name,
                    event.identifier,
                )

        for channel in sorted(skipped_counts, key=lambda c: c.value):
            logger.warning(
                "monitor %s: alert channel %s not configured; skipped for "
                "%d event(s) (%s). Other routed channels still delivered.",
                name,
                channel.value,
                skipped_counts[channel],
                skipped_reason[channel],
            )
    except StateError as exc:
        logger.error("monitor %s: state error, skipping this monitor: %s", name, exc)
    except Exception as exc:  # noqa: BLE001 -- one monitor never aborts the rest
        logger.error("monitor %s failed: %s", name, exc)
    finally:
        if ran and observed and not dry_run:
            try:
                store.record_run(name, now)
            except Exception as exc:  # noqa: BLE001 -- record fault isolated
                logger.error("monitor %s: failed to record_run: %s", name, exc)
        elif ran and not dry_run:
            # The check itself failed, so this run observed nothing. Leaving
            # last_run untouched makes the monitor due again on the very next
            # pass instead of waiting out its interval behind a timestamp that
            # claims a successful poll.
            logger.warning(
                "monitor %s: check failed; NOT advancing last_run so the "
                "monitor retries on the next pass",
                name,
            )


# --------------------------------------------------------------------------- #
# Public run()
# --------------------------------------------------------------------------- #


def _build_state_store(
    config: AppConfig, *, dry_run: bool
) -> tuple[StateStore, "tempfile.TemporaryDirectory[str] | None"]:
    """Build the StateStore. In a normal run it points at the real state dir. In
    dry-run it points at a throwaway temp dir seeded from the real state (copied
    if it exists, else empty) so monitors read realistic prior state but NOTHING
    is written back to the real ``state/``. Returns the store plus an optional
    ``TemporaryDirectory`` handle (kept alive by the caller until run end)."""
    if not dry_run:
        return StateStore(config.paths.state_dir), None

    tmp = tempfile.TemporaryDirectory(prefix="fomo-dryrun-state-")
    tmp_state = Path(tmp.name) / "state"
    real_state = config.paths.state_dir
    if real_state.exists():
        shutil.copytree(real_state, tmp_state)
    else:
        tmp_state.mkdir(parents=True, exist_ok=True)
    return StateStore(tmp_state), tmp


def build_clients() -> Clients:
    """Construct the concrete transport clients via DEFERRED imports so importing
    ``main`` pulls no network stack. Public because ``replay.py`` builds the same
    real clients for its replay pass."""
    from monitors._common import RequestsFeedClient
    from monitors.cnbc import CnbcHttpClient
    from monitors.edgar import EdgarHttpClient
    from monitors.youtube import YouTubeApiClient

    return Clients(
        edgar=EdgarHttpClient(),
        youtube=YouTubeApiClient(),
        feed=RequestsFeedClient(),
        cnbc=CnbcHttpClient(),
    )


def _build_dispatcher(config: AppConfig, *, dry_run: bool) -> DispatcherLike:
    """Construct the real ``Dispatcher`` with lazily-imported senders. In
    dry-run, senders are still constructed (side-effect free) but the Dispatcher
    is put in dry-run mode so nothing is ever sent."""
    from alerting.dispatch import Dispatcher
    from alerting.email_alert import GmailSender
    from alerting.sms_alert import TwilioSender

    email_sender = GmailSender()
    sms_sender = TwilioSender()
    return Dispatcher(email_sender, sms_sender, dry_run=dry_run)


def _build_bridge() -> DispatchBridge:
    from dispatch_bridge import RequestsDispatchBridge

    return RequestsDispatchBridge()


def run(
    now: datetime,
    *,
    dry_run: bool = False,
    config_path: str | Path | None = None,
    store: StoreLike | None = None,
    dispatcher: DispatcherLike | None = None,
    bridge: DispatchBridge | None = None,
    monitors: list[MonitorSpec] | None = None,
) -> int:
    """Run one monitoring pass. Returns a process exit code (0 normal, 2 fatal).

    All collaborators are injectable for testing (``store`` / ``dispatcher`` /
    ``bridge`` / ``monitors``); when omitted they are built from real config +
    deferred concrete clients.
    """
    # --- config (fatal on failure) ------------------------------------- #
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.critical("fatal: could not load config: %s", exc)
        return 2

    # --- state store + dry-run temp seeding ---------------------------- #
    tmp_handle: object = None
    try:
        active_store: StoreLike
        if store is None:
            concrete_store, tmp_handle = _build_state_store(config, dry_run=dry_run)
            active_store = concrete_store
        else:
            active_store = store
        # Startup state-probe: eagerly surface a corrupt state file BEFORE we
        # start running monitors, so a bad on-disk file is a clean fatal exit(2)
        # rather than N identical per-monitor failures. Wrapped broadly: ANY
        # exception here is fatal.
        try:
            active_store.load_last_run()
            active_store.load_seen_appearances()
        except Exception as exc:  # noqa: BLE001 -- any probe failure is fatal
            logger.critical("fatal: state probe failed (corrupt state?): %s", exc)
            return 2

        # --- collaborators --------------------------------------------- #
        if dispatcher is None:
            dispatcher = _build_dispatcher(config, dry_run=dry_run)
        if bridge is None:
            bridge = _build_bridge()

        # --- monitor specs --------------------------------------------- #
        if monitors is None:
            clients = build_clients()
            # build_monitor_specs binds the CONCRETE StateStore; when a fake
            # store is injected for tests, callers pass `monitors=` too.
            assert isinstance(active_store, StateStore)
            monitors = build_monitor_specs(config, active_store, clients, now)

        # --- bridge gate: probe PAT presence ONCE per run -------------- #
        bridge_enabled = config.dispatch_bridge.enabled and not dry_run
        pat_present = False
        if bridge_enabled:
            try:
                pat_present = bridge.pat_present()
            except Exception as exc:  # noqa: BLE001 -- probe must not crash
                logger.warning("bridge pat_present() probe failed: %s", exc)
                pat_present = False
            if not pat_present:
                logger.info(
                    "dispatch_bridge enabled but DISPATCH_GITHUB_PAT absent; "
                    "skipping all repository_dispatch fires this run"
                )
        gate = _BridgeGate(enabled=bridge_enabled, pat_present=pat_present)

        # --- run each monitor in isolation ----------------------------- #
        intervals = config.monitor_intervals
        alert_failures: list[str] = []
        for spec in monitors:
            _process_monitor(
                spec,
                config,
                active_store,
                dispatcher,
                bridge,
                gate,
                intervals,
                now,
                alert_failures,
                dry_run=dry_run,
            )

        logger.info(
            "%s run complete (dry_run=%s, alert_failures=%d)",
            constants.LOG_RUN_SUMMARY_PREFIX,
            dry_run,
            len(alert_failures),
        )

        # Fail LOUD. Raised only after every monitor has had its turn, so
        # per-monitor isolation is preserved, but the process still exits
        # non-zero -- which is what makes GitHub Actions' own failure
        # notification a backstop for a silent alerting outage. The workflow
        # commits state on `!cancelled()`, so events that DID deliver keep their
        # dedupe state and are not re-alerted by the next run.
        if alert_failures:
            sample = alert_failures[: constants.ALERT_FAILURE_SAMPLE_MAX]
            more = len(alert_failures) - len(sample)
            suffix = f" (+{more} more)" if more > 0 else ""
            raise AlertDeliveryError(
                f"{len(alert_failures)} alert(s) failed to deliver: "
                + " | ".join(sample)
                + suffix
            )
        return 0
    finally:
        # Clean up the dry-run temp state dir (if any). TemporaryDirectory.cleanup
        # is idempotent + best-effort.
        if isinstance(tmp_handle, tempfile.TemporaryDirectory):
            try:
                tmp_handle.cleanup()
            except Exception:  # noqa: BLE001 -- cleanup is best-effort
                pass


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI surface. No args => one normal monitoring pass (the cron's behaviour)."""
    parser = argparse.ArgumentParser(
        prog="fomo-fund-monitor",
        description=(
            "Run one monitoring pass, or replay alerts for past events with "
            "--replay-since."
        ),
    )
    parser.add_argument(
        "--replay-since",
        metavar="YYYY-MM-DD",
        help=(
            "Re-emit alerts for events published on or after this date, through "
            "the real send path. Does NOT mutate state or dedupe, so it is safe "
            "to run repeatedly. Defaults to the edgar monitor only."
        ),
    )
    parser.add_argument(
        "--monitor",
        action="append",
        dest="monitors",
        metavar="NAME",
        choices=sorted(m.value for m in MonitorName),
        help=(
            "Replay only this monitor; repeatable. Default: edgar. The "
            "high-volume sources (google_news, youtube, ...) replay ONLY when "
            "named explicitly. Ignored without --replay-since."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=(
            "Cap a replay at the N most recent matching events (still sent "
            "oldest-first). Default: no limit. Ignored without --replay-since."
        ),
    )
    parser.add_argument(
        "--backfill-seeds",
        action="store_true",
        help=(
            "One-shot maintenance: silently seed archival-feed entries published "
            "BEFORE their feed's seed date that the original seed missed (a feed "
            "can serve a truncated window at seed time and its full archive "
            "later, making old episodes look new). Sends no alerts. Idempotent."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List what would be sent/seeded without sending or writing anything. "
            "Applies to a replay, a backfill, or a normal pass."
        ),
    )
    return parser


def _parse_since(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(
            f"--replay-since must be YYYY-MM-DD, got {raw!r}"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.backfill_seeds:
        if args.replay_since is not None:
            raise SystemExit("--backfill-seeds and --replay-since are exclusive")
        # Deferred: a normal cron pass must not pay for the backfill module.
        from backfill import backfill_seeds

        backfill_seeds(dry_run=args.dry_run)
        return 0

    if args.replay_since is None:
        if args.monitors or args.limit is not None:
            logger.warning(
                "--monitor / --limit apply to --replay-since only; ignoring "
                "them for this monitoring pass"
            )
        return run(now, dry_run=args.dry_run)

    # Deferred import: a normal cron pass must not pay for the replay module.
    from replay import DEFAULT_REPLAY_MONITORS, replay

    since = _parse_since(args.replay_since)
    monitors = (
        tuple(MonitorName(name) for name in args.monitors)
        if args.monitors
        else DEFAULT_REPLAY_MONITORS
    )
    try:
        replay(
            since,
            now,
            monitors=monitors,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except ValueError as exc:  # unsupported monitor / bad --limit
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    sys.exit(main())
