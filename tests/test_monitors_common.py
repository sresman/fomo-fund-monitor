from __future__ import annotations

"""Tests for monitors/_common.py shared helpers.

The fetch client is tested via an injected fake ``HttpGetter`` (no network, no
requests.Session subclassing). ``parse_feed`` runs against canned fixture bytes.
"""

import sys
from datetime import timezone
from pathlib import Path
from typing import Mapping

import pytest
import requests

from errors import MonitorError
from monitors._common import (
    FeedEntry,
    RequestsFeedClient,
    ResponseLike,
    excerpt,
    matches_keywords,
    merge_appearances,
    news_seed_key,
    parse_feed,
    podcast_seed_key,
    surname_of,
    youtube_seed_key,
)
from state_manager import ConferenceSnapshot, SeenAppearances

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PODCAST_FEED = (FIXTURES_DIR / "sample_podcast_feed.xml").read_bytes()


# --------------------------------------------------------------------------- #
# Fake HttpGetter / ResponseLike (Protocols, not requests subclasses)
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, content: bytes, raise_exc: Exception | None = None) -> None:
        self.content = content
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc


class FakeGetter:
    def __init__(
        self,
        response: ResponseLike | None = None,
        get_exc: Exception | None = None,
    ) -> None:
        self._response = response
        self._get_exc = get_exc
        self.call_count = 0

    def get(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> ResponseLike:
        self.call_count += 1
        if self._get_exc is not None:
            raise self._get_exc
        assert self._response is not None
        return self._response


# --------------------------------------------------------------------------- #
# matches_keywords (per-field)
# --------------------------------------------------------------------------- #


def test_matches_keywords_case_insensitive() -> None:
    assert matches_keywords(("Gavin BAKER interview", ""), ("baker",)) is True


def test_matches_keywords_hit_in_title_only() -> None:
    assert matches_keywords(("Baker chat", "unrelated summary"), ("baker",)) is True


def test_matches_keywords_no_cross_field_match() -> None:
    # "gavin baker" split across two fields must NOT match (per-field only).
    assert matches_keywords(("gavin", "baker"), ("gavin baker",)) is False


def test_matches_keywords_empty_list_false() -> None:
    assert matches_keywords(("anything",), ()) is False


def test_matches_keywords_whitespace_keyword_ignored() -> None:
    assert matches_keywords(("anything",), ("   ",)) is False


# --------------------------------------------------------------------------- #
# excerpt
# --------------------------------------------------------------------------- #


def test_excerpt_under_limit_unchanged() -> None:
    assert excerpt("hello world", 100) == "hello world"


def test_excerpt_collapses_whitespace() -> None:
    assert excerpt("a\n\n  b\t c ", 100) == "a b c"


def test_excerpt_over_limit_capped() -> None:
    assert excerpt("abcdefghij", 4) == "abcd"


def test_excerpt_empty() -> None:
    assert excerpt("", 100) == ""


# --------------------------------------------------------------------------- #
# surname_of
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "person,expected",
    [
        ("Gavin Baker", "baker"),
        ("Leopold Aschenbrenner", "aschenbrenner"),
        ("John Smith Jr.", "smith"),
        ("Jane Doe III", "doe"),
        ("Cher", "cher"),
        ("Bob Jones jr", "jones"),
    ],
)
def test_surname_of(person: str, expected: str) -> None:
    assert surname_of(person) == expected


# --------------------------------------------------------------------------- #
# parse_feed
# --------------------------------------------------------------------------- #


def test_parse_feed_extracts_fields() -> None:
    entries = parse_feed(PODCAST_FEED)
    assert len(entries) == 3
    e1 = entries[0]
    assert e1.guid == "guid-ep1"
    assert e1.title == "Gavin Baker on semiconductors"
    assert e1.link == "https://example.com/iltb/ep1"
    assert "Gavin Baker" in e1.summary
    assert e1.published is not None
    assert e1.published.tzinfo is not None
    assert e1.published.astimezone(timezone.utc).year == 2025
    assert e1.enclosure_url == "https://example.com/audio/ep1.mp3"
    assert e1.source_title == "Invest Like the Best"


def test_parse_feed_guid_falls_back_to_link() -> None:
    entries = parse_feed(PODCAST_FEED)
    e2 = entries[1]  # missing guid
    assert e2.guid == "https://example.com/iltb/ep2"


