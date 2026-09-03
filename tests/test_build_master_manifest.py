from __future__ import annotations

"""Tests for ``tools/build_master_manifest.py`` and the manifest loader's
``youtube_url`` support.

The bug being locked out: the manifest is the YouTube dedupe source, but every
``url`` in it was an mp3, so ``load_manifest_youtube_ids`` returned an empty set
and the file contributed nothing -- silently, for the life of the repo.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from monitors.manifest import load_manifest_youtube_ids
from tools.build_master_manifest import build, main


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "celeb-pm"
    _write(
        root / "transcripts/_master_manifest.json",
        [
            {"date": "2026-08-04", "source": "ILTB", "label": "iltb_aug", "url": None},
            {"date": "2019-11-26", "source": "ILTB", "label": "iltb_2019", "url": None},
        ],
    )
    _write(
        root / "transcripts/youtube/_manifest.json",
        [{"id": "NGsi2PC4y68", "label": "iltb_aug"}],
    )
    return root


def test_attaches_youtube_url_from_the_corpus(corpus: Path) -> None:
    rows = build(corpus, existing=[])
    by_label = {r["label"]: r for r in rows}
    assert by_label["iltb_aug"]["youtube_url"] == (
        "https://www.youtube.com/watch?v=NGsi2PC4y68"
    )
    assert "youtube_url" not in by_label["iltb_2019"]


def test_preserves_rows_that_exist_only_in_the_manifest(corpus: Path) -> None:
    """A pure regeneration would silently delete the Boston Investment
    Conference rows, which are real appearances absent from the corpus."""
    existing: list[dict[str, Any]] = [
        {"date": "2022-10-15", "source": "BIC 2022", "label": "bic_2022", "notes": "x"}
    ]
    rows = build(corpus, existing=existing)
    kept = next(r for r in rows if r["label"] == "bic_2022")
    assert kept["notes"] == "x"  # extra fields survive too
    assert len(rows) == 3


def test_existing_url_wins_over_a_corpus_null(corpus: Path) -> None:
    existing: list[dict[str, Any]] = [
        {"label": "iltb_2019", "url": "https://traffic.megaphone.fm/x.mp3"}
    ]
    rows = build(corpus, existing=existing)
    row = next(r for r in rows if r["label"] == "iltb_2019")
    assert row["url"] == "https://traffic.megaphone.fm/x.mp3"


def test_corpus_url_is_not_clobbered_by_an_older_one(corpus: Path) -> None:
    _write(
        corpus / "transcripts/_master_manifest.json",
        [{"date": "2019-11-26", "label": "iltb_2019", "url": "https://new/x.mp3"}],
    )
    rows = build(corpus, existing=[{"label": "iltb_2019", "url": "https://old/x.mp3"}])
    assert rows[0]["url"] == "https://new/x.mp3"


def test_output_is_sorted_and_deterministic(corpus: Path) -> None:
    first = build(corpus, existing=[])
    second = build(corpus, existing=[])
    assert first == second
    assert [r["date"] for r in first] == sorted(r["date"] for r in first)


def test_check_mode_detects_drift_and_writes_nothing(
    corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "manifest.json"
    _write(out, [])
    assert main(["--corpus", str(corpus), "-o", str(out), "--check"]) == 1
    assert json.loads(out.read_text(encoding="utf-8")) == []  # untouched

    assert main(["--corpus", str(corpus), "-o", str(out)]) == 0
    assert main(["--corpus", str(corpus), "-o", str(out), "--check"]) == 0


def test_generated_manifest_actually_feeds_dedupe(
    corpus: Path, tmp_path: Path
) -> None:
    """End-to-end: generate, then load through the real consumer."""
    out = tmp_path / "manifest.json"
    main(["--corpus", str(corpus), "-o", str(out)])
    assert load_manifest_youtube_ids(out) == {"NGsi2PC4y68"}


# --------------------------------------------------------------------------- #
# Loader: youtube_url support
# --------------------------------------------------------------------------- #


def test_loader_reads_youtube_url_field(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    _write(path, [{"youtube_url": "https://www.youtube.com/watch?v=NGsi2PC4y68"}])
    assert load_manifest_youtube_ids(path) == {"NGsi2PC4y68"}


def test_loader_still_reads_the_url_field(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    _write(path, [{"url": "https://youtu.be/NGsi2PC4y68"}])
    assert load_manifest_youtube_ids(path) == {"NGsi2PC4y68"}


def test_loader_reads_both_fields_on_one_row(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    _write(
        path,
        [
            {
                "url": "https://traffic.megaphone.fm/x.mp3",
                "youtube_url": "https://www.youtube.com/watch?v=NGsi2PC4y68",
            }
        ],
    )
    assert load_manifest_youtube_ids(path) == {"NGsi2PC4y68"}


def test_loader_regression_mp3_only_row_yields_nothing(tmp_path: Path) -> None:
    """The original silent failure: an mp3-only manifest deduped nothing."""
    path = tmp_path / "m.json"
    _write(path, [{"url": "https://traffic.megaphone.fm/CLS1681572012.mp3"}])
    assert load_manifest_youtube_ids(path) == set()


def test_loader_rejects_lookalike_hosts_in_youtube_url(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    _write(path, [{"youtube_url": "https://notyoutube.com/watch?v=NGsi2PC4y68"}])
    assert load_manifest_youtube_ids(path) == set()
