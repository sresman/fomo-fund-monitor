from __future__ import annotations

"""Tests for state_manager.py (state portion)."""

import json
from pathlib import Path

import pytest

import constants
from errors import StateError
from state_manager import ConferenceSnapshot, DigestEntry, SeenAppearances, StateStore


def test_first_run_seen_filings_empty(store: StateStore, state_dir: Path) -> None:
    assert store.load_seen_filings() == {}
    # No file created on read.
    assert not (state_dir / "seen_filings.json").exists()


def test_first_run_seen_appearances_shape(store: StateStore) -> None:
    app = store.load_seen_appearances()
    assert app.youtube == []
    assert app.rss_guids == []
    assert app.urls == []
    assert app.conference_hashes == {}


def test_fresh_default_not_shared(store: StateStore) -> None:
    a = store.load_seen_appearances()
    b = store.load_seen_appearances()
    assert a is not b
    a.youtube.append("x")
    assert b.youtube == []


@pytest.mark.parametrize("content", ["", "   \n\t "])
def test_empty_file_returns_default(
    store: StateStore, state_dir: Path, content: str
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen_filings.json").write_text(content, encoding="utf-8")
    assert store.load_seen_filings() == {}


def test_corrupt_json_raises(store: StateStore, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen_filings.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.load_seen_filings()


def test_wrong_shape_raises(store: StateStore, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    # youtube must be a list, not a dict.
    (state_dir / "seen_appearances.json").write_text(
        json.dumps({"youtube": {"bad": "shape"}}), encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.load_seen_appearances()


def test_nested_bad_state_value_raises(
    store: StateStore, state_dir: Path
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen_appearances.json").write_text(
        json.dumps(
            {
                "youtube": ["ok", 42],
                "rss_guids": [],
                "urls": [],
                "conference_hashes": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateError):
        store.load_seen_appearances()


def test_mark_and_check_filing(store: StateStore) -> None:
    store.mark_filing_seen("atreides", "acc-1")
    assert store.is_filing_seen("atreides", "acc-1") is True
    assert store.is_filing_seen("atreides", "acc-2") is False
    assert store.is_filing_seen("other", "acc-1") is False


def test_mark_filing_idempotent(store: StateStore) -> None:
    store.mark_filing_seen("atreides", "acc-1")
    store.mark_filing_seen("atreides", "acc-1")
    data = store.load_seen_filings()
    assert data["atreides"] == ["acc-1"]


def test_seen_appearances_roundtrip(
    store: StateStore, state_dir: Path
) -> None:
    store.mark_appearance_seen("youtube", "vid-1")
    store.mark_appearance_seen("rss_guids", "guid-1")
    store.mark_appearance_seen("urls", "https://example.com/x")
    assert store.is_appearance_seen("youtube", "vid-1") is True
    assert store.is_appearance_seen("rss_guids", "guid-1") is True
    assert store.is_appearance_seen("urls", "https://example.com/x") is True

    on_disk = json.loads(
        (state_dir / "seen_appearances.json").read_text(encoding="utf-8")
    )
    assert set(on_disk.keys()) == {
        "youtube",
        "rss_guids",
        "urls",
        "conference_hashes",
        "markers",
    }


def test_conference_snapshot_roundtrip(store: StateStore) -> None:
    snap = ConferenceSnapshot(hash="abc123", text="full page text")
    store.set_conference_snapshot("bic_speakers", snap)
    loaded = store.get_conference_snapshot("bic_speakers")
    assert loaded is not None
    assert loaded.hash == "abc123"
    assert loaded.text == "full page text"
    assert store.get_conference_snapshot("missing") is None


def test_json_written_sorted_and_indented(
    store: StateStore, state_dir: Path
) -> None:
    data = {"zeta": ["1"], "alpha": ["2"]}
    store.save_seen_filings(data)
    content = (state_dir / "seen_filings.json").read_text(encoding="utf-8")
    # sort_keys=True -> alpha before zeta; indent=2 -> two-space indent.
    assert content.index('"alpha"') < content.index('"zeta"')
    assert "\n  " in content
    assert content.endswith("\n")


def test_atomic_write_no_temp_left(
    store: StateStore, state_dir: Path
) -> None:
    store.save_seen_filings({"atreides": ["acc-1"]})
    leftover = list(state_dir.glob("*.tmp"))
    assert leftover == []


def test_save_and_reload_appearances(store: StateStore) -> None:
    app = SeenAppearances(youtube=["a", "b"])
    store.save_seen_appearances(app)
    reloaded = store.load_seen_appearances()
    assert reloaded.youtube == ["a", "b"]


# --------------------------------------------------------------------------- #
# markers field (SD-P4-1)
# --------------------------------------------------------------------------- #


def test_markers_roundtrip(store: StateStore) -> None:
    markers = {
        "seeded:youtube:gavin baker interview": "2026-07-22",
        "youtube_sweep": "2026-07-22",
    }
    store.save_seen_appearances(SeenAppearances(markers=dict(markers)))
    reloaded = store.load_seen_appearances()
    assert reloaded.markers == markers


def test_first_run_appearances_markers_default_empty(store: StateStore) -> None:
    app = store.load_seen_appearances()
    assert app.markers == {}


def test_old_file_without_markers_loads_empty(
    store: StateStore, state_dir: Path
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    # A pre-Prompt-4 file: no `markers` key at all.
    (state_dir / "seen_appearances.json").write_text(
        json.dumps(
            {
                "youtube": ["v1"],
                "rss_guids": [],
                "urls": [],
                "conference_hashes": {},
            }
        ),
        encoding="utf-8",
    )
    app = store.load_seen_appearances()
    assert app.markers == {}
    assert app.youtube == ["v1"]


def test_non_dict_markers_raises(store: StateStore, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen_appearances.json").write_text(
        json.dumps({"markers": [1, 2]}), encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.load_seen_appearances()


def test_non_str_marker_value_raises(store: StateStore, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seen_appearances.json").write_text(
        json.dumps({"markers": {"k": 123}}), encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.load_seen_appearances()


def test_markers_coexist_with_all_buckets(
    store: StateStore, state_dir: Path
) -> None:
    app = SeenAppearances(
        youtube=["v1"],
        rss_guids=["g1"],
        urls=["u1"],
        conference_hashes={"bic": ConferenceSnapshot(hash="h", text="t")},
        markers={"seeded:news:q": "2026-07-22"},
    )
    store.save_seen_appearances(app)
    reloaded = store.load_seen_appearances()
    assert reloaded.youtube == ["v1"]
    assert reloaded.rss_guids == ["g1"]
    assert reloaded.urls == ["u1"]
    assert reloaded.conference_hashes["bic"].hash == "h"
    assert reloaded.markers == {"seeded:news:q": "2026-07-22"}

    on_disk = json.loads(
        (state_dir / "seen_appearances.json").read_text(encoding="utf-8")
    )
    assert set(on_disk.keys()) == {
        "youtube",
        "rss_guids",
        "urls",
        "conference_hashes",
        "markers",
    }


def test_markers_default_fresh_per_read(store: StateStore) -> None:
    a = store.load_seen_appearances()
    b = store.load_seen_appearances()
    assert a.markers is not b.markers
    a.markers["k"] = "v"
    assert b.markers == {}


def test_merge_appearances_merges_conference_hashes(store: StateStore) -> None:
    """One batched merge_appearances applies a conference_hashes update dict on
    top of reloaded state: updating changed keys, ADDING new keys, PRESERVING
    untouched keys AND other buckets (urls/rss_guids) AND markers -- all in one
    save (Prompt 5)."""
    from monitors._common import merge_appearances

    # Seed pre-existing state: buckets, a marker, and two conference hashes.
    initial = SeenAppearances(
        urls=["existing-url"],
        rss_guids=["existing-guid"],
        conference_hashes={
            "conference:untouched": ConferenceSnapshot("h_untouched", "keep me"),
            "conference:changing": ConferenceSnapshot("h_old", "old text"),
        },
        markers={"seeded:other": "2026-01-01"},
    )
    store.save_seen_appearances(initial)

    # A monitor accumulates: a new url seed, a new marker, and a hash update
    # (changing one key, adding a new key).
    fresh = store.load_seen_appearances()
    updates = {
        "conference:changing": ConferenceSnapshot("h_new", "new text"),
        "website:added": ConferenceSnapshot("h_added", "added text"),
    }
    merged = merge_appearances(
        fresh,
        "urls",
        ["new-url"],
        {"seeded:cnbc:q": "2026-07-22"},
        conference_hashes=updates,
    )
    store.save_seen_appearances(merged)

    reloaded = store.load_seen_appearances()
    ch = reloaded.conference_hashes
    # Untouched key preserved.
    assert ch["conference:untouched"].hash == "h_untouched"
    # Changed key updated.
    assert ch["conference:changing"].hash == "h_new"
    assert ch["conference:changing"].text == "new text"
    # New key added.
    assert ch["website:added"].hash == "h_added"
    # Other buckets preserved + new url appended.
    assert reloaded.urls == ["existing-url", "new-url"]
    assert reloaded.rss_guids == ["existing-guid"]
    # Markers preserved + new one applied.
    assert reloaded.markers["seeded:other"] == "2026-01-01"
    assert reloaded.markers["seeded:cnbc:q"] == "2026-07-22"


# --------------------------------------------------------------------------- #
# digest queue
# --------------------------------------------------------------------------- #


def _dentry(identifier: str, title: str = "t") -> DigestEntry:
    return DigestEntry(
        captured_at="2026-09-03T00:00:00+00:00",
        event_type="google_news",
        entity_key="atreides",
        source="Google News",
        title=title,
        url="https://ex.example/x",
        identifier=identifier,
        published="2026-09-01",
    )


def test_digest_queue_roundtrip(store: StateStore) -> None:
    store.append_digest_entries([_dentry("a"), _dentry("b")])
    assert [e.identifier for e in store.load_digest_queue()] == ["a", "b"]


def test_digest_queue_missing_file_is_empty(store: StateStore) -> None:
    assert store.load_digest_queue() == []


def test_digest_queue_dedupes_on_identifier(store: StateStore) -> None:
    store.append_digest_entries([_dentry("a")])
    store.append_digest_entries([_dentry("a"), _dentry("b")])
    assert [e.identifier for e in store.load_digest_queue()] == ["a", "b"]


def test_digest_queue_caps_and_drops_oldest(store: StateStore) -> None:
    """Overflow drops from the FRONT -- the newest week is the one worth
    reading, and the cap keeps the state file bounded if a heartbeat is missed."""
    cap = constants.DIGEST_QUEUE_MAX_ENTRIES
    store.append_digest_entries([_dentry(str(i)) for i in range(cap + 25)])
    queued = store.load_digest_queue()
    assert len(queued) == cap
    assert queued[0].identifier == "25"
    assert queued[-1].identifier == str(cap + 24)


def test_digest_queue_clear(store: StateStore) -> None:
    store.append_digest_entries([_dentry("a")])
    store.clear_digest_queue()
    assert store.load_digest_queue() == []


def test_digest_queue_skips_malformed_rows_without_raising(
    store: StateStore, state_dir: Path
) -> None:
    """One bad row must not break a monitoring run or the heartbeat."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / constants.STATE_FILE_DIGEST_QUEUE).write_text(
        json.dumps(
            [
                {"identifier": "incomplete"},          # missing fields
                "not-an-object",                       # wrong element type
                {f: "v" for f in (
                    "captured_at", "event_type", "entity_key", "source",
                    "title", "url", "identifier", "published")},
            ]
        ),
        encoding="utf-8",
    )
    assert [e.identifier for e in store.load_digest_queue()] == ["v"]


def test_digest_queue_wrong_container_raises(
    store: StateStore, state_dir: Path
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / constants.STATE_FILE_DIGEST_QUEUE).write_text("{}", encoding="utf-8")
    with pytest.raises(StateError):
        store.load_digest_queue()
