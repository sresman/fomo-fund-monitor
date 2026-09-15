from __future__ import annotations

"""Tests for the YouTube monitor (monitors/youtube.py).

A typed fake ``YouTubeClient`` (Protocol ``search``) is the primary seam. The
concrete ``YouTubeApiClient`` is tested via an injected fake ``build_fn`` (no
network, no real API, no env needed for the fake path).
"""

from datetime import datetime, timezone
from typing import Callable

import pytest

from alerting.formatting import build_alert
from config import AppConfig
from constants import ENV_YOUTUBE_API_KEY, MARKER_YOUTUBE_SWEEP
from errors import MonitorError
from models import AlertChannel, Confidence, EventType, Priority
from monitors.youtube import (
    VideoDetails,
    VideoResult,
    YouTubeApiClient,
    check_youtube,
    parse_iso8601_duration,
    _Classification,
    _parse_search_response,
)
from monitors._common import youtube_seed_key
from state_manager import SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TOMORROW = datetime(2026, 7, 23, tzinfo=timezone.utc)
TODAY_ISO = "2026-07-22"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeYouTubeClient:
    """Returns canned results per query; unknown query -> (); a designated
    query -> raises MonitorError (isolation test).

    ``details`` defaults to resolving NOTHING, which is the honest default for
    most tests here: an unresolved id falls back to the search snippet and an
    unknown duration. Pass ``details_by_id`` to exercise the enrichment, or
    ``raise_on_details`` to exercise its failure path.
    """

    def __init__(
        self,
        by_query: dict[str, tuple[VideoResult, ...]],
        raise_for: frozenset[str] = frozenset(),
        details_by_id: dict[str, VideoDetails] | None = None,
        raise_on_details: bool = False,
    ) -> None:
        self._by_query = by_query
        self._raise_for = raise_for
        self._details = details_by_id or {}
        self._raise_on_details = raise_on_details
        self.detail_calls: list[tuple[str, ...]] = []

    def search(self, query: str, max_results: int) -> tuple[VideoResult, ...]:
        if query in self._raise_for:
            raise MonitorError(f"boom for {query}")
        return self._by_query.get(query, ())

    def details(self, video_ids: tuple[str, ...]) -> dict[str, VideoDetails]:
        self.detail_calls.append(video_ids)
        if self._raise_on_details:
            raise MonitorError("boom for details")
        return {v: self._details[v] for v in video_ids if v in self._details}


def vid(
    video_id: str,
    title: str,
    channel: str = "Random Channel",
    published_at: str = "2026-07-22T10:00:00Z",
    description: str = "desc",
) -> VideoResult:
    return VideoResult(
        video_id=video_id,
        title=title,
        channel_title=channel,
        published_at=published_at,
        description=description,
    )


# Query strings from feeds_config.yaml.
Q_BAKER_BROAD = '"Gavin Baker"'
Q_BAKER_SWEEP1 = '"Gavin Baker" Atreides'
Q_BAKER_SWEEP2 = '"Gavin Baker" semiconductors'
Q_LEO_BROAD = '"Leopold Aschenbrenner"'
Q_LEO_SWEEP = '"Situational Awareness" AGI'


def _seed_all_markers(store: StateStore, config: AppConfig) -> None:
    """Mark every planned query (broad+sweep) + the sweep marker seeded for
    TODAY, so a subsequent run is fully in the normal branch."""
    markers: dict[str, str] = {MARKER_YOUTUBE_SWEEP: TODAY_ISO}
    for entity in config.entities:
        qset = config.youtube.queries_by_entity.get(entity.key)
        if qset is None:
            continue
        for q in list(qset.broad_queries) + list(qset.sweep_queries):
            markers[youtube_seed_key(q)] = TODAY_ISO
    store.save_seen_appearances(SeenAppearances(markers=markers))


# --------------------------------------------------------------------------- #
# now validation
# --------------------------------------------------------------------------- #


def test_now_naive_raises(feeds_config: AppConfig, store: StateStore) -> None:
    client = FakeYouTubeClient({})
    with pytest.raises(ValueError):
        check_youtube(feeds_config, store, client, datetime(2026, 7, 22))


# --------------------------------------------------------------------------- #
# concrete client via injected build_fn
# --------------------------------------------------------------------------- #


