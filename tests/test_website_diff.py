from __future__ import annotations

"""Tests for the website_diff monitor (monitors/website_diff.py).

ONE fake ``FeedClient`` by URL dispatches BOTH the page-hash fetch and the RSS
``/feed`` fetch. No network."""

from datetime import datetime, timezone

import pytest

from alerting.formatting import build_alert
from config import AppConfig
from constants import DIFF_SNIPPET_MAX
from errors import MonitorError
from models import AlertChannel, Confidence, EventType, Priority
from monitors._common import website_rss_seed_key, website_snapshot_key
from monitors._content_hash import content_hash, extract_normalized_text
from monitors.website_diff import _event_type_for, check_website_diff
from state_manager import ConferenceSnapshot, SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TODAY_ISO = "2026-07-22"

# sample_config website_diff:
#   gavinbaker_net (check_rss=false, keywords []) -> WEBSITE_DIFF
#   situational_awareness_com (check_rss=true, keywords ["AGI"]) -> LEOPOLD_POST
GB_URL = "https://gavinbaker.net"
GB_KEY = "gavinbaker_net"
SA_URL = "https://situational-awareness.com"
SA_KEY = "situational_awareness_com"
SA_FEED_URL = "https://situational-awareness.com/feed"


_PAD = " lorem ipsum dolor sit amet consectetur adipiscing"  # >50 chars per line


def _page(*paras: str) -> bytes:
    body = "".join(f"<p>{p}{_PAD}</p>" for p in paras)
    return f"<html><body>{body}</body></html>".encode("utf-8")


VALID_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Situational Awareness</title>
<item><title>Post One</title><link>https://situational-awareness.com/p/one</link>
<guid>sa-guid-1</guid><description>On AGI timelines and compute scaling.</description>
<pubDate>Tue, 22 Jul 2025 14:00:00 GMT</pubDate></item>
</channel></rss>"""

VALID_FEED_TWO = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Situational Awareness</title>
<item><title>Post Two</title><link>https://situational-awareness.com/p/two</link>
<guid>sa-guid-2</guid><description>More AGI content here.</description>
<pubDate>Wed, 23 Jul 2025 14:00:00 GMT</pubDate></item>
<item><title>Post One</title><link>https://situational-awareness.com/p/one</link>
<guid>sa-guid-1</guid><description>On AGI timelines.</description>
<pubDate>Tue, 22 Jul 2025 14:00:00 GMT</pubDate></item>
</channel></rss>"""

