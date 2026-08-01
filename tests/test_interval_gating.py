from __future__ import annotations

"""Tests for state_manager.py (interval-gating portion)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from errors import StateError
from state_manager import StateStore

T = datetime(2026, 7, 22, 14, 30, 0, tzinfo=timezone.utc)


def test_first_run_always_due(
    store: StateStore, intervals: dict[str, int]
) -> None:
    assert store.should_run("edgar", T, intervals) is True


def test_not_due_within_interval(
    store: StateStore, intervals: dict[str, int]
) -> None:
    store.record_run("edgar", T)
    assert store.should_run("edgar", T + timedelta(minutes=5), intervals) is False


def test_due_after_interval(
    store: StateStore, intervals: dict[str, int]
) -> None:
    store.record_run("edgar", T)
    # Boundary inclusive: exactly 15 minutes -> due.
    assert store.should_run("edgar", T + timedelta(minutes=15), intervals) is True


def test_boundary_just_before_interval(
    store: StateStore, intervals: dict[str, int]
) -> None:
    store.record_run("edgar", T)
    almost = T + timedelta(minutes=14, seconds=59)
    assert store.should_run("edgar", almost, intervals) is False


def test_record_run_persists(store: StateStore, state_dir: Path) -> None:
    store.record_run("youtube", T)
    on_disk = json.loads(
        (state_dir / "last_run.json").read_text(encoding="utf-8")
    )
    assert on_disk["youtube"] == T.isoformat()


def test_should_run_unknown_monitor_raises(
    store: StateStore, intervals: dict[str, int]
) -> None:
    with pytest.raises(ValueError):
        store.should_run("bogus", T, intervals)


def test_record_run_unknown_monitor_raises(store: StateStore) -> None:
    with pytest.raises(ValueError):
        store.record_run("bogus", T)


def test_should_run_naive_now_rejected(
    store: StateStore, intervals: dict[str, int]
) -> None:
    naive = datetime(2026, 7, 22, 14, 30, 0)
    with pytest.raises(ValueError):
        store.should_run("edgar", naive, intervals)


def test_record_run_naive_now_rejected(store: StateStore) -> None:
    naive = datetime(2026, 7, 22, 14, 30, 0)
    with pytest.raises(ValueError):
        store.record_run("edgar", naive)


def test_naive_persisted_timestamp_raises(
    store: StateStore, state_dir: Path, intervals: dict[str, int]
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last_run.json").write_text(
        json.dumps({"edgar": "2026-07-22T14:30:00"}), encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.should_run("edgar", T, intervals)


def test_non_string_last_run_value_raises(
    store: StateStore, state_dir: Path, intervals: dict[str, int]
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last_run.json").write_text(
        json.dumps({"edgar": 123}), encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.load_last_run()
    with pytest.raises(StateError):
        store.should_run("edgar", T, intervals)


def test_corrupt_last_run_raises(
    store: StateStore, state_dir: Path, intervals: dict[str, int]
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last_run.json").write_text(
        json.dumps({"edgar": "not-a-timestamp"}), encoding="utf-8"
    )
    with pytest.raises(StateError):
        store.should_run("edgar", T, intervals)
