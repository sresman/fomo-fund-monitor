from __future__ import annotations

"""Tests for the Google News monitor (monitors/google_news.py).

A typed fake ``FeedClient`` by URL. No network.
"""

import urllib.parse
from datetime import datetime, timezone

import pytest

from alerting.formatting import build_alert
from config import AppConfig
from constants import GOOGLE_NEWS_RSS_URL
from errors import MonitorError
from models import AlertChannel, Confidence, EventType, Priority
from monitors._common import news_seed_key
from monitors.google_news import check_google_news
from state_manager import SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TODAY_ISO = "2026-07-22"

Q_BAKER = '"Gavin Baker" interview'
Q_LEO = '"Leopold Aschenbrenner" AGI'


def _url_for(query: str) -> str:
    return GOOGLE_NEWS_RSS_URL.format(query=urllib.parse.quote_plus(query))


BAKER_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Baker - Google News</title>
<item>
  <title>Gavin Baker on AI - Bloomberg</title>
  <link>https://news.google.com/redirect-1</link>
  <guid>gn-guid-1</guid>
  <pubDate>Tue, 22 Jul 2025 14:00:00 GMT</pubDate>
  <source url="https://bloomberg.com">Bloomberg</source>
</item>
</channel></rss>"""

NO_SOURCE_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed Title Here</title>
<item>
  <title>Headline no source tag</title>
  <link>https://news.google.com/redirect-2</link>
  <guid>gn-guid-2</guid>
  <pubDate>Tue, 22 Jul 2025 14:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class FakeFeedClient:
    def __init__(
        self, by_url: dict[str, bytes], raise_for: frozenset[str] = frozenset()
    ) -> None:
        self._by_url = by_url
        self._raise_for = raise_for
        self.fetched: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        if url in self._raise_for:
            raise MonitorError(f"boom {url}")
        return self._by_url.get(url, b"<rss></rss>")


def _seed_all(store: StateStore, config: AppConfig) -> None:
    markers = {news_seed_key(q): TODAY_ISO for q in config.google_news.queries}
    store.save_seen_appearances(SeenAppearances(markers=markers))


# --------------------------------------------------------------------------- #


def test_now_naive_raises(feeds_config: AppConfig, store: StateStore) -> None:
    client = FakeFeedClient({})
    with pytest.raises(ValueError):
        check_google_news(feeds_config, store, client, datetime(2026, 7, 22))


def test_url_construction() -> None:
    assert "quote" not in Q_BAKER  # sanity
    built = _url_for(Q_BAKER)
    assert urllib.parse.quote_plus(Q_BAKER) in built


def test_fake_called_with_encoded_url(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient({_url_for(Q_BAKER): BAKER_FEED})
    check_google_news(feeds_config, store, client, NOW)
    assert _url_for(Q_BAKER) in client.fetched


def test_emits_and_fields(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient({_url_for(Q_BAKER): BAKER_FEED})
    events = check_google_news(feeds_config, store, client, NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.GOOGLE_NEWS
    assert ev.priority == Priority.MEDIUM
    assert ev.confidence == Confidence.MEDIUM
    assert ev.identifier == "gn-guid-1"
    assert ev.url == "https://news.google.com/redirect-1"
    assert ev.source == "Bloomberg"
    assert ev.entity_key == ""
    assert set(ev.payload.keys()) == {"query"}
    assert ev.payload["query"] == Q_BAKER
    assert ev.published is not None


def test_guid_first_dedupe(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    app = store.load_seen_appearances()
    app.urls.append("gn-guid-1")  # guid already seen
    store.save_seen_appearances(app)
    # Link CHANGED but guid same -> skipped (guid-first).
    changed_link = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>x</title>
<item><title>t</title><link>https://news.google.com/DIFFERENT</link>
<guid>gn-guid-1</guid><pubDate>Tue, 22 Jul 2025 14:00:00 GMT</pubDate></item>
</channel></rss>"""
    client = FakeFeedClient({_url_for(Q_BAKER): changed_link})
    events = check_google_news(feeds_config, store, client, NOW)
    assert events == []


def test_link_fallback_when_no_guid(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    no_guid = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>x</title>
<item><title>t</title><link>https://news.google.com/only-link</link>
<pubDate>Tue, 22 Jul 2025 14:00:00 GMT</pubDate></item>
</channel></rss>"""
    client = FakeFeedClient({_url_for(Q_BAKER): no_guid})
    events = check_google_news(feeds_config, store, client, NOW)
    assert [e.identifier for e in events] == ["https://news.google.com/only-link"]


def test_source_title_fallbacks(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient({_url_for(Q_BAKER): NO_SOURCE_FEED})
    events = check_google_news(feeds_config, store, client, NOW)
    # No entry <source> -> falls back to feed <title>.
    assert events[0].source == "Feed Title Here"


def test_in_run_dedupe_same_id_two_queries(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {_url_for(Q_BAKER): BAKER_FEED, _url_for(Q_LEO): BAKER_FEED}
    )
    events = check_google_news(feeds_config, store, client, NOW)
    assert [e.identifier for e in events] == ["gn-guid-1"]


def test_per_query_first_run_seeds_silently(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient({_url_for(Q_BAKER): BAKER_FEED})
    events = check_google_news(feeds_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert "gn-guid-1" in reloaded.urls
    assert reloaded.markers[news_seed_key(Q_BAKER)] == TODAY_ISO


def test_empty_first_run_sets_marker_then_emits(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient({_url_for(Q_BAKER): b"<rss></rss>"})
    check_google_news(feeds_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert reloaded.markers[news_seed_key(Q_BAKER)] == TODAY_ISO

    client2 = FakeFeedClient({_url_for(Q_BAKER): BAKER_FEED})
    events = check_google_news(feeds_config, store, client2, NOW)
    assert [e.identifier for e in events] == ["gn-guid-1"]


def test_fetch_failure_leaves_query_first_run(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient({}, raise_for=frozenset({_url_for(Q_BAKER)}))
    check_google_news(feeds_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert news_seed_key(Q_BAKER) not in reloaded.markers
    assert news_seed_key(Q_LEO) in reloaded.markers  # other query seeded


def test_per_query_isolation(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {_url_for(Q_LEO): BAKER_FEED}, raise_for=frozenset({_url_for(Q_BAKER)})
    )
    events = check_google_news(feeds_config, store, client, NOW)
    # Baker query raised, leo query still processed.
    assert [e.identifier for e in events] == ["gn-guid-1"]


def test_batched_write_zero_events(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient({_url_for(Q_BAKER): b"<rss></rss>"})
    events = check_google_news(feeds_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert reloaded.markers


def test_build_alert_google_news(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient({_url_for(Q_BAKER): BAKER_FEED})
    ev = check_google_news(feeds_config, store, client, NOW)[0]
    alert = build_alert(ev, feeds_config)
    assert alert.subject
    assert alert.body
    assert set(alert.channels) == {AlertChannel.EMAIL}