class FakeRequest:
    def __init__(self, response: object) -> None:
        self._response = response

    def execute(self) -> object:
        return self._response


class FakeSearchResource:
    def __init__(self, response: object) -> None:
        self._response = response

    def list(self, **kwargs: object) -> FakeRequest:
        return FakeRequest(self._response)


class FakeService:
    def __init__(self, response: object) -> None:
        self._response = response

    def search(self) -> FakeSearchResource:
        return FakeSearchResource(self._response)


def _canned_response() -> dict[str, object]:
    return {
        "items": [
            {
                "id": {"videoId": "vid00000001"},
                "snippet": {
                    "title": "Gavin Baker interview",
                    "channelTitle": "All-In Podcast",
                    "publishedAt": "2026-07-22T10:00:00Z",
                    "description": "chat",
                },
            },
            {
                # missing videoId -> skipped
                "id": {},
                "snippet": {"title": "no id"},
            },
        ]
    }


def test_concrete_client_extracts_and_builds_once() -> None:
    calls = {"n": 0}

    def build_fn(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return FakeService(_canned_response())

    client = YouTubeApiClient(build_fn=build_fn)
    r1 = client.search("q1", 5)
    r2 = client.search("q2", 5)
    assert calls["n"] == 1  # build ONCE across N searches
    assert len(r1) == 1
    assert r1[0].video_id == "vid00000001"
    assert r1[0].channel_title == "All-In Podcast"
    assert len(r2) == 1


def test_parse_search_skips_missing_videoid() -> None:
    results = _parse_search_response(_canned_response())
    assert [r.video_id for r in results] == ["vid00000001"]


def test_concrete_client_missing_key_default_build_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_YOUTUBE_API_KEY, raising=False)
    client = YouTubeApiClient()  # default build path, no injected build_fn
    with pytest.raises(MonitorError, match="YOUTUBE_API_KEY not set"):
        client.search("q", 5)


def test_concrete_client_fake_build_fn_needs_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_YOUTUBE_API_KEY, raising=False)
    client = YouTubeApiClient(build_fn=lambda *a, **k: FakeService(_canned_response()))
    results = client.search("q", 5)
    assert len(results) == 1


