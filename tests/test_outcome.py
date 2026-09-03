from __future__ import annotations

"""Tests for ``monitors/_outcome.py`` and the last_run gating it enables.

Unit tests for ``UnitTally`` plus end-to-end proof that a monitor whose source
units ALL fail raises (so the orchestrator does not advance ``last_run``), while
a monitor that loses only SOME units returns normally.
"""

import urllib.parse
from datetime import datetime, timezone

import pytest

from config import AppConfig
from constants import GOOGLE_NEWS_RSS_URL
from errors import MonitorError
from monitors._common import news_seed_key
from monitors._outcome import UnitTally
from monitors.google_news import check_google_news
from state_manager import SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TODAY_ISO = "2026-07-22"


# --------------------------------------------------------------------------- #
# UnitTally
# --------------------------------------------------------------------------- #


def test_no_units_attempted_never_raises() -> None:
    """A monitor with nothing configured to poll had no outage."""
    UnitTally("m").raise_if_total_failure()  # must not raise


def test_all_units_failed_raises() -> None:
    tally = UnitTally("google_news")
    tally.record_failure()
    tally.record_failure()
    with pytest.raises(MonitorError) as excinfo:
        tally.raise_if_total_failure()
    message = str(excinfo.value)
    assert "google_news" in message
    assert "all 2 source unit(s) failed" in message
    assert "last_run" in message


def test_one_success_is_enough() -> None:
    """A partial outage is still a successful run -- the operator's rule is
    'at least one query succeeded', not 'every query succeeded'."""
    tally = UnitTally("podcast_rss")
    for _ in range(11):
        tally.record_failure()
    tally.record_success()
    tally.raise_if_total_failure()  # must not raise
    assert tally.attempted == 12
    assert tally.succeeded == 1
    assert tally.failed == 11


def test_all_units_succeeded_never_raises() -> None:
    tally = UnitTally("edgar")
    tally.record_success()
    tally.record_success()
    tally.raise_if_total_failure()
    assert tally.attempted == 2


# --------------------------------------------------------------------------- #
# End-to-end through a real monitor
# --------------------------------------------------------------------------- #


def _url_for(query: str) -> str:
    return GOOGLE_NEWS_RSS_URL.format(query=urllib.parse.quote_plus(query))


class _FeedClient:
    """Raises for the URLs in ``raise_for``; serves an empty feed otherwise."""

    def __init__(self, raise_for: frozenset[str]) -> None:
        self._raise_for = raise_for

    def fetch(self, url: str) -> bytes:
        if url in self._raise_for:
            raise MonitorError(f"boom {url}")
        return b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'


def _seed_all(store: StateStore, config: AppConfig) -> None:
    markers = {news_seed_key(q): TODAY_ISO for q in config.google_news.queries}
    store.save_seen_appearances(SeenAppearances(markers=markers))


def test_monitor_raises_when_every_query_fails(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    all_urls = frozenset(_url_for(q) for q in feeds_config.google_news.queries)
    client = _FeedClient(raise_for=all_urls)
    with pytest.raises(MonitorError) as excinfo:
        check_google_news(feeds_config, store, client, NOW)
    assert "all" in str(excinfo.value)
    assert "failed this run" in str(excinfo.value)


def test_monitor_returns_normally_when_one_query_survives(
    feeds_config: AppConfig, store: StateStore
) -> None:
    _seed_all(store, feeds_config)
    queries = list(feeds_config.google_news.queries)
    assert len(queries) > 1, "fixture must have >1 query for this test"
    # All but the last query fail.
    client = _FeedClient(raise_for=frozenset(_url_for(q) for q in queries[:-1]))
    assert check_google_news(feeds_config, store, client, NOW) == []
