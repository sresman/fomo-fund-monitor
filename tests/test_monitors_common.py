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
    is_first_party_appearance,
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


# --- URL stripping + whole-token matching (2026-09-03) --------------------- #
# Every case below is a REAL false positive taken from the configured feeds.
# The audit found 22 historical surname matches and zero genuine appearances.


def test_url_contents_do_not_match() -> None:
    """`emilybakerwhite` in a BuzzFeed link matched "Baker" and alerted a 2022
    This Week in Startups episode about Amazon and TikTok."""
    summary = (
        "Check out BuzzFeed's report on TikTok: "
        "https://www.buzzfeednews.com/article/emilybakerwhite/tiktok-tapes"
    )
    assert matches_keywords(("$AMZN drops Basics", summary), ("Baker",)) is False


def test_his_own_handle_in_a_link_dump_does_not_match() -> None:
    """Six All-In episodes matched on x.com/GavinSBaker in a "Follow the crew"
    block. Being linked is not being on the show."""
    summary = "Follow the crew: https://twitter.com/GavinSBaker https://x.com/chamath"
    assert matches_keywords(("E66: $FB pullback", summary), ("Baker", "Gavin")) is False


def test_www_prefixed_urls_are_stripped_too() -> None:
    assert matches_keywords(("t", "see www.bakerlaw.com/x for more"), ("Baker",)) is False


def test_substring_of_a_longer_word_does_not_match() -> None:
    """"bakeries" and "bakers" matched "Baker" as a bare substring."""
    assert matches_keywords(("t", "small bakeries are winning"), ("Baker",)) is False
    assert matches_keywords(("t", "bakers like a16z backed it"), ("Baker",)) is False


def test_a_different_person_with_the_same_surname_does_not_match() -> None:
    """"Theo Baker's NYT essay" and the "Hobey Baker award" both alerted."""
    assert matches_keywords(("Theo Baker's NYT essay", ""), ("Gavin Baker",)) is False
    assert matches_keywords(("Hobey Baker award winner", ""), ("Gavin Baker",)) is False


def test_a_different_person_with_the_same_forename_does_not_match() -> None:
    """Seven All-In episodes matched "Gavin" on Gavin Newsom."""
    assert matches_keywords(("Newsom's 2028 surge", "Gavin Newsom is the favorite"),
                            ("Gavin Baker",)) is False


def test_genuine_appearances_still_match() -> None:
    """The signal that must survive -- every one of these was a real alert."""
    cases = [
        ("Gavin Baker: Why AI Demand Is Outrunning Compute Supply", ""),
        ("Anthropic's $2T IPO", "(0:00) Gavin Baker joins the show!"),
        ("Liquidity Summit Talks: Antonio Gracias and Gavin Baker | E1990", ""),
        ("Seven Experts", "You'll hear from Gavin Baker, Sarah Tavel, and others"),
    ]
    for title, summary in cases:
        assert matches_keywords((title, summary), ("Gavin Baker",)) is True, title


def test_token_boundary_allows_punctuation_and_hyphens() -> None:
    """Boundaries are non-alphanumeric, so possessives, colons and hyphenated
    run-ins still match -- only alphanumeric run-ons are rejected."""
    assert matches_keywords(("Gavin Baker's view", ""), ("Gavin Baker",)) is True
    assert matches_keywords(("Dario Amodei-Gavin Baker thread", ""),
                            ("Gavin Baker",)) is True
    assert matches_keywords(("Gavin Bakerson", ""), ("Gavin Baker",)) is False


def test_single_token_keywords_still_work() -> None:
    """"Atreides" stays a bare token in config -- distinctive enough to keep."""
    assert matches_keywords(("Atreides Management LP", ""), ("Atreides",)) is True
    assert matches_keywords(("Atreidesian", ""), ("Atreides",)) is False


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


# --------------------------------------------------------------------------- #
# is_first_party_appearance
#
# Every case below is real text from a configured feed. Audited over all feeds:
# 35 keyword matches -> 30 appearances, 5 mentions. No genuine appearance lost.
# --------------------------------------------------------------------------- #

KW = ("Gavin Baker",)


def test_name_in_title_qualifies() -> None:
    assert is_first_party_appearance(
        "Gavin Baker: Why AI Demand Is Outrunning Compute Supply", "", KW
    ) is True


def test_panel_show_with_the_guest_only_in_the_description() -> None:
    """All-In never names guests in the title. Four genuine Baker appearances
    hinge on this path."""
    for summary in (
        "(0:00) Gavin Baker joins the show!",
        "(0:00) Gavin Baker and Travis Kalanick join the show!",
        "(0:00) Brad Gerstner, Gavin Baker, and Kelly Rodriques join the Besties!",
        "(0:00) Antonio Gracias and Gavin Baker join to discuss SpaceX's Starship",
    ):
        assert is_first_party_appearance("E285: AI and markets", summary, KW) is True


def test_stem_matching_covers_inflections() -> None:
    """"join" must cover join/joins/joined/joining -- a stem list with only
    "joins" would have suppressed four real appearances."""
    for verb in ("join", "joins", "joined", "joining"):
        summary = f"Gavin Baker {verb} the show"
        assert is_first_party_appearance("t", summary, KW) is True, verb


def test_show_notes_cross_reference_is_not_an_appearance() -> None:
    """Two ILTB episodes linked to a Baker episode in their chapter list."""
    summary = "(25:42) - Gavin Baker podcast episode  (26:00) - last podcast appearance"
    assert is_first_party_appearance("Matt Ball - The Future of Media", summary, KW) is False


def test_cited_tweet_is_not_an_appearance() -> None:
    """This Week in Startups E2331 matched a show-notes link line."""
    summary = "Dario Amodei-Gavin Baker tweet thread: https://x.com/DarioAmodei/status/1"
    assert is_first_party_appearance(
        "Breaking down Nvidia's Hugging Face and Poolside bets | E2331", summary, KW
    ) is False


def test_clip_retrospective_is_not_an_appearance() -> None:
    summary = (
        "Michael Eisenberg curates the most compelling ideas from our 2025 episodes. "
        "00:46 - Gavin Baker: Why Global Warming Is a Solved Problem"
    )
    assert is_first_party_appearance("Special Episode: A Lookback", summary, KW) is False


def test_a_name_inside_a_url_never_qualifies() -> None:
    summary = "notes https://podcasts.apple.com/us/podcast/gavin-baker-ai-semiconductors joins"
    assert is_first_party_appearance("E: something", summary, KW) is False


def test_framing_far_from_the_name_does_not_qualify() -> None:
    """The stem must be NEAR the name, not merely present somewhere."""
    summary = "Gavin Baker was quoted here. " + ("filler text. " * 40) + "Our guest today is Bob."
    assert is_first_party_appearance("E: something", summary, KW) is False


def test_no_keyword_at_all_is_false() -> None:
    assert is_first_party_appearance("Unrelated", "nothing here", KW) is False
