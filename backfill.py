from __future__ import annotations

"""One-shot maintenance: seed archival feed entries the original seed missed.

WHY THIS IS NEEDED. Each feed monitor seeds itself on first contact: it fetches
the feed, records the guids of every KEYWORD-MATCHING entry it can see, writes a
seed marker, and emits nothing. Everything visible at that moment is treated as
"already known".

The flaw is that a feed's visible WINDOW is not stable. Libsyn and Substack feeds
in particular serve a truncated window sometimes and the full archive at others.
Anything that was outside the window at seed time but appears later is, to the
monitor, indistinguishable from a brand-new episode -- so it alerts, years after
publication. Observed on 2026-09-03: three This Week in Startups episodes from
2022, 2023 and 2024, plus a June 2024 Dwarkesh episode, all alerted as new.

THE RULE. For each feed that HAS a seed marker, any keyword-matching entry
published STRICTLY BEFORE that feed's seed date should already have been seeded,
so seed it now -- silently, with no alert. Entries on or after the seed date are
left alone: those are legitimately new and must still alert.

DELIBERATELY CONSERVATIVE, in both directions:
  * An entry whose ``published`` cannot be parsed is NEVER backfilled. Suppressing
    a real alert is worse than sending a duplicate, and an undated entry cannot be
    proven older than the seed.
  * An entry published ON the seed date is NOT backfilled either -- same-day
    ordering is ambiguous, and the same asymmetry applies.
  * A feed with NO seed marker is skipped entirely. It is genuinely first-run and
    the monitor's own seeding path will handle it correctly.

SCOPE: podcast_rss feeds only.

  * Search-based sources (youtube / google_news / cnbc) are NOT backfillable this
    way: their "back catalogue" is whatever the search engine returns today,
    which is not a stable archive, so there is no defensible older-than-seed
    boundary. Their seed markers are per-query and already behave correctly.
  * ``website_diff`` check_rss sites share the ``rss_guids`` bucket and have the
    same failure mode in principle, but are EXCLUDED on purpose. Their monitor
    (``_check_rss_site``) fetches ``<site.url>/feed`` rather than ``site.url``,
    restricts the RSS branch to sites that map to ``LEOPOLD_POST``, and gates on
    feedparser recognising a real feed before seeding at all. Reproducing those
    three rules here would mean maintaining a second copy of them, and getting
    any one wrong would silently seed -- i.e. permanently suppress -- the wrong
    entries. The one configured check_rss site has never successfully seeded
    anyway, so there is nothing to backfill. Revisit only by refactoring the
    monitor to expose its feed-URL and gating logic.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from config import AppConfig, load_config
from monitors._common import (
    matches_keywords,
    merge_appearances,
    parse_feed,
    podcast_seed_key,
)
from state_manager import StateStore

if TYPE_CHECKING:
    from monitors._common import FeedClient

logger = logging.getLogger("fomo_monitor.backfill")

# The dedupe bucket every archival RSS source shares.
_BUCKET: Literal["rss_guids"] = "rss_guids"


@dataclass(frozen=True)
class MissedEntry:
    """A keyword-matching entry older than its feed's seed date, yet unseeded."""

    source: str  # feed show name / website site key
    feed_url: str
    guid: str
    title: str
    published: datetime
    seed_date: date


@dataclass
class BackfillReport:
    scanned_feeds: int = 0
    skipped_unseeded: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)
    undated_skipped: int = 0
    missed: list[MissedEntry] = field(default_factory=list)
    seeded: int = 0
    dry_run: bool = False

    def summary(self) -> str:
        return (
            f"backfill: {self.scanned_feeds} feed(s) scanned, "
            f"{len(self.missed)} pre-seed entr(ies) missed, "
            f"{self.seeded} seeded, {self.undated_skipped} undated skipped, "
            f"{len(self.skipped_unseeded)} feed(s) not yet seeded, "
            f"{len(self.fetch_errors)} fetch error(s)"
            + (" (DRY RUN -- nothing written)" if self.dry_run else "")
        )


