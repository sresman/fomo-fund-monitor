from __future__ import annotations

"""Re-emit alerts for events newer than a given date, without touching state.

Why this exists: alerting was broken from the system's first run, so events were
detected, failed to alert, and were left un-committed. ``--replay-since`` sends
them once the transport is fixed.

HOW IT SOURCES EVENTS (important -- read before changing)
--------------------------------------------------------
Neither ``seen_filings.json`` nor ``seen_appearances.json`` stores a timestamp,
a title or a URL. ``seen_filings`` is ``{entity: [accession]}`` and
``seen_appearances`` is bare id lists, so "state entries newer than DATE" is not
answerable from state, and an alert cannot be reconstructed from an id alone.

Worse, the events actually worth replaying are precisely the ones NOT in state:
the orchestrator commits dedupe state only after a successful dispatch, so every
event that failed to alert was never recorded.

Replay therefore re-runs each monitor against its LIVE source through the real
production code path, with two adjustments:

  * The store is a throwaway ``TemporaryDirectory`` copy whose dedupe buckets are
    EMPTIED but whose keys and seed markers are KEPT. Empty buckets make every
    current item "new" (so already-alerted events are re-emitted); kept keys and
    markers keep every monitor out of its first-run seeding path, which would
    otherwise suppress everything and emit nothing.
  * Events are then filtered to ``published >= since``.

The real ``state/`` directory is never opened for writing. Replay never commits
dedupe state and never fires the ``repository_dispatch`` bridge, so it is safe to
run repeatedly -- repeated runs re-send, which is the point, and no run can
corrupt dedupe or lose an event.

DEFAULTS
--------
EDGAR only. The high-volume, low-signal sources (``google_news``, ``youtube``,
...) replay only when named explicitly with ``--monitor``: a single un-narrowed
``google_news`` replay is ~115 emails.

The hash-diff monitors (``conference_pages``, ``website_diff`` page-hash events)
stamp ``published = now``, so a date filter cannot distinguish them; they are
excluded from replay entirely rather than matching everything.
"""

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import constants
from config import AppConfig, load_config
from errors import AlertDeliveryError
from models import DetectedEvent, MonitorName
from state_manager import SeenAppearances, StateStore

if TYPE_CHECKING:
    # Typing-only: the concrete builders are imported lazily inside replay() so
    # importing this module pulls no network stack.
    from main import Clients, DispatcherLike

logger = logging.getLogger("fomo_monitor.replay")

# Monitors whose events carry a real source timestamp, so a date filter means
# something. conference_pages / website_diff stamp published=now (a content-hash
# change has no source date) and are deliberately absent.
REPLAYABLE_MONITORS: frozenset[MonitorName] = frozenset(
    {
        MonitorName.EDGAR,
        MonitorName.YOUTUBE,
        MonitorName.PODCAST_RSS,
        MonitorName.GOOGLE_NEWS,
        MonitorName.CNBC,
    }
)

# Replayed unless --monitor narrows it. EDGAR only: it is the high-signal source,
# and an un-narrowed google_news replay is ~115 emails in one batch.
DEFAULT_REPLAY_MONITORS: tuple[MonitorName, ...] = (MonitorName.EDGAR,)


@dataclass
class ReplayReport:
    """Outcome of one replay pass. Returned for logging and for tests."""

    since: date
    monitors: tuple[MonitorName, ...]
    considered: int = 0  # events the monitors produced
    undated: int = 0  # dropped: no `published`, so not date-filterable
    matched: int = 0  # events on/after `since`
    dispatched: int = 0  # events actually handed to the dispatcher
    delivered: int = 0  # alerts that reached at least one channel
    failures: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        return (
            f"replay since {self.since.isoformat()} "
            f"[{', '.join(m.value for m in self.monitors)}]: "
            f"{self.considered} considered, {self.undated} undated, "
            f"{self.matched} matched, {self.dispatched} dispatched, "
            f"{self.delivered} delivered, {len(self.failures)} failed"
            + (" (DRY RUN -- nothing sent)" if self.dry_run else "")
        )


def _replay_state_dir(config: AppConfig, root: Path) -> StateStore:
    """Build a throwaway store: real keys + markers, EMPTY dedupe buckets.

    Emptying the buckets is what makes already-alerted events replayable. Keeping
    the keys and markers is what keeps each monitor out of its first-run seeding
    branch, which returns zero events by design. ``conference_hashes`` are copied
    verbatim so the hash-diff monitors do not re-seed pages if one is ever added
    to ``REPLAYABLE_MONITORS``.
    """
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    replay_store = StateStore(state_dir)

    real = StateStore(config.paths.state_dir)
    real_filings = real.load_seen_filings()
    real_appearances = real.load_seen_appearances()

    # Every configured entity gets a present-but-empty list: present => not
    # first-run, empty => every filing reads as new.
    filings: dict[str, list[str]] = {entity.key: [] for entity in config.entities}
    for key in real_filings:
        filings[key] = []
    replay_store.save_seen_filings(filings)

    replay_store.save_seen_appearances(
        SeenAppearances(
            youtube=[],
            rss_guids=[],
            urls=[],
            conference_hashes=dict(real_appearances.conference_hashes),
            markers=dict(real_appearances.markers),
        )
    )
    return replay_store


@dataclass(frozen=True)
class Selection:
    """Result of date-filtering a batch of candidate events."""

    selected: tuple[DetectedEvent, ...]  # to send, OLDEST FIRST
    matched: int  # on/after `since` BEFORE `limit` was applied
    undated: int  # dropped: no `published`, so not date-filterable