def test_parse_feed_missing_date_is_none() -> None:
    entries = parse_feed(PODCAST_FEED)
    e3 = entries[2]  # missing pubDate
    assert e3.published is None


def test_parse_feed_malformed_does_not_raise() -> None:
    result = parse_feed(b"<not really xml <<<")
    assert isinstance(result, tuple)


def test_common_import_does_not_import_feedparser() -> None:
    # Importing this test module imported monitors._common; feedparser must not
    # have been pulled in at that point (deferred into parse_feed).
    import monitors._common  # noqa: F401 -- already imported; assert deferral

    # parse_feed has been called above in other tests within the session, so
    # feedparser may be present now; instead assert the module attribute is not a
    # top-level import binding.
    assert not hasattr(monitors._common, "feedparser")


def test_parse_feed_missing_feedparser(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "feedparser":
            raise ModuleNotFoundError("No module named 'feedparser'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delitem(sys.modules, "feedparser", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(MonitorError, match="feedparser not installed"):
        parse_feed(PODCAST_FEED)


# --------------------------------------------------------------------------- #
# RequestsFeedClient (injected HttpGetter)
# --------------------------------------------------------------------------- #


def test_fetch_returns_bytes() -> None:
    getter = FakeGetter(response=FakeResponse(b"payload"))
    client = RequestsFeedClient(http=getter)
    assert client.fetch("https://x") == b"payload"
    assert getter.call_count == 1


def test_fetch_getter_raises_becomes_monitor_error() -> None:
    getter = FakeGetter(get_exc=requests.ConnectionError("boom"))
    client = RequestsFeedClient(http=getter)
    with pytest.raises(MonitorError):
        client.fetch("https://x")
    assert getter.call_count == 1  # no retry


def test_fetch_raise_for_status_raises_becomes_monitor_error() -> None:
    resp = FakeResponse(b"", raise_exc=requests.HTTPError("500"))
    getter = FakeGetter(response=resp)
    client = RequestsFeedClient(http=getter)
    with pytest.raises(MonitorError):
        client.fetch("https://x")
    assert getter.call_count == 1  # no retry


# --------------------------------------------------------------------------- #
# Seed-key helpers
# --------------------------------------------------------------------------- #


def test_seed_keys() -> None:
    assert youtube_seed_key("Gavin Baker interview") == (
        "seeded:youtube:Gavin Baker interview"
    )
    assert youtube_seed_key("  Gavin Baker  ") == "seeded:youtube:Gavin Baker"
    assert podcast_seed_key(" https://x/feed ") == "seeded:podcast:https://x/feed"
    assert news_seed_key(" q ") == "seeded:news:q"


# --------------------------------------------------------------------------- #
# merge_appearances
# --------------------------------------------------------------------------- #


def test_merge_markers_new_keys_win() -> None:
    fresh = SeenAppearances(markers={"a": "1", "b": "1"})
    out = merge_appearances(fresh, None, (), {"b": "2", "c": "2"})
    assert out.markers == {"a": "1", "b": "2", "c": "2"}


def test_merge_bucket_dedup_order_preserving() -> None:
    fresh = SeenAppearances(youtube=["x", "y"])
    out = merge_appearances(fresh, "youtube", ["y", "z", "z"], {})
    assert out.youtube == ["x", "y", "z"]


def test_merge_preserves_other_buckets_and_conf() -> None:
    fresh = SeenAppearances(
        youtube=["v1"],
        rss_guids=["g1"],
        urls=["u1"],
        conference_hashes={"bic": ConferenceSnapshot(hash="h", text="t")},
        markers={"m": "1"},
    )
    out = merge_appearances(fresh, "urls", ["u2"], {"m2": "2"})
    assert out.youtube == ["v1"]
    assert out.rss_guids == ["g1"]
    assert out.urls == ["u1", "u2"]
    assert out.conference_hashes["bic"].hash == "h"
    assert out.markers == {"m": "1", "m2": "2"}


def test_merge_skips_empty_ids() -> None:
    fresh = SeenAppearances()
    out = merge_appearances(fresh, "youtube", ["", "a"], {})
    assert out.youtube == ["a"]


def test_feed_entry_is_frozen() -> None:
    entry = FeedEntry(
        guid="g",
        title="t",
        link="l",
        summary="s",
        published=None,
        enclosure_url="",
        source_title="",
    )
    with pytest.raises(Exception):
        entry.guid = "x"  # type: ignore[misc]