def test_concrete_client_missing_googleapiclient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import sys

    monkeypatch.setenv(ENV_YOUTUBE_API_KEY, "key")
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "googleapiclient.discovery" or name.startswith(
            "googleapiclient"
        ):
            raise ModuleNotFoundError("no googleapiclient")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delitem(sys.modules, "googleapiclient.discovery", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    client = YouTubeApiClient()
    with pytest.raises(MonitorError, match="google-api-python-client not installed"):
        client.search("q", 5)


# --------------------------------------------------------------------------- #
# confidence classification (surname gate)
# --------------------------------------------------------------------------- #


def test_high_surname_and_known_channel(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa1", "Baker chat", channel="All-In Podcast"),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].event_type == EventType.YOUTUBE_HIGH
    assert events[0].confidence == Confidence.HIGH
    assert events[0].priority == Priority.HIGH


def test_framing_in_title_no_longer_promotes_an_unknown_channel(
    feeds_config: AppConfig, store: StateStore
) -> None:
    """INVERTED 2026-09-03. This asserted HIGH for a framing word in the title
    on an UNKNOWN channel -- the loophole itself.

    A title is written by whoever uploaded it, so any third-party channel could
    call its 90-second cut "Gavin Baker interview" and inherit HIGH, which is
    exactly how a Chinese-language recap and two ~100-second clips reached the
    inbox. HIGH now requires a known publisher channel; this is MEDIUM, which
    routes nowhere.
    """
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa2", "Gavin Baker interview", channel="Zzz"),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].event_type == EventType.YOUTUBE_MEDIUM
    assert events[0].confidence == Confidence.MEDIUM


@pytest.mark.parametrize(
    ("channel", "title"),
    [
        # The three that actually reached the inbox on 2026-09-03.
        ("Markluce AI", "為什麼 AI 的需求跑贏了算力供給｜a16z × Gavin Baker 完整重點整理"),
        ("Bumlife2Bomblife. ent", "Gavin Baker: The Ensemble Future of AI Models"),
        ("UninformedInvestors", "Gavin Baker says Grok Bot feels like another ChatGPT moment"),
    ],
)
def test_real_derivative_uploads_classify_as_medium(
    feeds_config: AppConfig, store: StateStore, channel: str, title: str
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient({Q_BAKER_BROAD: (vid("aaaaaaaaaa9", title, channel=channel),)})
    events = check_youtube(feeds_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].event_type == EventType.YOUTUBE_MEDIUM


def test_medium_surname_only(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa3", "Baker on chips", channel="Zzz"),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].event_type == EventType.YOUTUBE_MEDIUM
    assert events[0].confidence == Confidence.MEDIUM
    assert events[0].priority == Priority.MEDIUM


def test_exclude_surname_not_in_title(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa4", "Something unrelated", channel="All-In Podcast"),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert events == []


def test_known_channel_case_insensitive(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa5", "Baker talks", channel="  all-in podcast "),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert events[0].event_type == EventType.YOUTUBE_HIGH


def test_suffix_stripped_surname() -> None:
    # person "Foo Bar Jr." -> surname "bar", matched in the title.
    from monitors.youtube import _classify

    cls = _classify("Foo Bar Jr.", "the bar segment", "Zzz", (), "")
    assert cls is not None
    assert cls.event_type == EventType.YOUTUBE_MEDIUM


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #


def test_dedupe_already_seen_and_manifest(
    feeds_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_all_markers(store, feeds_config)
    # Put one id in the bucket already.
    app = store.load_seen_appearances()
    app.youtube.append("seenvidaaa1")
    store.save_seen_appearances(app)
    # Manifest returns one id to skip.
    monkeypatch.setattr(
        "monitors.youtube.load_manifest_youtube_ids",
        lambda _p: {"manifestaaa1"},
    )
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (
                vid("seenvidaaa1", "Baker interview", channel="All-In Podcast"),
                vid("manifestaaa1", "Baker interview", channel="All-In Podcast"),
                vid("freshvidaa1", "Baker interview", channel="All-In Podcast"),
            )
        }
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.identifier for e in events] == ["freshvidaa1"]


def test_in_run_dedupe_same_id_two_queries(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    shared = vid("sharedvid01", "Baker interview", channel="All-In Podcast")
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (shared,), Q_BAKER_SWEEP1: (shared,)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.identifier for e in events] == ["sharedvid01"]


def test_in_run_seed_then_observe_not_reemitted(
    feeds_config: AppConfig, store: StateStore
) -> None:
    # Broad query first-run (seeds), sweep query already-seeded observes same id.
    markers = {youtube_seed_key(Q_BAKER_SWEEP1): TODAY_ISO}
    # Leave broad first-run; seed everything else so plan stays deterministic.
    for entity in feeds_config.entities:
        qset = feeds_config.youtube.queries_by_entity.get(entity.key)
        if qset is None:
            continue
        for q in list(qset.broad_queries) + list(qset.sweep_queries):
            if q != Q_BAKER_BROAD:
                markers[youtube_seed_key(q)] = TODAY_ISO
    markers[MARKER_YOUTUBE_SWEEP] = TODAY_ISO
    store.save_seen_appearances(SeenAppearances(markers=markers))

    shared = vid("crossvid001", "Baker interview", channel="All-In Podcast")
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (shared,), Q_BAKER_SWEEP1: (shared,)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    # Broad seeded it silently; sweep must NOT re-emit.
    assert events == []
    reloaded = store.load_seen_appearances()
    assert "crossvid001" in reloaded.youtube


# --------------------------------------------------------------------------- #
# per-query first-run seeding
# --------------------------------------------------------------------------- #


def test_first_run_seeds_and_no_events(
    feeds_config: AppConfig, store: StateStore
) -> None:
    # Nothing seeded -> everything first-run (broad+sweep run together).
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (vid("firstbaker1", "Baker interview", channel="All-In Podcast"),),
            Q_LEO_BROAD: (vid("firstleo001", "Aschenbrenner talk"),),
        }
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert "firstbaker1" in reloaded.youtube
    assert "firstleo001" in reloaded.youtube
    assert reloaded.markers[youtube_seed_key(Q_BAKER_BROAD)] == TODAY_ISO
    assert reloaded.markers[youtube_seed_key(Q_LEO_BROAD)] == TODAY_ISO
    assert reloaded.markers[MARKER_YOUTUBE_SWEEP] == TODAY_ISO


def test_mixed_first_run_and_normal(
    feeds_config: AppConfig, store: StateStore
) -> None:
    # Seed everything except the leopold broad query.
    markers: dict[str, str] = {MARKER_YOUTUBE_SWEEP: TODAY_ISO}
    for entity in feeds_config.entities:
        qset = feeds_config.youtube.queries_by_entity.get(entity.key)
        if qset is None:
            continue
        for q in list(qset.broad_queries) + list(qset.sweep_queries):
            if q != Q_LEO_BROAD:
                markers[youtube_seed_key(q)] = TODAY_ISO
    store.save_seen_appearances(SeenAppearances(markers=markers))

    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (vid("emitbaker01", "Baker interview", channel="All-In Podcast"),),
            Q_LEO_BROAD: (vid("seedleo0001", "Aschenbrenner"),),
        }
    )
    events = check_youtube(feeds_config, store, client, NOW)
    # Baker broad is normal -> emits; leopold broad first-run -> seeds silently.
    assert [e.identifier for e in events] == ["emitbaker01"]
    reloaded = store.load_seen_appearances()
    assert "seedleo0001" in reloaded.youtube
    assert reloaded.markers[youtube_seed_key(Q_LEO_BROAD)] == TODAY_ISO


# --------------------------------------------------------------------------- #
# partial-query-failure (the per-query fix)
# --------------------------------------------------------------------------- #


def test_partial_query_failure(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("okbaker0001", "Baker interview", channel="All-In Podcast"),)},
        raise_for=frozenset({Q_BAKER_SWEEP1}),
    )
    events = check_youtube(feeds_config, store, client, NOW)
    # Broad still emits despite the sweep query raising.
    assert [e.identifier for e in events] == ["okbaker0001"]
    reloaded = store.load_seen_appearances()
    # Failed sweep query marker unchanged (was TODAY from seed, still TODAY --
    # it did not get REMOVED and did not error). The key point: no crash, broad
    # emitted. Sweep marker still present since the run was already-seeded.
    assert reloaded.markers[youtube_seed_key(Q_BAKER_SWEEP1)] == TODAY_ISO


