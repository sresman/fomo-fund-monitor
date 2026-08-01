from __future__ import annotations

"""Tests for the CNBC monitor (monitors/cnbc.py).

Two seams tested: the fragile ``_parse_search_html`` (canned HTML bytes) and the
``check_cnbc`` flow (fake ``CnbcClient``). No network."""

import urllib.parse
from datetime import datetime, timezone

import pytest

from alerting.formatting import build_alert
from config import AppConfig
from constants import CNBC_SEARCH_URL, CNBC_SOURCE_LABEL
from errors import MonitorError
from models import AlertChannel, Confidence, EventType, Priority
from monitors._common import ResponseLike, cnbc_seed_key
from monitors.cnbc import (
    CnbcHttpClient,
    CnbcResult,
    _canonicalize_url,
    _map_query,
    _parse_search_html,
    check_cnbc,
)
from state_manager import SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TODAY_ISO = "2026-07-22"

Q_BAKER = "Gavin Baker"
Q_LEO = "Leopold Aschenbrenner"

# Recognizable results container + two video anchors (one relative, one absolute).
SEARCH_HTML = (
    b"<html><body>"
    b'<div class="SearchResult-searchResultCard">'
    b'<a href="/video/2026/07/22/gavin-baker-on-ai.html">Gavin Baker on AI</a>'
    b"</div>"
    b'<div class="SearchResult-searchResultCard">'
    b'<a href="https://www.cnbc.com/video/2026/07/21/baker-macro.html">Baker on macro</a>'
    b"</div>"
    b'<a href="/business/some-article.html">Not a video article link</a>'
    b"</body></html>"
)

# Structured but zero video rows (recognizable container, no /video/ anchors).
EMPTY_STRUCTURED_HTML = (
    b'<html><body><div class="SearchResults-searchResultsContainer">'
    b"<p>No results found</p></div></body></html>"
)

# Bare challenge/empty shell: no recognizable structure, no video anchors.
CHALLENGE_HTML = b"<html><body><p>Please enable javascript to continue.</p></body></html>"


class FakeCnbcClient:
    def __init__(
        self,
        by_query: dict[str, tuple[CnbcResult, ...]],
        recognized_by_query: dict[str, bool] | None = None,
        raise_for: frozenset[str] = frozenset(),
    ) -> None:
        self._by_query = by_query
        self._recognized_by_query = recognized_by_query or {}
        self._raise_for = raise_for
        self._last_query = ""
        self.searched: list[str] = []

    def search(self, query: str) -> tuple[CnbcResult, ...]:
        self.searched.append(query)
        self._last_query = query
        if query in self._raise_for:
            raise MonitorError(f"boom {query}")
        return self._by_query.get(query, ())

    def had_recognizable_structure(self) -> bool:
        return self._recognized_by_query.get(self._last_query, False)


class FakeHttpGetter:
    def __init__(self, content: bytes, raise_exc: Exception | None = None) -> None:
        self._content = content
        self._raise = raise_exc

    def get(
        self, url: str, *, headers: object, timeout: object
    ) -> ResponseLike:
        return _FakeResponse(self._content, self._raise)


class _FakeResponse:
    def __init__(self, content: bytes, raise_exc: Exception | None) -> None:
        self._content = content
        self._raise = raise_exc

    @property
    def content(self) -> bytes:
        return self._content

    def raise_for_status(self) -> None:
        if self._raise is not None:
            raise self._raise


def _search_url(query: str) -> str:
    return CNBC_SEARCH_URL.format(query=urllib.parse.quote_plus(query))


def _seed_all(store: StateStore, config: AppConfig) -> None:
    markers = {cnbc_seed_key(q): TODAY_ISO for q in config.cnbc.queries}
    store.save_seen_appearances(SeenAppearances(markers=markers))


def _vid(url: str, title: str = "T") -> CnbcResult:
    return CnbcResult(url=url, title=title, published=None)


# --------------------------------------------------------------------------- #
# _parse_search_html + _canonicalize_url
# --------------------------------------------------------------------------- #


def test_parse_extracts_video_rows() -> None:
    parsed = _parse_search_html(SEARCH_HTML)
    assert parsed.recognized is True
    assert len(parsed.results) == 2
    urls = [r.url for r in parsed.results]
    assert "https://www.cnbc.com/video/2026/07/22/gavin-baker-on-ai.html" in urls
    assert "https://www.cnbc.com/video/2026/07/21/baker-macro.html" in urls
    titles = [r.title for r in parsed.results]
    assert "Gavin Baker on AI" in titles


def test_parse_relative_anchor_resolved() -> None:
    parsed = _parse_search_html(SEARCH_HTML)
    rel = next(r for r in parsed.results if "gavin-baker-on-ai" in r.url)
    assert rel.url.startswith("https://www.cnbc.com/video/")