EMPTY_VALID_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Situational Awareness</title></channel></rss>"""

# Not a feed: HTML challenge page (feedparser yields empty version).
NOT_A_FEED = b"<html><body><p>Checking your browser please wait</p></body></html>"


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
        if url not in self._by_url:
            raise MonitorError(f"unexpected url {url}")
        return self._by_url[url]


def _seed_page_snapshot(store: StateStore, key: str, content: bytes) -> None:
    text = extract_normalized_text(content)
    app = store.load_seen_appearances()
    app.conference_hashes[website_snapshot_key(key)] = ConferenceSnapshot(
        content_hash(text), text
    )
    store.save_seen_appearances(app)


def _seed_rss(store: StateStore, guids: list[str]) -> None:
    app = store.load_seen_appearances()
    app.markers[website_rss_seed_key(SA_KEY)] = TODAY_ISO
    for g in guids:
        if g not in app.rss_guids:
            app.rss_guids.append(g)
    store.save_seen_appearances(app)


# --------------------------------------------------------------------------- #
# Mapping + signature
# --------------------------------------------------------------------------- #


def test_event_type_mapping() -> None:
    assert _event_type_for(SA_KEY) == EventType.LEOPOLD_POST
    assert _event_type_for(GB_KEY) == EventType.WEBSITE_DIFF


def test_now_naive_raises(scrape_config: AppConfig, store: StateStore) -> None:
    client = FakeFeedClient({})
    with pytest.raises(ValueError):
        check_website_diff(scrape_config, store, client, datetime(2026, 7, 22))


def test_single_feed_client_dispatches_both(
    scrape_config: AppConfig, store: StateStore
) -> None:
    # First run: page-hash seeds, RSS seeds -> both branches touched via one client.
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home page content padding"), SA_FEED_URL: VALID_FEED}
    )
    check_website_diff(scrape_config, store, client, NOW)
    assert GB_URL in client.fetched
    assert SA_FEED_URL in client.fetched


# --------------------------------------------------------------------------- #
# RSS branch (situational_awareness_com)
# --------------------------------------------------------------------------- #


def test_rss_feed_url_built(
    scrape_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: VALID_FEED}
    )
    check_website_diff(scrape_config, store, client, NOW)
    assert SA_FEED_URL in client.fetched


def test_rss_first_run_seeds_silently(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: VALID_FEED}
    )
    events = check_website_diff(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert reloaded.markers[website_rss_seed_key(SA_KEY)] == TODAY_ISO
    assert "sa-guid-1" in reloaded.rss_guids


def test_rss_new_post_emits_leopold(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    _seed_rss(store, ["sa-guid-1"])
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: VALID_FEED_TWO}
    )
    events = check_website_diff(scrape_config, store, client, NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.LEOPOLD_POST
    assert ev.identifier == "sa-guid-2"
    assert ev.url == "https://situational-awareness.com/p/two"
    assert ev.priority == Priority.HIGH
    assert ev.confidence == Confidence.HIGH
    assert ev.payload["excerpt"]


def test_rss_primary_no_page_hash_written(
    scrape_config: AppConfig, store: StateStore
) -> None:
    """RSS site: page-hash NOT consulted; no website:<key> snapshot for SA and no
    WEBSITE_DIFF fires for it."""
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: VALID_FEED}
    )
    check_website_diff(scrape_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert website_snapshot_key(SA_KEY) not in reloaded.conference_hashes
    # SA feed url fetched, SA page url NOT fetched.
    assert SA_URL not in client.fetched


def test_rss_valid_but_empty_feed_seeds_marker(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: EMPTY_VALID_FEED}
    )
    events = check_website_diff(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert reloaded.markers[website_rss_seed_key(SA_KEY)] == TODAY_ISO


def test_rss_not_a_feed_not_seeded(
    scrape_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: NOT_A_FEED}
    )
    with caplog.at_level("WARNING"):
        events = check_website_diff(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert website_rss_seed_key(SA_KEY) not in reloaded.markers


def test_rss_dedupe_against_bucket(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    _seed_rss(store, ["sa-guid-1", "sa-guid-2"])  # both already seen
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: VALID_FEED_TWO}
    )
    events = check_website_diff(scrape_config, store, client, NOW)
    assert events == []


def test_rss_branch_non_leopold_warns_and_skips(
    scrape_config: AppConfig,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Force SA_KEY to map to a non-LEOPOLD type -> WARNING + SKIP RSS branch.
    import monitors.website_diff as wd

    monkeypatch.setitem(wd._SITE_EVENT_TYPE, SA_KEY, EventType.WEBSITE_DIFF)
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    client = FakeFeedClient({GB_URL: _page("Gavin Baker home padding padding")})
    with caplog.at_level("WARNING"):
        events = check_website_diff(scrape_config, store, client, NOW)
    # No RSS event; SA feed never even fetched.
    assert events == []
    assert SA_FEED_URL not in client.fetched
    assert any("not LEOPOLD_POST" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Page-hash branch (gavinbaker_net, empty keywords)
# --------------------------------------------------------------------------- #


def test_page_hash_first_run_no_emit(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home content padding"), SA_FEED_URL: VALID_FEED}
    )
    events = check_website_diff(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert website_snapshot_key(GB_KEY) in reloaded.conference_hashes


def test_page_hash_change_emits_website_diff(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    _seed_page_snapshot(store, GB_KEY, _page("Old home content padding padding"))
    new = _page("New home content padding padding changed")
    client = FakeFeedClient({GB_URL: new, SA_FEED_URL: VALID_FEED})
    events = check_website_diff(scrape_config, store, client, NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.WEBSITE_DIFF
    assert ev.source == GB_KEY
    assert ev.identifier.startswith(f"{website_snapshot_key(GB_KEY)}@")
    assert ev.payload["diff"]
    assert ev.priority == Priority.MEDIUM
    # snapshot advanced.
    reloaded = store.load_seen_appearances()
    snap = reloaded.conference_hashes[website_snapshot_key(GB_KEY)]
    assert snap.hash == content_hash(extract_normalized_text(new))


def test_page_hash_empty_keywords_alerts_any_change(
    scrape_config: AppConfig, store: StateStore
) -> None:
    # gavinbaker_net has empty keywords -> any change alerts.
    _seed_rss(store, ["sa-guid-1"])
    _seed_page_snapshot(store, GB_KEY, _page("First padding padding padding"))
    new = _page("Totally different padding padding padding")
    client = FakeFeedClient({GB_URL: new, SA_FEED_URL: VALID_FEED})
    events = check_website_diff(scrape_config, store, client, NOW)
    assert len(events) == 1


def test_page_hash_unchanged_no_emit(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    content = _page("Stable home content padding padding")
    _seed_page_snapshot(store, GB_KEY, content)
    client = FakeFeedClient({GB_URL: content, SA_FEED_URL: VALID_FEED})
    events = check_website_diff(scrape_config, store, client, NOW)
    assert events == []


def test_page_hash_waf_and_min_length_not_seeded(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    tiny = b"<html><body>hi</body></html>"
    client = FakeFeedClient({GB_URL: tiny, SA_FEED_URL: VALID_FEED})
    check_website_diff(scrape_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert website_snapshot_key(GB_KEY) not in reloaded.conference_hashes


def test_page_hash_diff_capped(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    old = _page(*[f"line number {i} padding" for i in range(300)])
    _seed_page_snapshot(store, GB_KEY, old)
    new = _page(*[f"line number {i} padding" for i in range(300)], "brand new line")
    client = FakeFeedClient({GB_URL: new, SA_FEED_URL: VALID_FEED})
    ev = check_website_diff(scrape_config, store, client, NOW)[0]
    assert len(ev.payload["diff"]) <= DIFF_SNIPPET_MAX


# --------------------------------------------------------------------------- #
# Save semantics
# --------------------------------------------------------------------------- #


def test_save_failure_suppresses_content_keeps_feed(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    _seed_page_snapshot(store, GB_KEY, _page("Old content padding padding"))
    new = _page("New content padding padding changed")
    client = FakeFeedClient({GB_URL: new, SA_FEED_URL: VALID_FEED_TWO})

    def _boom(_data: SeenAppearances) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_seen_appearances", _boom)
    events = check_website_diff(scrape_config, store, client, NOW)
    types = {e.event_type for e in events}
    # WEBSITE_DIFF (content) suppressed; LEOPOLD_POST (feed) kept.
    assert EventType.WEBSITE_DIFF not in types
    assert EventType.LEOPOLD_POST in types


def test_empty_save_skip(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _page("Stable content padding padding")
    _seed_page_snapshot(store, GB_KEY, content)
    _seed_rss(store, ["sa-guid-1"])
    calls: list[int] = []
    real_save = store.save_seen_appearances

    def _counting(data: SeenAppearances) -> None:
        calls.append(1)
        real_save(data)

    monkeypatch.setattr(store, "save_seen_appearances", _counting)
    client = FakeFeedClient({GB_URL: content, SA_FEED_URL: VALID_FEED})
    check_website_diff(scrape_config, store, client, NOW)
    assert calls == []


def test_state_read_failure_fatal(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> SeenAppearances:
        raise MonitorError("state read boom")

    monkeypatch.setattr(store, "load_seen_appearances", _boom)
    client = FakeFeedClient({})
    with pytest.raises(MonitorError):
        check_website_diff(scrape_config, store, client, NOW)


def test_per_site_isolation(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    _seed_page_snapshot(store, GB_KEY, _page("Old content padding padding"))
    # GB page raises; SA feed still yields a new post.
    client = FakeFeedClient(
        {SA_FEED_URL: VALID_FEED_TWO}, raise_for=frozenset({GB_URL})
    )
    events = check_website_diff(scrape_config, store, client, NOW)
    assert [e.event_type for e in events] == [EventType.LEOPOLD_POST]


# --------------------------------------------------------------------------- #
# build_alert integration
# --------------------------------------------------------------------------- #


def test_build_alert_leopold(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_page_snapshot(store, GB_KEY, _page("Gavin Baker home padding padding"))
    _seed_rss(store, ["sa-guid-1"])
    client = FakeFeedClient(
        {GB_URL: _page("Gavin Baker home padding padding"), SA_FEED_URL: VALID_FEED_TWO}
    )
    ev = check_website_diff(scrape_config, store, client, NOW)[0]
    assert ev.event_type == EventType.LEOPOLD_POST
    alert = build_alert(ev, scrape_config)
    assert set(alert.channels) == {AlertChannel.EMAIL, AlertChannel.SMS}
    assert "Excerpt" in alert.body


def test_build_alert_website_diff(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_rss(store, ["sa-guid-1"])
    _seed_page_snapshot(store, GB_KEY, _page("Old content padding padding"))
    new = _page("New content padding padding changed here")
    client = FakeFeedClient({GB_URL: new, SA_FEED_URL: VALID_FEED})
    ev = check_website_diff(scrape_config, store, client, NOW)[0]
    assert ev.event_type == EventType.WEBSITE_DIFF
    alert = build_alert(ev, scrape_config)
    assert set(alert.channels) == {AlertChannel.EMAIL}
    assert "Change" in alert.body