def test_failed_first_run_query_stays_first_run(
    feeds_config: AppConfig, store: StateStore
) -> None:
    # Seed everything except baker sweep1; make baker sweep1 raise on first run.
    markers: dict[str, str] = {MARKER_YOUTUBE_SWEEP: TODAY_ISO}
    for entity in feeds_config.entities:
        qset = feeds_config.youtube.queries_by_entity.get(entity.key)
        if qset is None:
            continue
        for q in list(qset.broad_queries) + list(qset.sweep_queries):
            if q != Q_BAKER_SWEEP1:
                markers[youtube_seed_key(q)] = TODAY_ISO
    store.save_seen_appearances(SeenAppearances(markers=markers))

    client = FakeYouTubeClient({}, raise_for=frozenset({Q_BAKER_SWEEP1}))
    check_youtube(feeds_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert youtube_seed_key(Q_BAKER_SWEEP1) not in reloaded.markers


# --------------------------------------------------------------------------- #
# first-run seeds manifest-matched ids too
# --------------------------------------------------------------------------- #


def test_first_run_seeds_manifest_matched(
    feeds_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "monitors.youtube.load_manifest_youtube_ids",
        lambda _p: {"manifestvid1"},
    )
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("manifestvid1", "Baker interview", channel="All-In Podcast"),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert events == []  # first-run
    reloaded = store.load_seen_appearances()
    assert "manifestvid1" in reloaded.youtube  # seeded despite being in manifest


# --------------------------------------------------------------------------- #
# successful-empty completes first-run; later match emits
# --------------------------------------------------------------------------- #


def test_empty_first_run_sets_marker_then_later_emits(
    feeds_config: AppConfig, store: StateStore
) -> None:
    empty_client = FakeYouTubeClient({})  # all queries -> ()
    check_youtube(feeds_config, store, empty_client, NOW)
    reloaded = store.load_seen_appearances()
    assert reloaded.markers[youtube_seed_key(Q_BAKER_BROAD)] == TODAY_ISO

    # Second run: marker present -> normal branch -> real result emits.
    client2 = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("laterbaker1", "Baker interview", channel="All-In Podcast"),)}
    )
    events = check_youtube(feeds_config, store, client2, NOW)
    assert [e.identifier for e in events] == ["laterbaker1"]


