from __future__ import annotations

"""Tests for ``heartbeat.py``.

The load-bearing property: thresholds are calibrated on the OBSERVED ~6/weekday
cadence, not the `*/15` cron spec. A heartbeat that alarmed against 96 runs/day
would fire every week and be ignored -- which is the failure mode it exists to
prevent.
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest
import requests

import constants
import heartbeat
from config import AppConfig, load_config
from heartbeat import HeartbeatReport, MonitorStatus, collect, render
from state_manager import SeenAppearances, StateStore

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _status(
    name: str, age_hours: float | None, *, interval: int = 15
) -> MonitorStatus:
    budget = max(interval, constants.HEARTBEAT_OBSERVED_RUN_GAP_MINUTES)
    stale_after = float(budget * constants.HEARTBEAT_STALE_INTERVAL_MULTIPLIER)
    age = None if age_hours is None else age_hours * 60
    return MonitorStatus(
        name=name,
        last_run=None if age is None else NOW - timedelta(minutes=age),
        age_minutes=age,
        interval_minutes=interval,
        stale_after_minutes=stale_after,
        stale=age is None or age > stale_after,
    )


def _report(**kwargs: object) -> HeartbeatReport:
    base: dict[str, object] = {
        "window_days": 7,
        "window_start": NOW - timedelta(days=7),
        "now": NOW,
        "runs_executed": 40,
        "runs_failed": 0,
        "runs_source": "actions-api",
        "events_committed": 3,
        "monitors": (_status("edgar", 2.0),),
        "problems": (),
    }
    base.update(kwargs)
    return HeartbeatReport(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Thresholds reflect OBSERVED cadence, not the cron spec
# --------------------------------------------------------------------------- #


def test_threshold_is_calibrated_on_observed_cadence_not_cron_spec() -> None:
    """`*/15` implies 96 runs/day = 672/week. The observed rate is ~6/weekday
    (~40/week). The floor must sit below the OBSERVED rate, not the declared
    one, or every heartbeat is a false alarm."""
    cron_implied_weekly = 96 * 7
    observed_weekly = 6 * 5
    assert constants.HEARTBEAT_MIN_RUNS_PER_WINDOW < observed_weekly
    assert constants.HEARTBEAT_MIN_RUNS_PER_WINDOW < cron_implied_weekly / 10


def test_observed_gap_is_hours_not_minutes() -> None:
    """The staleness budget must be driven by the ~4h delivery gap, not by a
    15-minute configured interval that GitHub never honours."""
    assert constants.HEARTBEAT_OBSERVED_RUN_GAP_MINUTES >= 120


def test_fast_monitor_at_realistic_cadence_is_not_stale() -> None:
    """edgar has a 15-minute interval but really runs every ~4h. Six hours
    since the last observation is NORMAL and must not alarm."""
    status = _status("edgar", age_hours=6.0, interval=15)
    assert not status.stale


def test_genuinely_dead_monitor_is_stale() -> None:
    status = _status("edgar", age_hours=72.0, interval=15)
    assert status.stale


def test_never_run_monitor_is_stale() -> None:
    assert _status("cnbc", age_hours=None).stale


# --------------------------------------------------------------------------- #
# Verdict + rendering
# --------------------------------------------------------------------------- #


def test_healthy_when_no_problems() -> None:
    assert _report().healthy


def test_not_healthy_when_problems() -> None:
    assert not _report(problems=("2 run(s) failed",)).healthy


def test_subject_carries_the_verdict_and_headline_numbers() -> None:
    subject, _ = render(_report(runs_executed=41, events_committed=2))
    assert subject.startswith(constants.HEARTBEAT_SUBJECT_PREFIX)
    assert "OK" in subject
    assert "41 runs" in subject
    assert "2 alerts" in subject


def test_subject_says_check_when_unhealthy() -> None:
    subject, _ = render(_report(problems=("monitor edgar last observed 90.0h ago",)))
    assert "CHECK" in subject


def test_body_lists_problems() -> None:
    _, body = render(
        _report(problems=("3 run(s) failed", "monitor cnbc last observed never"))
    )
    assert "PROBLEMS:" in body
    assert "3 run(s) failed" in body
    assert "monitor cnbc last observed never" in body


def test_healthy_body_states_that_silence_is_meaningful() -> None:
    _, body = render(_report())
    assert "Silence" in body
    assert "not a broken monitor" in body


def test_body_flags_the_degraded_run_count_source() -> None:
    _, body = render(_report(runs_source="state-commits", runs_failed=None))
    assert "cannot see failures" in body


def test_body_lists_every_monitor() -> None:
    monitors = (_status("edgar", 2.0), _status("cnbc", None, interval=360))
    _, body = render(_report(monitors=monitors))
    assert "edgar" in body
    assert "cnbc" in body
    assert "never run" in body


# --------------------------------------------------------------------------- #
# collect() against a real git repo
# --------------------------------------------------------------------------- #


# The baseline state commit is BACKDATED to before the heartbeat window, so
# collect() has a real "before" side to diff against. Without this the commit
# lands at wall-clock now, falls inside the window, and every id in state reads
# as newly committed.
BASELINE_COMMIT_DATE = (NOW - timedelta(days=10)).isoformat()


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, env=merged
    )


@pytest.fixture
def git_repo(tmp_path: Path, copy_config: Callable[[], Path]) -> tuple[Path, Path]:
    """A real git repo whose state/ has one baseline commit, so collect() can
    diff against it. Returns (repo_root, config_path)."""
    config_path = copy_config()  # lands in tmp_path
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    config = load_config(config_path)
    store = StateStore(config.paths.state_dir)
    store.save_seen_filings({"atreides": ["baseline-1"]})
    store.save_seen_appearances(SeenAppearances(urls=["u1"]))
    store.save_last_run({"edgar": (NOW - timedelta(hours=2)).isoformat()})
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "baseline state",
        env={
            "GIT_AUTHOR_DATE": BASELINE_COMMIT_DATE,
            "GIT_COMMITTER_DATE": BASELINE_COMMIT_DATE,
        },
    )
    return repo, config_path


def test_collect_counts_newly_committed_events(
    git_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, config_path = git_repo
    monkeypatch.delenv(constants.ENV_GITHUB_TOKEN, raising=False)
    monkeypatch.delenv(constants.ENV_GITHUB_REPOSITORY, raising=False)

    config = load_config(config_path)
    store = StateStore(config.paths.state_dir)
    # Two new filings + one new url land after the baseline commit.
    store.save_seen_filings({"atreides": ["baseline-1", "new-1", "new-2"]})
    store.save_seen_appearances(SeenAppearances(urls=["u1", "u2"]))

    report = collect(NOW, config_path=config_path, repo_root=repo)
    assert report.events_committed == 3
    assert report.runs_source == "state-commits"
    assert report.runs_failed is None


def test_collect_reports_stale_monitors(
    git_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, config_path = git_repo
    monkeypatch.delenv(constants.ENV_GITHUB_TOKEN, raising=False)
    monkeypatch.delenv(constants.ENV_GITHUB_REPOSITORY, raising=False)

    report = collect(NOW, config_path=config_path, repo_root=repo)
    names = {s.name for s in report.monitors}
    # Every configured monitor is reported, not just the ones with a last_run.
    assert names == set(load_config(config_path).monitor_intervals)
    # Only edgar has ever run; the rest are stale-by-absence.
    edgar = next(s for s in report.monitors if s.name == "edgar")
    assert not edgar.stale
    assert any(s.stale for s in report.monitors if s.name != "edgar")
    assert not report.healthy


def test_collect_survives_a_missing_git_repo(
    tmp_path: Path, copy_config: Callable[[], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat that crashed on a git fault would reintroduce the silence it
    exists to prevent."""
    monkeypatch.delenv(constants.ENV_GITHUB_TOKEN, raising=False)
    monkeypatch.delenv(constants.ENV_GITHUB_REPOSITORY, raising=False)
    config_path = copy_config()
    report = collect(NOW, config_path=config_path, repo_root=tmp_path / "not-a-repo")
    assert report.runs_executed == 0
    assert report.events_committed == 0
    assert not report.healthy  # zero runs is itself the finding


def test_actions_api_skipped_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(constants.ENV_GITHUB_TOKEN, raising=False)
    monkeypatch.delenv(constants.ENV_GITHUB_REPOSITORY, raising=False)
    assert heartbeat._actions_run_counts(NOW - timedelta(days=7)) is None


def test_actions_api_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.ENV_GITHUB_TOKEN, "t")
    monkeypatch.setenv(constants.ENV_GITHUB_REPOSITORY, "o/r")

    def _boom(*a: object, **k: object) -> object:
        raise OSError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    assert heartbeat._actions_run_counts(NOW - timedelta(days=7)) is None
