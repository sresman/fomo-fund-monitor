from __future__ import annotations

"""Tests for ``backfill.py``.

The asymmetry these pin down: suppressing a real alert is worse than sending a
duplicate. So the backfill seeds ONLY what it can prove predates the seed --
strictly older, dated, and on a feed that was actually seeded.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from backfill import backfill_seeds, find_missed
from config import AppConfig, load_config
from monitors._common import podcast_seed_key
from state_manager import SeenAppearances, StateStore

SEED_DAY = "2026-08-09"


def _rss(items: list[tuple[str, str, str]]) -> bytes:
    """(guid, title, RFC-822 pubDate) -> a minimal feed."""
    body = "".join(
        f"<item><title>{t}</title><guid>{g}</guid>"
        f"<pubDate>{p}</pubDate><description>d</description></item>"
        for g, t, p in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>F</title>{body}</channel></rss>"
    ).encode()


class _FeedClient:
    def __init__(self, by_url: dict[str, bytes], fail: frozenset[str] = frozenset()):
        self._by_url = by_url
        self._fail = fail
        self.fetched: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        if url in self._fail:
            raise OSError(f"boom {url}")
        return self._by_url.get(url, _rss([]))


@pytest.fixture
def cfg(copy_config: Callable[[], Path]) -> tuple[AppConfig, Path]:
    path = copy_config()
    return load_config(path), path


def _seed_markers(config: AppConfig, store: StateStore, *, guids: list[str]) -> None:
    """Mark every podcast feed as seeded on SEED_DAY."""
    markers = {
        podcast_seed_key(f.url): SEED_DAY
        for f in config.podcast_rss.feeds
        if f.url.strip()
    }
    store.save_seen_appearances(SeenAppearances(rss_guids=guids, markers=markers))


def _first_feed_url(config: AppConfig) -> str:
    return next(f.url for f in config.podcast_rss.feeds if f.url.strip())


def test_seeds_strictly_older_matching_entry(cfg: tuple[AppConfig, Path]) -> None:
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-1", "Gavin Baker on AI", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    report = backfill_seeds(config_path=path, store=store, feed_client=client)
    assert [m.guid for m in report.missed] == ["old-1"]
    assert report.seeded == 1
    assert "old-1" in store.load_seen_appearances().rss_guids


def test_does_not_seed_entries_on_or_after_the_seed_date(
    cfg: tuple[AppConfig, Path]
) -> None:
    """These are legitimately new and MUST still alert. On the seed date itself
    ordering is ambiguous, so it is left alone too."""
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient(
        {
            url: _rss(
                [
                    ("same-day", "Baker same day", "Sat, 09 Aug 2026 12:00:00 +0000"),
                    ("newer", "Baker newer", "Fri, 14 Aug 2026 12:00:00 +0000"),
                ]
            )
        }
    )
    report = backfill_seeds(config_path=path, store=store, feed_client=client)
    assert report.missed == []
    assert store.load_seen_appearances().rss_guids == []


def test_never_seeds_an_undated_entry(cfg: tuple[AppConfig, Path]) -> None:
    """An entry that cannot be proven older than the seed is left to alert."""
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient({url: _rss([("undated", "Baker undated", "not-a-date")])})
    report = backfill_seeds(config_path=path, store=store, feed_client=client)
    assert report.missed == []
    assert report.undated_skipped == 1
    assert store.load_seen_appearances().rss_guids == []


def test_ignores_non_matching_entries(cfg: tuple[AppConfig, Path]) -> None:
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-x", "Unrelated episode", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    assert backfill_seeds(config_path=path, store=store, feed_client=client).missed == []


def test_skips_feeds_that_were_never_seeded(cfg: tuple[AppConfig, Path]) -> None:
    """No marker => genuinely first-run; the monitor's own seeding handles it.
    (Live case: the Dwarkesh feed has never fetched successfully.)"""
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    store.save_seen_appearances(SeenAppearances())  # no markers at all
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-1", "Gavin Baker", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    report = backfill_seeds(config_path=path, store=store, feed_client=client)
    assert report.missed == []
    assert report.scanned_feeds == 0
    assert report.skipped_unseeded


def test_dry_run_writes_nothing(cfg: tuple[AppConfig, Path]) -> None:
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-1", "Gavin Baker", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    report = backfill_seeds(
        config_path=path, store=store, feed_client=client, dry_run=True
    )
    assert len(report.missed) == 1
    assert report.seeded == 0
    assert store.load_seen_appearances().rss_guids == []


def test_is_idempotent(cfg: tuple[AppConfig, Path]) -> None:
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-1", "Gavin Baker", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    first = backfill_seeds(config_path=path, store=store, feed_client=client)
    second = backfill_seeds(config_path=path, store=store, feed_client=client)
    assert first.seeded == 1
    assert second.seeded == 0
    assert second.missed == []
    assert store.load_seen_appearances().rss_guids == ["old-1"]


def test_is_purely_additive(cfg: tuple[AppConfig, Path]) -> None:
    """Existing ids and markers must survive untouched."""
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=["pre-existing"])
    before_markers = dict(store.load_seen_appearances().markers)
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-1", "Gavin Baker", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    backfill_seeds(config_path=path, store=store, feed_client=client)
    after = store.load_seen_appearances()
    assert "pre-existing" in after.rss_guids
    assert "old-1" in after.rss_guids
    assert after.markers == before_markers




def test_find_missed_is_read_only(cfg: tuple[AppConfig, Path]) -> None:
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    url = _first_feed_url(config)
    client = _FeedClient(
        {url: _rss([("old-1", "Gavin Baker", "Mon, 19 Jun 2023 12:00:00 +0000")])}
    )
    find_missed(config, store, client)
    assert store.load_seen_appearances().rss_guids == []


def test_dead_feed_is_recorded_not_raised(cfg: tuple[AppConfig, Path]) -> None:
    """A feed that fails to fetch is reported, never fatal -- the backfill must
    not abort partway and leave the operator guessing what it managed to do."""
    config, path = cfg
    store = StateStore(config.paths.state_dir)
    _seed_markers(config, store, guids=[])
    client = _FeedClient({}, fail=frozenset({_first_feed_url(config)}))
    report = backfill_seeds(config_path=path, store=store, feed_client=client)
    assert report.fetch_errors
    assert report.missed == []
    assert report.seeded == 0


def test_website_diff_rss_sites_are_out_of_scope(cfg: tuple[AppConfig, Path]) -> None:
    """Deliberate exclusion, pinned so it is not "fixed" by accident.

    _check_rss_site fetches <site.url>/feed, restricts to LEOPOLD_POST, and gates
    on a valid-feed check. Backfilling those here would mean a second copy of all
    three rules, and getting one wrong would permanently suppress real posts.
    """
    from backfill import _sources

    config, _ = cfg
    site_urls = {s.url for s in config.website_diff}
    assert site_urls, "fixture must have website_diff sites"
    assert not (site_urls & {s.url for s in _sources(config)})