# --------------------------------------------------------------------------- #
# sweep marker scheduling
# --------------------------------------------------------------------------- #


def test_sweep_runs_first_then_once_per_day(
    feeds_config: AppConfig, store: StateStore
) -> None:
    called: list[str] = []

    class RecordingClient:
        def search(self, query: str, max_results: int) -> tuple[VideoResult, ...]:
            called.append(query)
            return ()

        def details(self, video_ids: tuple[str, ...]) -> dict[str, VideoDetails]:
            return {}

    # First run -> broad + sweep both called.
    check_youtube(feeds_config, store, RecordingClient(), NOW)
    assert Q_BAKER_SWEEP1 in called
    assert store.load_seen_appearances().markers[MARKER_YOUTUBE_SWEEP] == TODAY_ISO

    # Same-day rerun -> sweep NOT called again (marker == today).
    called.clear()
    check_youtube(feeds_config, store, RecordingClient(), NOW)
    assert Q_BAKER_BROAD in called
    assert Q_BAKER_SWEEP1 not in called

    # Tomorrow -> sweep runs again, marker updated.
    called.clear()
    check_youtube(feeds_config, store, RecordingClient(), TOMORROW)
    assert Q_BAKER_SWEEP1 in called
    assert (
        store.load_seen_appearances().markers[MARKER_YOUTUBE_SWEEP]
        == "2026-07-23"
    )


def test_sweep_all_fail_does_not_set_marker(
    feeds_config: AppConfig, store: StateStore
) -> None:
    # Seed all broad+sweep so we're in the normal branch; leave sweep marker
    # absent so a sweep is scheduled. All sweep queries raise.
    markers: dict[str, str] = {}
    sweep_queries: set[str] = set()
    for entity in feeds_config.entities:
        qset = feeds_config.youtube.queries_by_entity.get(entity.key)
        if qset is None:
            continue
        for q in list(qset.broad_queries) + list(qset.sweep_queries):
            markers[youtube_seed_key(q)] = TODAY_ISO
        sweep_queries.update(qset.sweep_queries)
    store.save_seen_appearances(SeenAppearances(markers=markers))

    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("broadok0001", "Baker interview", channel="All-In Podcast"),)},
        raise_for=frozenset(sweep_queries),
    )
    events = check_youtube(feeds_config, store, client, NOW)
    # Broad (non-first-run) still emits.
    assert [e.identifier for e in events] == ["broadok0001"]
    reloaded = store.load_seen_appearances()
    assert MARKER_YOUTUBE_SWEEP not in reloaded.markers


# --------------------------------------------------------------------------- #
# batched write with zero events
# --------------------------------------------------------------------------- #