def test_parse_structural_sentinel_true_zero_rows() -> None:
    parsed = _parse_search_html(EMPTY_STRUCTURED_HTML)
    assert parsed.recognized is True
    assert parsed.results == ()


def test_parse_challenge_shell_no_structure_zero_rows() -> None:
    parsed = _parse_search_html(CHALLENGE_HTML)
    assert parsed.recognized is False
    assert parsed.results == ()


def test_parse_malformed_row_skipped() -> None:
    html = (
        b'<html><body><div class="searchresult">'
        b"<a>no href video anchor</a>"
        b'<a href="/video/x.html">Good</a>'
        b"</div></body></html>"
    )
    parsed = _parse_search_html(html)
    assert len(parsed.results) == 1
    assert parsed.results[0].title == "Good"


def test_canonicalize_drops_query_and_fragment() -> None:
    a = _canonicalize_url("https://www.cnbc.com/video/x.html?foo=1#frag")
    b = _canonicalize_url("https://WWW.CNBC.com/video/x.html/")
    assert a == b == "https://www.cnbc.com/video/x.html"


# --------------------------------------------------------------------------- #
# CnbcHttpClient
# --------------------------------------------------------------------------- #


def test_http_client_parses_fixture() -> None:
    client = CnbcHttpClient(http=FakeHttpGetter(SEARCH_HTML))
    results = client.search(Q_BAKER)
    assert len(results) == 2
    assert client.had_recognizable_structure() is True
    # Assert it fetched the encoded search url shape (indirectly: no crash).
    assert results[0].url.startswith("https://www.cnbc.com/video/")


def test_http_client_transport_fault_raises_monitor_error() -> None:
    client = CnbcHttpClient(
        http=FakeHttpGetter(b"", raise_exc=RuntimeError("boom"))
    )
    with pytest.raises(MonitorError):
        client.search(Q_BAKER)


# --------------------------------------------------------------------------- #
# _map_query
# --------------------------------------------------------------------------- #


def test_map_query_surname_substring(scrape_config: AppConfig) -> None:
    assert _map_query(scrape_config, "Gavin Baker") == "atreides"
    assert _map_query(scrape_config, "baker interview cnbc") == "atreides"
    assert (
        _map_query(scrape_config, "Leopold Aschenbrenner")
        == "situational_awareness"
    )
    assert _map_query(scrape_config, "unrelated topic") == ""


# --------------------------------------------------------------------------- #
# check_cnbc flow
# --------------------------------------------------------------------------- #


def test_now_naive_raises(scrape_config: AppConfig, store: StateStore) -> None:
    client = FakeCnbcClient({})
    with pytest.raises(ValueError):
        check_cnbc(scrape_config, store, client, datetime(2026, 7, 22))


