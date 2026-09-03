from __future__ import annotations

"""Tests for monitors/manifest.py (YouTube-id loader). Filesystem via tmp_path;
no network."""

import logging
from pathlib import Path

import pytest

from monitors.manifest import _extract_youtube_id, load_manifest_youtube_ids

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_MANIFEST = FIXTURES_DIR / "master_manifest_sample.json"
REAL_MANIFEST = Path(__file__).parent.parent / "reference" / "master_manifest_v2.json"


def test_missing_file_returns_empty_with_warning(
    tmp_path: Path, caplog: "pytest.LogCaptureFixture"
) -> None:
    with caplog.at_level(logging.WARNING):
        result = load_manifest_youtube_ids(tmp_path / "nope.json")
    assert result == set()
    assert any("not found" in r.message for r in caplog.records)


def test_malformed_json_returns_empty_with_warning(
    tmp_path: Path, caplog: "pytest.LogCaptureFixture"
) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = load_manifest_youtube_ids(p)
    assert result == set()
    assert caplog.records


def test_top_level_not_a_list_returns_empty(
    tmp_path: Path, caplog: "pytest.LogCaptureFixture"
) -> None:
    p = tmp_path / "obj.json"
    p.write_text('{"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = load_manifest_youtube_ids(p)
    assert result == set()
    assert caplog.records


def test_real_manifest_feeds_youtube_dedupe() -> None:
    """INVERTED 2026-09-03. This test previously asserted the real manifest has
    ZERO YouTube urls -- pinning the defect as expected behaviour.

    The manifest IS the YouTube dedupe source, and every url in it was an mp3 or
    a Colossus page, so load_manifest_youtube_ids returned an empty set and the
    file deduped nothing for the life of the repo. It is now generated from the
    celeb-pm corpus by tools/build_master_manifest.py, which attaches a
    youtube_url to every row whose transcript came from YouTube.

    Asserted as a floor, not an exact count, so adding corpus appearances does
    not break the suite.
    """
    ids = load_manifest_youtube_ids(REAL_MANIFEST)
    assert len(ids) >= 30, f"expected the manifest to carry YouTube ids, got {len(ids)}"
    # A known transcribed appearance: ILTB 2026-08-04 "AI Market Jitters".
    assert "NGsi2PC4y68" in ids


def test_sample_manifest_extracts_expected_ids() -> None:
    result = load_manifest_youtube_ids(SAMPLE_MANIFEST)
    assert result == {
        "aaaaaaaaaaa",  # watch?v=
        "bbbbbbbbbbb",  # youtu.be
        "ccccccccccc",  # shorts/
        "ddddddddddd",  # embed/
        "eeeeeeeeeee",  # youtube-nocookie embed
    }
    # False positives excluded.
    assert "fffffffffff" not in result  # notyoutube.com host
    assert "gggggggggggg" not in result  # 12-char id


def test_extract_watch() -> None:
    assert (
        _extract_youtube_id("https://www.youtube.com/watch?v=aaaaaaaaaaa&t=30s")
        == "aaaaaaaaaaa"
    )


def test_extract_youtu_be() -> None:
    assert _extract_youtube_id("https://youtu.be/bbbbbbbbbbb") == "bbbbbbbbbbb"


def test_extract_shorts_and_embed() -> None:
    assert (
        _extract_youtube_id("https://www.youtube.com/shorts/ccccccccccc")
        == "ccccccccccc"
    )
    assert (
        _extract_youtube_id("https://www.youtube.com/embed/ddddddddddd")
        == "ddddddddddd"
    )


def test_extract_nocookie() -> None:
    assert (
        _extract_youtube_id("https://www.youtube-nocookie.com/embed/eeeeeeeeeee")
        == "eeeeeeeeeee"
    )


def test_reject_notyoutube_host() -> None:
    assert _extract_youtube_id("https://notyoutube.com/watch?v=fffffffffff") is None


def test_reject_twelve_char_id() -> None:
    assert (
        _extract_youtube_id("https://www.youtube.com/watch?v=gggggggggggg") is None
    )


def test_reject_ten_char_id() -> None:
    assert _extract_youtube_id("https://www.youtube.com/watch?v=hhhhhhhhhh") is None


def test_reject_v_param_on_non_allowlisted_host() -> None:
    assert _extract_youtube_id("https://evil.com/watch?v=aaaaaaaaaaa") is None


def test_non_dict_and_empty_url_entries_skipped(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(
        '[{"url": ""}, {"nope": 1}, "string-entry", '
        '{"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}]',
        encoding="utf-8",
    )
    assert load_manifest_youtube_ids(p) == {"aaaaaaaaaaa"}
