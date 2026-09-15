"""Tests for ``digest_policy`` -- what earns a line in the weekly digest.

The floor is stated in ``constants`` and read from there, not hardcoded, so
moving it does not silently invalidate these tests. The BOUNDARY cases are
written relative to the constant for the same reason.
"""

from __future__ import annotations

import constants
from digest_policy import duration_of, should_queue
from state_manager import DigestEntry

FLOOR = constants.DIGEST_MIN_DURATION_SECONDS


def entry(
    *,
    event_type: str = "youtube_medium",
    duration_seconds: str = "",
    identifier: str = "v1",
) -> DigestEntry:
    return DigestEntry(
        captured_at="2026-09-15T12:00:00+00:00",
        event_type=event_type,
        entity_key="atreides",
        source="Some Channel",
        title="t",
        url="https://www.youtube.com/watch?v=v1",
        identifier=identifier,
        published="2026-09-14T10:00:00+00:00",
        duration_seconds=duration_seconds,
    )


# --------------------------------------------------------------------------- #
# duration_of
# --------------------------------------------------------------------------- #


def test_duration_of_parses_a_decimal_string() -> None:
    assert duration_of(entry(duration_seconds="4466")) == 4466


def test_duration_of_tolerates_surrounding_whitespace() -> None:
    assert duration_of(entry(duration_seconds="  600 ")) == 600


def test_duration_of_zero_is_zero_not_unknown() -> None:
    """A genuinely zero-length video is BELOW the floor, not unknown."""
    assert duration_of(entry(duration_seconds="0")) == 0


def test_duration_of_unknown_is_none() -> None:
    assert duration_of(entry(duration_seconds="")) is None


def test_duration_of_garbage_is_unknown_not_zero() -> None:
    """Unparseable must degrade to KEEP, so it cannot read as 0 seconds."""
    for bad in ("PT45S", "abc", "12.5", "1e3", "-"):
        assert duration_of(entry(duration_seconds=bad)) is None, bad


def test_duration_of_negative_is_unknown() -> None:
    assert duration_of(entry(duration_seconds="-5")) is None


# --------------------------------------------------------------------------- #
# Event-type filter
# --------------------------------------------------------------------------- #


def test_google_news_never_reaches_the_digest() -> None:
    """Dropped 2026-09-15: ~80% of volume and re-queryable on demand. Its
    duration is irrelevant -- the type alone disqualifies it."""
    assert not should_queue(entry(event_type="google_news"))
    assert not should_queue(
        entry(event_type="google_news", duration_seconds=str(FLOOR * 10))
    )


def test_other_silent_types_do_not_reach_the_digest() -> None:
    for event_type in ("website_diff", "filing_other", "conference_change"):
        assert not should_queue(entry(event_type=event_type)), event_type


def test_youtube_medium_is_the_only_configured_type() -> None:
    """Pins the current policy. If this fails, DIGEST_EVENT_TYPES changed and
    the sensitivity numbers in the workstream doc need recomputing."""
    assert constants.DIGEST_EVENT_TYPES == frozenset({"youtube_medium"})


# --------------------------------------------------------------------------- #
# Duration floor
# --------------------------------------------------------------------------- #


def test_long_form_is_queued() -> None:
    assert should_queue(entry(duration_seconds=str(FLOOR * 3)))


def test_at_the_floor_is_queued() -> None:
    """The floor is inclusive: >= keeps."""
    assert should_queue(entry(duration_seconds=str(FLOOR)))


def test_one_second_under_the_floor_is_dropped() -> None:
    assert not should_queue(entry(duration_seconds=str(FLOOR - 1)))


def test_a_clip_is_dropped() -> None:
    """96s -- the real "Olhar em Redes" Baker clip from 2026-09-12."""
    assert not should_queue(entry(duration_seconds="96"))


def test_unknown_duration_is_kept() -> None:
    """The load-bearing rule: missing metadata must never silently discard the
    recovery case the digest exists for."""
    assert should_queue(entry(duration_seconds=""))


def test_unparseable_duration_is_kept() -> None:
    assert should_queue(entry(duration_seconds="not-a-number"))


# --------------------------------------------------------------------------- #
# Real corpus shapes (measured 2026-09-15)
# --------------------------------------------------------------------------- #


def test_real_captured_items() -> None:
    """Six real rows from the captured corpus, with their true durations.

    The three kept are long-form commentary, not appearances -- the corpus
    contained no genuine unallowlisted first-party appearance to validate
    against. They are here to pin the SHAPE of the cut, not to claim it has
    been proven to surface a real recovery case.
    """
    kept = [
        ("Marktgeflüster Podcast (#216)", "4337"),  # 72m17
        ("【廣東話配音】Gavin Baker：AI 係泡沫定供應追唔上？", "2078"),  # 34m38
        ("Examining the fall of Leopold Aschenbrenner", "1593"),  # 26m33
    ]
    dropped = [
        ("Gavin Baker e o futuro da Inteligência Artificial", "96"),  # 1m36
        ("Gavin Baker：「AI实验室宁可砍掉3600亿美元营收」", "716"),  # 11m56
        ("Aschenbrenner旗下基金重返公开市场 #Shorts", "63"),  # 1m03
    ]
    for title, secs in kept:
        assert should_queue(entry(duration_seconds=secs)), title
    for title, secs in dropped:
        assert not should_queue(entry(duration_seconds=secs)), title