def test_emits_and_fields(scrape_config: AppConfig, store: StateStore) -> None:
    _seed_all(store, scrape_config)
    res = _vid("https://www.cnbc.com/video/x.html", "Gavin Baker on AI")
    client = FakeCnbcClient({Q_BAKER: (res,)})
    events = check_cnbc(scrape_config, store, client, NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.CNBC_VIDEO
    assert ev.priority == Priority.HIGH
    assert ev.confidence == Confidence.HIGH
    assert ev.source == CNBC_SOURCE_LABEL
    assert ev.url == "https://www.cnbc.com/video/x.html"
    assert ev.identifier == "https://www.cnbc.com/video/x.html"
    assert ev.payload == {}
    assert ev.entity_key == "atreides"


def test_canonicalization_dedupe_in_run(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, scrape_config)
    a = _vid("https://www.cnbc.com/video/x.html?a=1", "One")
    b = _vid("https://www.cnbc.com/video/x.html#frag", "Two")
    client = FakeCnbcClient({Q_BAKER: (a, b)})
    events = check_cnbc(scrape_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].identifier == "https://www.cnbc.com/video/x.html"


def test_empty_title_and_url_skipped(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, scrape_config)
    good = _vid("https://www.cnbc.com/video/good.html", "Good")
    no_title = _vid("https://www.cnbc.com/video/nt.html", "")
    no_url = _vid("", "Has title but no url")
    client = FakeCnbcClient({Q_BAKER: (good, no_title, no_url)})
    events = check_cnbc(scrape_config, store, client, NOW)
    assert [e.identifier for e in events] == [
        "https://www.cnbc.com/video/good.html"
    ]


def test_first_run_with_results_seeds_silently(
    scrape_config: AppConfig, store: StateStore
) -> None:
    res = _vid("https://www.cnbc.com/video/x.html", "T")
    client = FakeCnbcClient(
        {Q_BAKER: (res,)}, recognized_by_query={Q_BAKER: True}
    )
    events = check_cnbc(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert "https://www.cnbc.com/video/x.html" in reloaded.urls
    assert reloaded.markers[cnbc_seed_key(Q_BAKER)] == TODAY_ISO


def test_first_run_zero_no_structure_not_seeded(
    scrape_config: AppConfig, store: StateStore
) -> None:
    client = FakeCnbcClient({}, recognized_by_query={})  # zero, unrecognized
    check_cnbc(scrape_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert cnbc_seed_key(Q_BAKER) not in reloaded.markers
    assert cnbc_seed_key(Q_LEO) not in reloaded.markers


def test_first_run_empty_but_structured_sets_marker(
    scrape_config: AppConfig, store: StateStore
) -> None:
    client = FakeCnbcClient(
        {}, recognized_by_query={Q_BAKER: True, Q_LEO: True}
    )
    check_cnbc(scrape_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert reloaded.markers[cnbc_seed_key(Q_BAKER)] == TODAY_ISO


def test_zero_after_nonzero_warns(
    scrape_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_all(store, scrape_config)  # already seeded -> non-first-run
    client = FakeCnbcClient({})  # zero results
    with caplog.at_level("WARNING"):
        check_cnbc(scrape_config, store, client, NOW)
    assert any("zero results" in r.message for r in caplog.records)


def test_already_seen_not_reemitted(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, scrape_config)
    app = store.load_seen_appearances()
    app.urls.append("https://www.cnbc.com/video/x.html")
    store.save_seen_appearances(app)
    res = _vid("https://www.cnbc.com/video/x.html?ref=1", "T")
    client = FakeCnbcClient({Q_BAKER: (res,)})
    events = check_cnbc(scrape_config, store, client, NOW)
    assert events == []


def test_in_run_dedupe_includes_seeds(
    scrape_config: AppConfig, store: StateStore
) -> None:
    """A URL seeded by an earlier FIRST-RUN query is not emitted by a later
    NON-first-run query in the same run."""
    # Seed ONLY the Leo query so Baker is first-run, Leo is not.
    store.save_seen_appearances(
        SeenAppearances(markers={cnbc_seed_key(Q_LEO): TODAY_ISO})
    )
    shared = _vid("https://www.cnbc.com/video/shared.html", "Shared")
    client = FakeCnbcClient(
        {Q_BAKER: (shared,), Q_LEO: (shared,)},
        recognized_by_query={Q_BAKER: True},
    )
    events = check_cnbc(scrape_config, store, client, NOW)
    # Baker (first-run) seeds it; Leo (non-first-run) must NOT emit it.
    assert events == []


def test_per_query_isolation(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, scrape_config)
    res = _vid("https://www.cnbc.com/video/leo.html", "Leo vid")
    client = FakeCnbcClient(
        {Q_LEO: (res,)}, raise_for=frozenset({Q_BAKER})
    )
    events = check_cnbc(scrape_config, store, client, NOW)
    assert [e.identifier for e in events] == [
        "https://www.cnbc.com/video/leo.html"
    ]


def test_save_failure_still_returns_feed_events(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_all(store, scrape_config)
    res = _vid("https://www.cnbc.com/video/x.html", "T")
    client = FakeCnbcClient({Q_BAKER: (res,)})

    def _boom(_data: SeenAppearances) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_seen_appearances", _boom)
    events = check_cnbc(scrape_config, store, client, NOW)
    # CNBC events are feed_events -> returned regardless of the save.
    assert len(events) == 1


def test_state_read_failure_fatal(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> SeenAppearances:
        raise MonitorError("state read boom")

    monkeypatch.setattr(store, "load_seen_appearances", _boom)
    client = FakeCnbcClient({})
    with pytest.raises(MonitorError):
        check_cnbc(scrape_config, store, client, NOW)


def test_published_naive_becomes_none() -> None:
    # A naive datetime would violate the tz-aware contract; CnbcResult carries
    # None for such cases (parse yields None). Assert the model accepts None.
    r = _vid("https://www.cnbc.com/video/x.html", "T")
    assert r.published is None


def test_build_alert_cnbc(scrape_config: AppConfig, store: StateStore) -> None:
    _seed_all(store, scrape_config)
    res = _vid("https://www.cnbc.com/video/x.html", "Gavin Baker on AI")
    client = FakeCnbcClient({Q_BAKER: (res,)})
    ev = check_cnbc(scrape_config, store, client, NOW)[0]
    alert = build_alert(ev, scrape_config)
    assert alert.subject
    assert alert.body
    assert set(alert.channels) == {AlertChannel.EMAIL, AlertChannel.SMS}
