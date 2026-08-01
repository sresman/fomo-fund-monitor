from __future__ import annotations

"""Shared pytest fixtures. All fixtures are fully type-annotated (tests are in
mypy strict scope)."""

import shutil
from pathlib import Path
from typing import Callable

import pytest

from config import AppConfig, load_config
from state_manager import StateStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_CONFIG = FIXTURES_DIR / "sample_config.yaml"
FEEDS_CONFIG = FIXTURES_DIR / "feeds_config.yaml"


@pytest.fixture
def feeds_config() -> AppConfig:
    """AppConfig for the FEED monitors (youtube / podcast_rss / google_news)."""
    return load_config(FEEDS_CONFIG)


@pytest.fixture
def scrape_config() -> AppConfig:
    """AppConfig for the SCRAPE/DIFF monitors (cnbc / conference_pages /
    website_diff).

    Reuses ``sample_config.yaml``, which already carries realistic
    ``gavinbaker_net`` (check_rss=false -> WEBSITE_DIFF) + ``situational_awareness_com``
    (check_rss=true -> LEOPOLD_POST) website_diff sites, two conference pages,
    CNBC queries, and the atreides/Gavin Baker + situational_awareness/Leopold
    Aschenbrenner entities the surname mapping relies on."""
    return load_config(SAMPLE_CONFIG)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def store(state_dir: Path) -> StateStore:
    return StateStore(state_dir)


@pytest.fixture
def intervals() -> dict[str, int]:
    return {
        "edgar": 15,
        "youtube": 120,
        "podcast_rss": 30,
        "google_news": 120,
        "cnbc": 360,
        "conference_pages": 1440,
        "website_diff": 1440,
    }


@pytest.fixture
def sample_config_path() -> Path:
    return SAMPLE_CONFIG


@pytest.fixture
def copy_config(tmp_path: Path) -> Callable[[], Path]:
    """Return a helper that copies the fixture config into ``tmp_path`` and
    returns the copy path, so mutation tests never touch the fixture file."""

    def _copy() -> Path:
        dest = tmp_path / "config.yaml"
        shutil.copyfile(SAMPLE_CONFIG, dest)
        return dest

    return _copy