def test_batched_write_runs_with_zero_events(
    feeds_config: AppConfig, store: StateStore
) -> None:
    client = FakeYouTubeClient({})  # first run, all empty
    events = check_youtube(feeds_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert reloaded.markers  # markers persisted despite zero events


# --------------------------------------------------------------------------- #
# entity without queries_by_entity is skipped
# --------------------------------------------------------------------------- #


def test_entity_without_queries_skipped(store: StateStore) -> None:
    # Build a config where one entity has no queries_by_entity entry.
    from config import (
        AppConfig as AC,
        DispatchBridgeConfig,
        EntityConfig,
        GoogleNewsConfig,
        CNBCConfig,
        PodcastRSSConfig,
        YouTubeConfig,
        YouTubeQuerySet,
        Paths,
        AlertRecipients,
    )
    from constants import DEFAULT_DISPATCH_EVENT_TYPE
    from models import EventType as ET, AlertChannel as AChan
    from pathlib import Path

    e1 = EntityConfig("atreides", "Atreides", "Gavin Baker", "0001777813", ("13F-HR",))
    e2 = EntityConfig("nomon", "NoMon", "Nobody Person", "0002045724", ("13F-HR",))
    yt = YouTubeConfig(
        queries_by_entity={
            "atreides": YouTubeQuerySet(broad_queries=(Q_BAKER_BROAD,), sweep_queries=())
        },
        max_results_per_query=5,
        master_manifest_path=Path("reference/master_manifest_v2.json").resolve(),
        known_channels=("All-In Podcast",),
        framing_keywords=("interview",),
    )
    routing: dict[ET, tuple[AChan, ...]] = {et: (AChan.EMAIL,) for et in ET}
    cfg = AC(
        entities=(e1, e2),
        monitor_intervals={
            "edgar": 15, "youtube": 120, "podcast_rss": 30, "google_news": 120,
            "cnbc": 360, "conference_pages": 1440, "website_diff": 1440,
        },
        youtube=yt,
        podcast_rss=PodcastRSSConfig(feeds=()),
        google_news=GoogleNewsConfig(queries=()),
        cnbc=CNBCConfig(queries=()),
        conference_pages=(),
        website_diff=(),
        alert_routing=routing,
        alert_recipients=AlertRecipients(email_env="ALERT_EMAIL", phone_env="ALERT_PHONE"),
        paths=Paths(state_dir=Path("state").resolve(), reference_dir=Path("reference").resolve()),
        dispatch_bridge=DispatchBridgeConfig(
            enabled=False, repo="", event_type=DEFAULT_DISPATCH_EVENT_TYPE
        ),
    )
    client = FakeYouTubeClient({})
    check_youtube(cfg, store, client, NOW)
    reloaded = store.load_seen_appearances()
    # No marker for the unmonitored entity's (nonexistent) queries.
    assert youtube_seed_key(Q_BAKER_BROAD) in reloaded.markers
    # nomon contributes no queries, so nothing extra.


# --------------------------------------------------------------------------- #
# payload / url / identifier / published
# --------------------------------------------------------------------------- #


def test_payload_and_fields(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (
                vid(
                    "payloadvid1",
                    "Baker interview",
                    channel="All-In Podcast",
                    published_at="2026-07-22T10:00:00Z",
                    description="  long   description  text ",
                ),
            )
        }
    )
    ev = check_youtube(feeds_config, store, client, NOW)[0]
    assert set(ev.payload.keys()) == {"person", "duration", "description"}
    assert ev.payload["duration"] == ""
    assert ev.payload["person"] == "Gavin Baker"
    assert ev.payload["description"] == "long description text"
    assert ev.url == "https://www.youtube.com/watch?v=payloadvid1"
    assert ev.identifier == "payloadvid1"
    assert ev.published is not None
    assert ev.published.tzinfo is not None
    assert ev.published.astimezone(timezone.utc).hour == 10


def test_naive_published_becomes_none(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("naivevid001", "Baker interview", channel="All-In Podcast", published_at="2026-07-22T10:00:00"),)}
    )
    ev = check_youtube(feeds_config, store, client, NOW)[0]
    assert ev.published is None


def test_empty_published_none_event_still_emitted(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("emptypub001", "Baker interview", channel="All-In Podcast", published_at=""),)}
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].published is None


# --------------------------------------------------------------------------- #
# multi-entity attribution
# --------------------------------------------------------------------------- #


def test_multi_entity_attribution(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (vid("attrbaker01", "Baker interview", channel="All-In Podcast"),),
            Q_LEO_BROAD: (vid("attrleo0001", "Aschenbrenner interview", channel="All-In Podcast"),),
        }
    )
    events = check_youtube(feeds_config, store, client, NOW)
    by_id = {e.identifier: e for e in events}
    assert by_id["attrbaker01"].entity_key == "atreides"
    assert by_id["attrbaker01"].payload["person"] == "Gavin Baker"
    assert by_id["attrleo0001"].entity_key == "situational_awareness"
    assert by_id["attrleo0001"].payload["person"] == "Leopold Aschenbrenner"


# --------------------------------------------------------------------------- #
# build_alert integration
# --------------------------------------------------------------------------- #


def test_build_alert_youtube_high(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("alerthigh01", "Baker interview", channel="All-In Podcast"),)}
    )
    ev = check_youtube(feeds_config, store, client, NOW)[0]
    alert = build_alert(ev, feeds_config)
    assert alert.subject
    assert alert.body
    assert set(alert.channels) == {AlertChannel.EMAIL, AlertChannel.SMS}


