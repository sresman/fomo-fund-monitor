from __future__ import annotations

"""Google News RSS monitor (Monitor: google_news, Prompt 4).

Builds a Google News RSS search URL per configured query, fetches + parses it,
dedupes by guid-first (link fallback) in the shared ``urls`` bucket, seeds
first-run backlog silently PER QUERY, and returns a ``list[DetectedEvent]``.
Reuses the feed stack from ``_common``. Does NOT send alerts (orchestrator,
Prompt 6).

``urls`` bucket semantics (SD-P4-7): the ``urls`` bucket holds "web-appearance
identifiers (URL or GUID)" and is SHARED by google_news + (Prompt 5) cnbc.
google_news dedupes by ``entry.guid`` first, fallback ``entry.link``; both are
long opaque strings, so a guid<->url collision is negligible.

Contract (Option B): writes state ONLY for per-query first-run seeds. It NEVER
marks normal new items seen -- the orchestrator calls
``store.mark_appearance_seen("urls", ev.identifier)`` after a successful
dispatch. The NORMAL branch writes nothing.

Priority is informational; routing is resolved from EventType via
``config.alert_routing`` (GOOGLE_NEWS -> email only).
"""

import logging
import urllib.parse
from datetime import datetime, timezone

from config import AppConfig
from constants import GOOGLE_NEWS_RSS_URL
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._common import (
    FeedClient,
    merge_appearances,
    news_seed_key,
    parse_feed,
)
from state_manager import StateStore

_log = logging.getLogger(__name__)


def check_google_news(
    config: AppConfig,
    store: StateStore,
    feed_client: FeedClient,
    now: datetime,
) -> list[DetectedEvent]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    seen = store.load_seen_appearances()  # fatal on StateError
    already_seen = set(seen.urls)
    markers = seen.markers
    today_utc = now.astimezone(timezone.utc).date().isoformat()

    handled: set[str] = set()
    events: list[DetectedEvent] = []
    pending_bucket_seeds: list[str] = []
    new_markers: dict[str, str] = {}

    for query in config.google_news.queries:
        seed_key = news_seed_key(query)
        first_run = seed_key not in markers
        url = GOOGLE_NEWS_RSS_URL.format(query=urllib.parse.quote_plus(query))
        try:
            content = feed_client.fetch(url)
            entries = parse_feed(content)
        except Exception:  # noqa: BLE001 -- per-query isolation
            _log.exception(
                "google_news: query %r failed; skipping (stays first-run if "
                "not yet seeded)",
                query,
            )
            continue

        query_seeds: list[str] = []
        for entry in entries:
            # Relevance is inherent in the query -- no extra keyword filtering.
            identifier = entry.guid or entry.link  # guid-first (redirect links change)
            if identifier == "":
                continue
            if identifier in handled:
                continue
            if first_run:
                query_seeds.append(identifier)
                handled.add(identifier)
                continue
            if identifier in already_seen:
                continue
            events.append(
                DetectedEvent(
                    event_type=EventType.GOOGLE_NEWS,
                    entity_key="",  # news queries span both people
                    source=entry.source_title,
                    title=entry.title,
                    url=entry.link,
                    identifier=identifier,
                    published=entry.published,
                    priority=Priority.MEDIUM,
                    confidence=Confidence.MEDIUM,
                    payload={"query": query},
                )
            )
            handled.add(identifier)

        if first_run:
            for ident in query_seeds:
                if ident not in pending_bucket_seeds:
                    pending_bucket_seeds.append(ident)
            new_markers[seed_key] = today_utc  # even if zero found

    if pending_bucket_seeds or new_markers:
        try:
            fresh = store.load_seen_appearances()
            merged = merge_appearances(
                fresh, "urls", pending_bucket_seeds, new_markers
            )
            store.save_seen_appearances(merged)
        except Exception:  # noqa: BLE001 -- non-fatal; re-seeds next run
            _log.exception(
                "google_news: failed to persist first-run seeds; will retry "
                "next run (no data loss)"
            )

    return events
