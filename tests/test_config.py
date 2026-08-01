from __future__ import annotations

"""Tests for config.py (the typed config loader)."""

from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

import config
from config import load_config
from errors import ConfigError
from models import AlertChannel, EventType


def _load_raw(path: Path) -> dict[str, Any]:
    """Load a config file into a plain dict for mutation. Returns Any-typed
    values on purpose (test-side manipulation of arbitrary YAML shapes)."""
    text = path.read_text(encoding="utf-8")
    data: Any = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _write_config(tmp_path: Path, data: object) -> Path:
    dest = tmp_path / "config.yaml"
    dest.write_text(yaml.safe_dump(data), encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- #
# Valid loads
# --------------------------------------------------------------------------- #


def test_load_valid_sample_config(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    assert len(cfg.entities) == 2
    by_key = {e.key: e for e in cfg.entities}
    assert by_key["atreides"].cik == "0001777813"
    assert by_key["situational_awareness"].cik == "0002045724"
    assert cfg.monitor_intervals["edgar"] == 15
    assert len(cfg.monitor_intervals) == 7
    assert set(cfg.youtube.queries_by_entity.keys()) == {
        "atreides",
        "situational_awareness",
    }
    # alert_routing keyed by EventType members -> AlertChannel tuples
    assert cfg.alert_routing[EventType.FILING_13F] == (
        AlertChannel.EMAIL,
        AlertChannel.SMS,
    )
    assert cfg.alert_routing[EventType.GOOGLE_NEWS] == (AlertChannel.EMAIL,)


def test_master_manifest_path_resolved_absolute(
    tmp_path: Path, copy_config: Callable[[], Path]
) -> None:
    cfg_path = copy_config()
    cfg = load_config(cfg_path)
    expected = (tmp_path / "reference" / "master_manifest_v2.json").resolve()
    assert cfg.youtube.master_manifest_path == expected
    assert cfg.youtube.master_manifest_path.is_absolute()


def test_load_real_repo_config() -> None:
    # Loads the REAL repo-root config.yaml via default module-anchored resolution.
    cfg = load_config()
    assert len(cfg.entities) >= 1
    real_path = (Path(config.__file__).parent / "config.yaml").resolve()
    assert real_path.exists()


def test_paths_resolved_absolute(
    tmp_path: Path, copy_config: Callable[[], Path]
) -> None:
    cfg_path = copy_config()
    cfg = load_config(cfg_path)
    assert cfg.paths.state_dir == (tmp_path / "state").resolve()
    assert cfg.paths.state_dir.is_absolute()
    assert cfg.paths.reference_dir == (tmp_path / "reference").resolve()


# --------------------------------------------------------------------------- #
# Missing / wrong-type
# --------------------------------------------------------------------------- #


def test_missing_required_key_raises(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    del data["entities"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_wrong_type_raises(tmp_path: Path, sample_config_path: Path) -> None:
    data = _load_raw(sample_config_path)
    data["monitor_intervals"]["edgar"] = "soon"
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# CIK parsing
# --------------------------------------------------------------------------- #


def test_cik_zero_padding_from_string(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["cik"] = "1777813"
    cfg_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path)
    assert cfg.entities[0].cik == "0001777813"


def test_cik_from_yaml_integer(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["cik"] = 1777813
    cfg_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path)
    assert cfg.entities[0].cik == "0001777813"


def test_cik_rejects_bool(tmp_path: Path, sample_config_path: Path) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["cik"] = True
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_cik_rejects_empty_string(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["cik"] = ""
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


@pytest.mark.parametrize("value", ["0000000000", "0"])
def test_cik_rejects_all_zeros(
    tmp_path: Path, sample_config_path: Path, value: str
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["cik"] = value
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# monitor_intervals exact-set
# --------------------------------------------------------------------------- #


def test_missing_interval_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    del data["monitor_intervals"]["website_diff"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_extra_interval_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["monitor_intervals"]["bogus_monitor"] = 10
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_empty_monitor_intervals_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["monitor_intervals"] = {}
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# alert_routing exact-set + channels
# --------------------------------------------------------------------------- #


def test_alert_routing_missing_event_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    del data["alert_routing"]["website_diff"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_unknown_alert_routing_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["alert_routing"]["telegram_blast"] = ["email"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_unknown_alert_channel_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["alert_routing"]["filing_13f"] = ["carrier_pigeon"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# unknown keys
# --------------------------------------------------------------------------- #


def test_unknown_top_level_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["bogus_section"] = {"foo": "bar"}
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_unknown_nested_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["stray"] = "value"
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_non_str_yaml_key_rejected(tmp_path: Path) -> None:
    # A mapping with a non-string (integer) key inside a nested map.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "entities:\n"
        "  - key: a\n"
        "    name: A\n"
        "    person: P\n"
        "    cik: '1'\n"
        "    filing_types: ['4']\n"
        "    1: oops\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# uniqueness
# --------------------------------------------------------------------------- #


def test_duplicate_entity_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][1]["key"] = data["entities"][0]["key"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_duplicate_entity_cik_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    # Same CIK after zero-pad normalization ("1777813" -> "0001777813").
    data["entities"][1]["cik"] = "1777813"
    data["entities"][0]["cik"] = "0001777813"
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_duplicate_conference_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["conference_pages"][1]["key"] = data["conference_pages"][0]["key"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_duplicate_website_diff_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["website_diff"][1]["key"] = data["website_diff"][0]["key"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# non-empty checks
# --------------------------------------------------------------------------- #


def test_empty_entity_name_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["entities"][0]["name"] = ""
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_empty_query_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["google_news"]["queries"][0] = ""
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_empty_env_var_name_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["alert_recipients"]["email_env"] = ""
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# youtube entity-key validation
# --------------------------------------------------------------------------- #


def test_known_channels_and_framing_loaded(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    assert cfg.youtube.known_channels == ("All-In Podcast", "Bg2 Pod")
    assert cfg.youtube.framing_keywords == ("interview", "podcast")


def test_optional_youtube_lists_absent_default_empty(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    del data["youtube"]["known_channels"]
    del data["youtube"]["framing_keywords"]
    cfg_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path)
    assert cfg.youtube.known_channels == ()
    assert cfg.youtube.framing_keywords == ()


def test_optional_youtube_lists_empty_allowed(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"]["known_channels"] = []
    data["youtube"]["framing_keywords"] = []
    cfg_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path)
    assert cfg.youtube.known_channels == ()
    assert cfg.youtube.framing_keywords == ()


@pytest.mark.parametrize("field", ["known_channels", "framing_keywords"])
def test_optional_youtube_list_null_rejected(
    tmp_path: Path, sample_config_path: Path, field: str
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"][field] = None
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


@pytest.mark.parametrize("field", ["known_channels", "framing_keywords"])
def test_optional_youtube_list_scalar_rejected(
    tmp_path: Path, sample_config_path: Path, field: str
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"][field] = 5
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


@pytest.mark.parametrize("field", ["known_channels", "framing_keywords"])
def test_optional_youtube_list_empty_item_rejected(
    tmp_path: Path, sample_config_path: Path, field: str
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"][field] = ["", "x"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


@pytest.mark.parametrize("field", ["known_channels", "framing_keywords"])
def test_optional_youtube_list_non_str_item_rejected(
    tmp_path: Path, sample_config_path: Path, field: str
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"][field] = ["x", 1]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_unknown_youtube_key_still_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"]["bogus_key"] = ["x"]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_youtube_key_not_matching_entity_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"]["queries_by_entity"]["nonexistent_entity"] = {
        "broad_queries": ["x"],
        "sweep_queries": ["y"],
    }
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #


def test_max_results_per_query_must_be_positive(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["youtube"]["max_results_per_query"] = 0
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


@pytest.mark.parametrize("bad_month", [0, 13, -1])
def test_season_month_out_of_range_rejected(
    tmp_path: Path, sample_config_path: Path, bad_month: int
) -> None:
    data = _load_raw(sample_config_path)
    data["conference_pages"][0]["season_months"] = [bad_month]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_season_month_duplicate_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["conference_pages"][0]["season_months"] = [9, 9]
    cfg_path = _write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_empty_feed_url_allowed(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    # sample already has one empty feed url; assert it loads without error.
    cfg_path = _write_config(tmp_path, data)
    cfg = load_config(cfg_path)
    assert any(feed.url == "" for feed in cfg.podcast_rss.feeds)


# --------------------------------------------------------------------------- #
# file-level errors
# --------------------------------------------------------------------------- #


def test_missing_config_file_wrapped() -> None:
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.yaml")


def test_malformed_yaml_wrapped(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("entities: [unbalanced\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg_path)


@pytest.mark.parametrize("content", ["", "   \n\t  \n"])
def test_empty_config_file_wrapped(tmp_path: Path, content: str) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# dispatch_bridge (Prompt 6)
# --------------------------------------------------------------------------- #

from constants import DEFAULT_DISPATCH_EVENT_TYPE, DISPATCH_EVENT_TYPE_MAX_CHARS


def test_dispatch_bridge_absent_defaults_disabled(
    tmp_path: Path, sample_config_path: Path
) -> None:
    # The sample fixture has NO dispatch_bridge section: absent => inert default.
    data = _load_raw(sample_config_path)
    assert "dispatch_bridge" not in data
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.dispatch_bridge.enabled is False
    assert cfg.dispatch_bridge.repo == ""
    assert cfg.dispatch_bridge.event_type == DEFAULT_DISPATCH_EVENT_TYPE


def test_dispatch_bridge_enabled_valid(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {
        "enabled": True,
        "repo": "collectifadv/celeb-pm",
        "event_type": "fomo_monitor_event",
    }
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.dispatch_bridge.enabled is True
    assert cfg.dispatch_bridge.repo == "collectifadv/celeb-pm"
    assert cfg.dispatch_bridge.event_type == "fomo_monitor_event"


def test_dispatch_bridge_event_type_defaulted_when_omitted(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {"enabled": True, "repo": "owner/name"}
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.dispatch_bridge.event_type == DEFAULT_DISPATCH_EVENT_TYPE


def test_dispatch_bridge_present_null_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    # Present-but-null is DISTINCT from absent: the strict builder runs and
    # rejects a non-mapping value.
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = None
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_dispatch_bridge_blank_repo_rejected_when_enabled(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {"enabled": True, "repo": "   "}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_dispatch_bridge_nonstr_repo_rejected_even_when_disabled(
    tmp_path: Path, sample_config_path: Path
) -> None:
    # repo must ALWAYS be a str, even when disabled.
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {"enabled": False, "repo": 123}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


@pytest.mark.parametrize(
    "bad_repo",
    ["ownernoname", "owner/name/extra", "owner /name", "owner/ name", "/name", "owner/"],
)
def test_dispatch_bridge_malformed_repo_rejected(
    tmp_path: Path, sample_config_path: Path, bad_repo: str
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {"enabled": True, "repo": bad_repo}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_dispatch_bridge_event_type_too_long_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {
        "enabled": True,
        "repo": "owner/name",
        "event_type": "x" * (DISPATCH_EVENT_TYPE_MAX_CHARS + 1),
    }
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_dispatch_bridge_blank_event_type_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {
        "enabled": True,
        "repo": "owner/name",
        "event_type": "   ",
    }
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_dispatch_bridge_unknown_key_rejected(
    tmp_path: Path, sample_config_path: Path
) -> None:
    data = _load_raw(sample_config_path)
    data["dispatch_bridge"] = {"enabled": False, "repo": "", "bogus": 1}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))