def test_build_alert_youtube_medium(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("alertmed001", "Baker on chips", channel="Zzz"),)}
    )
    ev = check_youtube(feeds_config, store, client, NOW)[0]
    alert = build_alert(ev, feeds_config)
    assert set(alert.channels) == {AlertChannel.EMAIL}


# --------------------------------------------------------------------------- #
# Duration enrichment (videos.list; search.list omits contentDetails)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PT1H14M26S", 4466),  # the real a16z episode
        ("PT45S", 45),
        ("PT7M35S", 455),
        ("PT16M", 960),
        ("P1DT2H", 93600),
        ("PT0S", 0),
        ("P2D", 172800),
    ],
)
def test_parse_iso8601_duration_accepts_real_shapes(text: str, expected: int) -> None:
    assert parse_iso8601_duration(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "garbage", "1H2M", "PT", "PT1.5M", "P"])
def test_parse_iso8601_duration_rejects_to_none_not_zero(text: str) -> None:
    """None is UNKNOWN. Collapsing it to 0 would make every unparseable video
    look shorter than any floor, which is the silent-discard failure the digest
    exists to prevent."""
    assert parse_iso8601_duration(text) is None


def test_durations_fill_the_event_payload(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa1", "Gavin Baker on the AI bubble"),)},
        details_by_id={"aaaaaaaaaa1": VideoDetails(4466, "")},
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.payload["duration"] for e in events] == ["4466"]


def test_details_lookup_is_one_batched_call_for_all_candidates(
    feeds_config: AppConfig, store: StateStore
) -> None:
    """Quota discipline: videos.list is 1 unit, but one call per video would
    still be one HTTP round trip per hit."""
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (
                vid("aaaaaaaaaa1", "Gavin Baker interview"),
                vid("aaaaaaaaaa2", "Gavin Baker again"),
            )
        },
        details_by_id={
            "aaaaaaaaaa1": VideoDetails(4466, ""),
            "aaaaaaaaaa2": VideoDetails(96, ""),
        },
    )
    check_youtube(feeds_config, store, client, NOW)
    assert client.detail_calls == [("aaaaaaaaaa1", "aaaaaaaaaa2")]


def test_unresolved_duration_stays_empty_not_zero(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa1", "Gavin Baker on the AI bubble"),)},
        details_by_id={},  # resolves nothing
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.payload["duration"] for e in events] == [""]


def test_details_lookup_failure_never_fails_the_run(
    feeds_config: AppConfig, store: StateStore
) -> None:
    """Enrichment is best-effort: a videos.list outage must degrade the digest's
    precision, never drop the event."""
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {Q_BAKER_BROAD: (vid("aaaaaaaaaa1", "Gavin Baker on the AI bubble"),)},
        raise_on_details=True,
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.identifier for e in events] == ["aaaaaaaaaa1"]
    assert events[0].payload["duration"] == ""


