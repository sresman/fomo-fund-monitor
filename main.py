from __future__ import annotations

"""Orchestrator entrypoint for fomo-fund-monitor (Prompt 6).

``run(now, ...)`` drives one monitoring pass: build config + clients + monitor
specs, interval-gate each monitor, run it in isolation, dispatch alerts, commit
per-event dedupe state ONLY after a successful alert dispatch, then fire the
optional ``repository_dispatch`` bridge for each committed event. ``main()`` is a
thin shell that wires the real clock + logging and translates the return code
into a process exit code.

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
  * NO import-time side effects. ``logging.basicConfig`` runs INSIDE ``main()``.
  * Exit 0 for a normal run (even with per-monitor failures); exit 2 ONLY for a
    fatal config load or startup state-probe failure.
  * ``dry_run=True`` short-circuits ALL commits + bridge unconditionally and
    runs monitors against a throwaway temp copy of state so the real ``state/``
    is never touched.

Heavy transport deps (``GmailSender`` / ``TwilioSender``, the concrete HTTP
clients) are imported lazily inside ``run()`` so importing this module pulls no
network stack (preserves ``test_main_importable_no_side_effects``).
"""

import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
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
from errors import ConfigError, DispatchBridgeAuthError, DispatchBridgeError, StateError
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
    3. At least one channel actually delivered. If EVERY routed channel was
       skipped, nothing was sent; committing would mark the event seen without
       anyone ever having been told, losing the alert permanently. That case is
       an alerting-layer outage, not a per-event condition, and is deliberately
       treated as NOT delivered so the event re-fires once alerting is fixed.
    """
    if result.event_error is not None:
        return False
    if result.errors:
        return False
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
    *,
    dry_run: bool,
) -> None:
    """Run a single monitor end-to-end in isolation.

    ``should_run`` is INSIDE the try/except so a corrupt ``last_run`` timestamp
    (raising ``StateError``) skips ONLY this monitor. ``record_run`` is in the
    ``finally`` so a monitor that actually ran records its timestamp even if its
    body raised mid-way -- EXCEPT when ``should_run`` itself failed (we never
    reached the run) or short-circuited (not due yet).
    """
    name = spec.name.value
    ran = False
    try:
        if not store.should_run(name, now, intervals):
            logger.debug("monitor %s not due; skipping", name)
            return
        ran = True
        events = spec.run_check()
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
                    "monitor %s: alert failed for %r; leaving un-committed to "
                    "retry next run",
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
        if ran and not dry_run:
            try:
                store.record_run(name, now)
            except Exception as exc:  # noqa: BLE001 -- record fault isolated
                logger.error("monitor %s: failed to record_run: %s", name, exc)


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


def _build_clients() -> Clients:
    """Construct the concrete transport clients via DEFERRED imports so importing
    ``main`` pulls no network stack."""
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
            clients = _build_clients()
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
                dry_run=dry_run,
            )

        logger.info(
            "%s run complete (dry_run=%s)",
            constants.LOG_RUN_SUMMARY_PREFIX,
            dry_run,
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    now = datetime.now(timezone.utc)
    return run(now)


if __name__ == "__main__":
    sys.exit(main())
