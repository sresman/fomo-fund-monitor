from __future__ import annotations

"""Podcast RSS monitor (Monitor: podcast_rss, Prompt 4).

Fetches each configured podcast feed, keyword-matches entries per-field, dedupes
against the ``rss_guids`` bucket, seeds first-run backlog silently PER FEED URL,
and returns a ``list[DetectedEvent]``. Reuses the feed stack from ``_common``.
Does NOT send alerts (orchestrator, Prompt 6).

Contract (Option B): writes state ONLY for per-feed first-run seeds (bucket seeds
+ the feed's seed marker). It NEVER marks normal new episodes seen -- the
orchestrator calls ``store.mark_appearance_seen("rss_guids", ev.identifier)``
after a successful dispatch. The NORMAL (already-seeded) branch writes nothing.

Priority is informational; routing is resolved from EventType via
``config.alert_routing`` (PODCAST_RSS -> email+sms).
"""

import logging
from datetime import datetime, timezone

from config import AppConfig
from constants import FEED_DESCRIPTION_EXCERPT_MAX
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._outcome import UnitTally
from monitors._common import (
    FeedClient,
    excerpt,
    is_first_party_appearance,
    merge_appearances,
    parse_feed,
    podcast_seed_key,
)
from state_manager import StateStore

_log = logging.getLogger(__name__)


def _map_person(config: AppConfig, keywords: tuple[str, ...]) -> tuple[str, str]:
    """Best-effort DISPLAY label: (entity_key, person) or ("", "").

    First ``config.entities`` entry ``e`` such that any of ``keywords``
    (lowercased) is a substring of ``e.person.lower()`` OR ``e.name.lower()``.
    (``e.key`` is a slug and intentionally not matched.) Affects ONLY the shown
    person name -- not routing (EventType-driven) nor dedupe (guid-driven).
    """
    kws = [k.lower() for k in keywords if k.strip() != ""]
    for e in config.entities:
        person_lower = e.person.lower()
        name_lower = e.name.lower()
        for kw in kws:
            if kw in person_lower or kw in name_lower:
                return e.key, e.person
    return "", ""


def check_podcast_rss(
    config: AppConfig,
    store: StateStore,
    feed_client: FeedClient,
    now: datetime,
) -> list[DetectedEvent]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    seen = store.load_seen_appearances()  # fatal on StateError
    already_seen = set(seen.rss_guids)
    markers = seen.markers
    today_utc = now.astimezone(timezone.utc).date().isoformat()

    handled: set[str] = set()
    events: list[DetectedEvent] = []
    pending_bucket_seeds: list[str] = []
    new_markers: dict[str, str] = {}
    tally = UnitTally("podcast_rss")

    for feed in config.podcast_rss.feeds:
        if feed.url.strip() == "":
            _log.debug("podcast_rss: skipping empty-url feed %s", feed.show)
            continue  # empty-url feeds get NO seed key
        seed_key = podcast_seed_key(feed.url)
        first_run = seed_key not in markers
        try:
            content = feed_client.fetch(feed.url)
            entries = parse_feed(content)
        except Exception:  # noqa: BLE001 -- per-feed isolation
            tally.record_failure()
            _log.exception(
                "podcast_rss: feed %s (%s) failed; skipping (stays first-run "
                "if not yet seeded)",
                feed.show,
                feed.url,
            )
            continue
        tally.record_success()

        feed_seeds: list[str] = []
        entity_key, person = _map_person(config, feed.keywords)
        for entry in entries:
            # First-party gate: the person APPEARING, not merely referenced in
            # show notes. See monitors/_common.is_first_party_appearance.
            if not is_first_party_appearance(
                entry.title, entry.summary, feed.keywords
            ):
                continue
            identifier = entry.guid
            if identifier == "":
                continue
            if identifier in handled:
                continue
            if first_run:
                feed_seeds.append(identifier)
                handled.add(identifier)
                continue
            if identifier in already_seen:
                continue
            events.append(
                DetectedEvent(
                    event_type=EventType.PODCAST_RSS,
                    entity_key=entity_key,
                    source=feed.show,  # config label, not FeedEntry.source_title
                    title=entry.title,
                    url=entry.link,
                    identifier=identifier,
                    published=entry.published,
                    priority=Priority.HIGH,
                    confidence=Confidence.HIGH,
                    payload={
                        "person": person,
                        "audio_url": entry.enclosure_url,
                        "description": excerpt(
                            entry.summary, FEED_DESCRIPTION_EXCERPT_MAX
                        ),
                    },
                )
            )
            handled.add(identifier)

        if first_run:
            for guid in feed_seeds:
                if guid not in pending_bucket_seeds:
                    pending_bucket_seeds.append(guid)
            new_markers[seed_key] = today_utc  # even if zero matched

    if pending_bucket_seeds or new_markers:
        try:
            fresh = store.load_seen_appearances()
            merged = merge_appearances(
                fresh, "rss_guids", pending_bucket_seeds, new_markers
            )
            store.save_seen_appearances(merged)
        except Exception:  # noqa: BLE001 -- non-fatal; re-seeds next run
            _log.exception(
                "podcast_rss: failed to persist first-run seeds; will retry "
                "next run (no data loss)"
            )

    # Every feed dead => this run observed nothing; do not let last_run advance.
    tally.raise_if_total_failure()
    return events