@dataclass(frozen=True)
class _Source:
    """One archival RSS source: where to fetch, what matches, which seed key."""

    label: str
    url: str
    keywords: tuple[str, ...]
    seed_key: str


def _sources(config: AppConfig) -> list[_Source]:
    """Every podcast feed that seeds into the ``rss_guids`` bucket.

    Empty-URL feeds get no seed key from the monitor, so they are skipped here
    exactly as ``check_podcast_rss`` skips them. See the module docstring for why
    ``website_diff`` check_rss sites are deliberately NOT included.
    """
    return [
        _Source(
            label=feed.show,
            url=feed.url,
            keywords=tuple(feed.keywords),
            seed_key=podcast_seed_key(feed.url),
        )
        for feed in config.podcast_rss.feeds
        if feed.url.strip() != ""
    ]


def _parse_seed_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def find_missed(
    config: AppConfig, store: StateStore, feed_client: "FeedClient"
) -> BackfillReport:
    """Scan every seeded archival source for pre-seed entries that were missed.

    Read-only: never writes state. ``backfill_seeds`` applies the result.
    """
    report = BackfillReport()
    seen = store.load_seen_appearances()
    already = set(seen.rss_guids)
    markers = seen.markers

    for source in _sources(config):
        raw_marker = markers.get(source.seed_key)
        if raw_marker is None:
            # Never seeded => genuinely first-run; the monitor will handle it.
            report.skipped_unseeded.append(source.label)
            continue
        seed_date = _parse_seed_date(raw_marker)
        if seed_date is None:
            report.fetch_errors.append(
                f"{source.label}: unparseable seed marker {raw_marker!r}"
            )
            continue

        report.scanned_feeds += 1
        try:
            entries = parse_feed(feed_client.fetch(source.url))
        except Exception as exc:  # noqa: BLE001 -- one dead feed never aborts the rest
            logger.error("backfill: %s (%s) failed: %s", source.label, source.url, exc)
            report.fetch_errors.append(f"{source.label}: {exc}")
            continue

        for entry in entries:
            if entry.guid == "" or entry.guid in already:
                continue
            if not matches_keywords((entry.title, entry.summary), source.keywords):
                continue
            if entry.published is None:
                # Cannot prove it predates the seed -> leave it to alert.
                report.undated_skipped += 1
                continue
            if entry.published.date() >= seed_date:
                continue  # legitimately new; must still alert
            report.missed.append(
                MissedEntry(
                    source=source.label,
                    feed_url=source.url,
                    guid=entry.guid,
                    title=entry.title,
                    published=entry.published,
                    seed_date=seed_date,
                )
            )
    return report


def backfill_seeds(
    *,
    dry_run: bool = False,
    config_path: str | Path | None = None,
    store: StateStore | None = None,
    feed_client: "FeedClient | None" = None,
) -> BackfillReport:
    """Find and (unless ``dry_run``) seed pre-seed entries the seed missed.

    Idempotent: a second run finds nothing, because the first run seeded them.
    Purely additive -- ``merge_appearances`` only appends to ``rss_guids``; no id
    is ever removed and no marker is changed.
    """
    config = load_config(config_path)
    active_store = store if store is not None else StateStore(config.paths.state_dir)
    if feed_client is None:
        from monitors._common import RequestsFeedClient

        feed_client = RequestsFeedClient()

    report = find_missed(config, active_store, feed_client)
    report.dry_run = dry_run

    for entry in sorted(report.missed, key=lambda m: (m.source, m.published)):
        logger.info(
            "backfill: %s %s | %s | %s | seed was %s",
            "WOULD SEED" if dry_run else "SEEDING",
            entry.published.date().isoformat(),
            entry.source,
            entry.title[:80],
            entry.seed_date.isoformat(),
        )

    if not dry_run and report.missed:
        fresh = active_store.load_seen_appearances()
        merged = merge_appearances(
            fresh, _BUCKET, [m.guid for m in report.missed], {}
        )
        active_store.save_seen_appearances(merged)
        report.seeded = len(report.missed)

    logger.info("%s", report.summary())
    return report