def select_events(
    events: Sequence[DetectedEvent], since: date, limit: int | None
) -> Selection:
    """Filter to ``published >= since``, apply ``limit``, order for sending.

    When ``limit`` is set the MOST RECENT ``limit`` events are kept -- a
    truncated replay should surface the newest news, not the oldest -- but the
    returned tuple is always ordered OLDEST FIRST so the resulting emails arrive
    in chronological order. ``identifier`` is the tie-break, so events sharing a
    timestamp still order deterministically.
    """
    undated = sum(1 for e in events if e.published is None)
    # Build (sort key, event) triples inside the comprehension so `published` is
    # statically known to be non-None -- no assert, no cast.
    dated: list[tuple[datetime, str, DetectedEvent]] = [
        (e.published, e.identifier, e)
        for e in events
        if e.published is not None and e.published.date() >= since
    ]
    dated.sort(key=lambda t: (t[0], t[1]))
    matched = len(dated)
    if limit is not None and matched > limit:
        dated = dated[-limit:]  # newest `limit`, still oldest-first
    return Selection(
        selected=tuple(t[2] for t in dated), matched=matched, undated=undated
    )


def replay(
    since: date,
    now: datetime,
    *,
    monitors: tuple[MonitorName, ...] = DEFAULT_REPLAY_MONITORS,
    limit: int | None = None,
    dry_run: bool = False,
    config_path: str | Path | None = None,
    clients: "Clients | None" = None,
    dispatcher: "DispatcherLike | None" = None,
) -> ReplayReport:
    """Re-emit alerts for events on or after ``since`` through the real send path.

    Raises ``ValueError`` for a monitor that cannot be replayed, and
    ``AlertDeliveryError`` if any alert fails to deliver (same fail-loud contract
    as a normal run). Never writes to the real state directory.

    ``clients`` / ``dispatcher`` are injectable for testing ONLY; production
    leaves both ``None`` so the real transport clients and the real
    Gmail/Twilio senders are built here.
    """
    # Deferred so importing this module pulls no network stack (mirrors main.py).
    from main import build_clients, build_monitor_specs

    unsupported = [m for m in monitors if m not in REPLAYABLE_MONITORS]
    if unsupported:
        raise ValueError(
            "cannot replay "
            + ", ".join(m.value for m in unsupported)
            + " (events carry no source timestamp to filter on); replayable: "
            + ", ".join(sorted(m.value for m in REPLAYABLE_MONITORS))
        )
    if limit is not None and limit < 1:
        raise ValueError(f"--limit must be >= 1, got {limit}")

    config = load_config(config_path)
    report = ReplayReport(since=since, monitors=monitors, dry_run=dry_run)
    wanted = set(monitors)

    with tempfile.TemporaryDirectory(prefix="fomo-replay-state-") as tmp:
        store = _replay_state_dir(config, Path(tmp))
        active_clients = clients if clients is not None else build_clients()
        specs = [
            s for s in build_monitor_specs(config, store, active_clients, now)
            if s.name in wanted
        ]

        collected: list[DetectedEvent] = []
        for spec in specs:
            try:
                produced = spec.run_check()
            except Exception as exc:  # noqa: BLE001 -- one source never aborts the rest
                logger.error("replay: monitor %s failed: %s", spec.name.value, exc)
                report.failures.append(f"{spec.name.value}: source fetch failed: {exc}")
                continue
            logger.info(
                "replay: monitor %s produced %d candidate event(s)",
                spec.name.value,
                len(produced),
            )
            collected.extend(produced)

        report.considered = len(collected)
        selection = select_events(collected, since, limit)
        selected = selection.selected
        report.matched = selection.matched
        report.undated = selection.undated

        if dry_run:
            for event in selected:
                logger.info(
                    "replay (DRY RUN) would send: %s | %s | %s",
                    event.published.date().isoformat() if event.published else "?",
                    event.identifier,
                    event.title,
                )
            logger.info("%s", report.summary())
            return report

        _dispatch(selected, config, report, dispatcher)

    logger.info("%s", report.summary())
    if report.failures:
        sample = report.failures[: constants.ALERT_FAILURE_SAMPLE_MAX]
        more = len(report.failures) - len(sample)
        raise AlertDeliveryError(
            f"replay: {len(report.failures)} alert(s) failed to deliver: "
            + " | ".join(sample)
            + (f" (+{more} more)" if more > 0 else "")
        )
    return report


def _dispatch(
    events: Sequence[DetectedEvent],
    config: AppConfig,
    report: ReplayReport,
    dispatcher: "DispatcherLike | None",
) -> None:
    """Send through the REAL production dispatcher (real senders, real env)."""
    from main import alert_delivered, describe_alert_failure

    if dispatcher is None:
        from alerting.dispatch import Dispatcher
        from alerting.email_alert import GmailSender
        from alerting.sms_alert import TwilioSender

        dispatcher = Dispatcher(GmailSender(), TwilioSender(), dry_run=False)
    results = dispatcher.dispatch_events(events, config)
    report.dispatched = len(events)
    for event, result in zip(events, results):
        if alert_delivered(result):
            report.delivered += 1
            logger.info("replay: delivered %r", event.identifier)
        else:
            reason = describe_alert_failure(result)
            logger.error("replay: FAILED %r -- %s", event.identifier, reason)
            report.failures.append(f"{event.identifier}: {reason}")