def test_no_candidates_means_no_details_call(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient({})
    assert check_youtube(feeds_config, store, client, NOW) == []
    assert client.detail_calls == []


# --------------------------------------------------------------------------- #
# Channel-first classification (FLAG-YT-TITLE, 2026-09-15)
# --------------------------------------------------------------------------- #

A16Z_DESC = (
    "a16z's David George sits down with Gavin Baker to unpack the state of "
    "the AI boom, why demand for intelligence may still be dramatically "
    "underestimated, and why the outcome doesn't have to be winner-take-all."
)
ALLIN_DESC = (
    "(0:00) Bestie intros! Gavin Baker, Ben Shapiro, and Phil Deutch join the "
    "show (7:32) GPT-5 underwhelms, benchmark saturation"
)
# A 95-minute Bloomberg market show that merely MENTIONS him -- the exact shape
# a duration floor cannot tell apart from a real appearance.
MARKET_SHOW_DESC = (
    "Technology stocks rallied as a rebound in chipmakers carried into Friday. "
    "Gavin Baker's Atreides was among the funds cited in the selloff coverage."
)


def _cls(
    title: str,
    channel: str,
    description: str = "",
    person: str = "Gavin Baker",
) -> "_Classification | None":
    from monitors.youtube import _classify

    return _classify(person, title, channel, ("a16z", "All-In Podcast"), description)


def test_allowlisted_channel_qualifies_on_description_framing() -> None:
    """THE BUG. a16z titled a 74-minute Baker interview "Why AI Demand Is
    Outrunning Compute Supply" -- no surname -- and it never entered the dedupe
    bucket at all. The publisher is the first-party signal; the title is not."""
    cls = _cls("Why AI Demand Is Outrunning Compute Supply", "a16z", A16Z_DESC)
    assert cls is not None
    assert cls.event_type == EventType.YOUTUBE_HIGH


def test_allowlisted_panel_show_qualifies_on_join_framing() -> None:
    cls = _cls(
        "OpenAI's GPT-5 Flop, AI's Unlimited Market, China's Big Advantage",
        "All-In Podcast",
        ALLIN_DESC,
    )
    assert cls is not None
    assert cls.event_type == EventType.YOUTUBE_HIGH


def test_allowlisted_channel_still_qualifies_on_surname_in_title() -> None:
    """The original rule is preserved exactly -- no regression for the 43 videos
    already classifying HIGH on title alone."""
    cls = _cls("Baker chat", "All-In Podcast")
    assert cls is not None
    assert cls.event_type == EventType.YOUTUBE_HIGH


def test_allowlisted_channel_membership_alone_is_not_enough() -> None:
    """Measured over the full back catalogue of all 17 allowlisted channels,
    admitting on membership alone would newly alert on 61 videos; a 20m floor
    only cuts that to 25, because the noise is long-form too. The description
    gate cuts the same 61 to 4."""
    assert _cls("Amazon Lifts Tech Rebound | The Opening Trade", "a16z") is None


def test_allowlisted_mention_without_framing_is_excluded() -> None:
    cls = _cls("Amazon Lifts Tech Rebound", "a16z", MARKET_SHOW_DESC)
    assert cls is None


def test_off_allowlist_rule_is_unchanged_surname_in_title() -> None:
    cls = _cls("Gavin Baker on the AI bubble", "Some Random Channel")
    assert cls is not None
    assert cls.event_type == EventType.YOUTUBE_MEDIUM


def test_off_allowlist_description_framing_does_not_promote() -> None:
    """Off the allowlist the description is NOT consulted: anyone can write
    "sits down with" under a reupload."""
    assert _cls("Why AI Demand Is Outrunning", "Some Random Channel", A16Z_DESC) is None


def test_off_allowlist_surname_in_title_stays_medium_despite_framing() -> None:
    cls = _cls("Gavin Baker interview", "Some Random Channel", A16Z_DESC)
    assert cls is not None
    assert cls.event_type == EventType.YOUTUBE_MEDIUM


def test_full_description_is_used_for_classification(
    feeds_config: AppConfig, store: StateStore
) -> None:
    """End-to-end: search.list truncates descriptions to ~120 chars, so the
    framing must be read from the videos.list copy."""
    _seed_all_markers(store, feeds_config)
    truncated = "a16z's David George sits down with ..."
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (
                vid(
                    "aaaaaaaaaa1",
                    "Why AI Demand Is Outrunning Compute Supply",
                    channel="All-In Podcast",
                    description=truncated,
                ),
            )
        },
        details_by_id={"aaaaaaaaaa1": VideoDetails(4466, A16Z_DESC)},
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.event_type for e in events] == [EventType.YOUTUBE_HIGH]
    assert events[0].payload["duration"] == "4466"


def test_details_failure_falls_back_to_the_search_snippet(
    feeds_config: AppConfig, store: StateStore
) -> None:
    """If videos.list is down, classification still runs on what search gave
    us -- degraded, never skipped."""
    _seed_all_markers(store, feeds_config)
    client = FakeYouTubeClient(
        {
            Q_BAKER_BROAD: (
                vid(
                    "aaaaaaaaaa1",
                    "Why AI Demand Is Outrunning Compute Supply",
                    channel="All-In Podcast",
                    description=A16Z_DESC,
                ),
            )
        },
        raise_on_details=True,
    )
    events = check_youtube(feeds_config, store, client, NOW)
    assert [e.event_type for e in events] == [EventType.YOUTUBE_HIGH]
    assert events[0].payload["duration"] == ""
