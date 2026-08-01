from __future__ import annotations

"""Tests for the podcast RSS monitor (monitors/podcast_rss.py).

A typed fake ``FeedClient`` (Protocol ``fetch``) returns canned XML bytes by URL.
No network.
"""

from datetime import datetime, timezone

import pytest

from alerting.formatting import build_alert
from config import AppConfig
from errors import MonitorError
from models import AlertChannel, Confidence, EventType, Priority
from monitors._common import podcast_seed_key
from monitors.podcast_rss import check_podcast_rss
from state_manager import SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TODAY_ISO = "2026-07-22"

ILTB_URL = "https://example.com/iltb.rss"
DWARKESH_URL = "https://example.com/dwarkesh.rss"
GENERIC_URL = "https://example.com/generic.rss"

FEED_WITH_BAKER = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Invest Like the Best</title>
<item>
  <title>Gavin Baker on chips</title>
  <link>https://example.com/ep1</link>
  <guid>guid-baker-1</guid>
  <description>Baker discusses semiconductors.</description>
  <pubDate>Tue, 22 Jul 2025 12:00:00 GMT</pubDate>
  <enclosure url="https://example.com/a1.mp3" length="1" type="audio/mpeg"/>
</item>
<item>
  <title>Unrelated market chat</title>
  <link>https://example.com/ep2</link>
  <guid>guid-other-1</guid>
  <description>No notable guest.</description>
</item>
</channel></rss>"""

FEED_NO_GUID = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>ILTB</title>
<item>
  <title>Baker chat no guid</title>
  <link>https://example.com/noguid</link>
  <description>Baker again.</description>
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
        try:
            return self._by_url[url]
        except KeyError as exc:
            raise MonitorError(f"no feed for {url}") from exc


def _seed_all(store: StateStore, config: AppConfig) -> None:
    markers: dict[str, str] = {}
    for feed in config.podcast_rss.feeds:
        if feed.url.strip() == "":
            continue
        markers[podcast_seed_key(feed.url)] = TODAY_ISO
    store.save_seen_appearances(SeenAppearances(markers=markers))


# --------------------------------------------------------------------------- #


def test_now_naive_raises(feeds_config: AppConfig, store: StateStore) -> None:
    client = FakeFeedClient({})
    with pytest.raises(ValueError):
        check_podcast_rss(feeds_config, store, client, datetime(2026, 7, 22))


def test_empty_url_feed_skipped(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    check_podcast_rss(feeds_config, store, client, NOW)
    # The empty-url "All-In Podcast" feed is never fetched.
    assert "" not in client.fetched
    reloaded = store.load_seen_appearances()
    assert podcast_seed_key("") not in reloaded.markers


def test_keyword_match_emits_and_dedupes(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    # Only the Baker item matches ("Baker" keyword); the unrelated one does not.
    assert [e.identifier for e in events] == ["guid-baker-1"]
    assert events[0].event_type == EventType.PODCAST_RSS
    assert events[0].priority == Priority.HIGH
    assert events[0].confidence == Confidence.HIGH


def test_already_seen_guid_skipped(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    app = store.load_seen_appearances()
    app.rss_guids.append("guid-baker-1")
    store.save_seen_appearances(app)
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    assert events == []


def test_guid_falls_back_to_link(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {ILTB_URL: FEED_NO_GUID, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    assert [e.identifier for e in events] == ["https://example.com/noguid"]


def test_person_mapping(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    dwarkesh_feed = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Dwarkesh</title>
<item><title>Leopold Aschenbrenner on AGI</title><link>https://x/leo</link>
<guid>guid-leo-1</guid><description>Aschenbrenner talk.</description></item>
</channel></rss>"""
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: dwarkesh_feed, GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    by_id = {e.identifier: e for e in events}
    assert by_id["guid-baker-1"].entity_key == "atreides"
    assert by_id["guid-baker-1"].payload["person"] == "Gavin Baker"
    assert by_id["guid-leo-1"].entity_key == "situational_awareness"
    assert by_id["guid-leo-1"].payload["person"] == "Leopold Aschenbrenner"


def test_per_feed_first_run_seeds_silently(
    feeds_config: AppConfig, store: StateStore
) -> None:
    # No markers -> all feeds first-run.
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert "guid-baker-1" in reloaded.rss_guids
    assert reloaded.markers[podcast_seed_key(ILTB_URL)] == TODAY_ISO


def test_empty_match_first_run_sets_marker_then_emits(
    feeds_config: AppConfig, store: StateStore
) -> None:
    empty = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>ILTB</title></channel></rss>"""
    client = FakeFeedClient(
        {ILTB_URL: empty, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    check_podcast_rss(feeds_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert reloaded.markers[podcast_seed_key(ILTB_URL)] == TODAY_ISO

    client2 = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client2, NOW)
    assert [e.identifier for e in events] == ["guid-baker-1"]


def test_fetch_failure_leaves_feed_first_run(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient(
        {DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"},
        raise_for=frozenset({ILTB_URL}),
    )
    check_podcast_rss(feeds_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert podcast_seed_key(ILTB_URL) not in reloaded.markers
    # Other feeds still seeded.
    assert podcast_seed_key(DWARKESH_URL) in reloaded.markers


def test_per_feed_isolation(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"},
        raise_for=frozenset({ILTB_URL}),
    )
    # ILTB raises but the run does not crash; no events (others empty/no match).
    events = check_podcast_rss(feeds_config, store, client, NOW)
    assert events == []


def test_in_run_dedupe_same_guid_two_feeds(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    shared = FEED_WITH_BAKER  # guid-baker-1
    # Put the same guid in the dwarkesh feed too, with a Leopold keyword match.
    dup_feed = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>D</title>
<item><title>Baker and Aschenbrenner</title><link>https://x/d</link>
<guid>guid-baker-1</guid><description>Leopold and Baker.</description></item>
</channel></rss>"""
    client = FakeFeedClient(
        {ILTB_URL: shared, DWARKESH_URL: dup_feed, GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    assert [e.identifier for e in events].count("guid-baker-1") == 1


def test_payload_keys_and_audio_url(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    ev = check_podcast_rss(feeds_config, store, client, NOW)[0]
    assert set(ev.payload.keys()) == {"person", "audio_url", "description"}
    assert ev.payload["audio_url"] == "https://example.com/a1.mp3"
    assert ev.source == "Invest Like the Best"  # feed.show, not source_title
    assert ev.published is not None
    assert ev.published.tzinfo is not None


def test_batched_write_zero_events(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient(
        {ILTB_URL: b"<rss></rss>", DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    events = check_podcast_rss(feeds_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert reloaded.markers  # first-run markers persisted


def test_build_alert_podcast(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    client = FakeFeedClient(
        {ILTB_URL: FEED_WITH_BAKER, DWARKESH_URL: b"<rss></rss>", GENERIC_URL: b"<rss></rss>"}
    )
    ev = check_podcast_rss(feeds_config, store, client, NOW)[0]
    alert = build_alert(ev, feeds_config)
    assert alert.subject
    assert alert.body
    assert set(alert.channels) == {AlertChannel.EMAIL, AlertChannel.SMS}
