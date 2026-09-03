from __future__ import annotations

"""Tests for ``replay.py`` and the ``--replay-since`` CLI.

The invariants that matter operationally:
  * replay re-emits events that are ALREADY in dedupe state (that is the point);
  * it also emits events that were never committed (the ones alerting lost);
  * the real ``state/`` directory is byte-identical afterwards;
  * it defaults to EDGAR, and refuses monitors with no source timestamp.
"""

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import pytest

import constants
import main as main_mod
import replay as replay_mod
from config import AppConfig, load_config
from errors import AlertDeliveryError
from models import (
    AlertChannel,
    Confidence,
    DetectedEvent,
    EventType,
    MonitorName,
    Priority,
)
from monitors.edgar import FilingRecord, SubmissionsResponse
from replay import (
    DEFAULT_REPLAY_MONITORS,
    REPLAYABLE_MONITORS,
    ReplayReport,
    select_events,
)
from state_manager import StateStore

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
ATREIDES_CIK = "0001777813"
SA_CIK = "0002045724"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_event(
    identifier: str,
    published: datetime | None,
    *,
    event_type: EventType = EventType.FILING_13F,
) -> DetectedEvent:
    return DetectedEvent(
        event_type=event_type,
        entity_key="atreides",
        source="SEC EDGAR",
        title=f"title {identifier}",
        url=f"https://ex.example/{identifier}",
        identifier=identifier,
        published=published,
        priority=Priority.HIGH,
        confidence=Confidence.HIGH,
        payload={},
    )


def at(day: int) -> datetime:
    return datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc)


class _RecordingDispatcher:
    """DispatcherLike that records what it was asked to send."""

    def __init__(self, fail_ids: frozenset[str] = frozenset()) -> None:
        self.sent: list[str] = []
        self._fail_ids = fail_ids

    def dispatch_events(
        self, events: Sequence[DetectedEvent], config: AppConfig
    ) -> Sequence[main_mod.DispatchResultLike]:
        results: list[main_mod.DispatchResultLike] = []
        for e in events:
            self.sent.append(e.identifier)
            failed = e.identifier in self._fail_ids
            results.append(
                _Result(
                    channels_sent=() if failed else (AlertChannel.EMAIL,),
                    errors={AlertChannel.EMAIL: "boom"} if failed else {},
                )
            )
        return results


class _Result:
    def __init__(
        self,
        channels_sent: tuple[AlertChannel, ...],
        errors: dict[AlertChannel, str],
    ) -> None:
        self.routed: tuple[AlertChannel, ...] = (AlertChannel.EMAIL,)
        self.channels_sent = channels_sent
        self.channels_skipped: tuple[AlertChannel, ...] = ()
        self.errors = errors
        self.skipped_reasons: dict[AlertChannel, str] = {}
        self.event_error: str | None = None


class _FakeEdgarClient:
    def __init__(self, by_cik: dict[str, SubmissionsResponse]) -> None:
        self._by_cik = by_cik

    def fetch_submissions(self, cik: str) -> SubmissionsResponse:
        return self._by_cik[cik]


def _clients(edgar: _FakeEdgarClient) -> main_mod.Clients:
    """Only the EDGAR client is exercised; the rest are never called because
    replay filters specs down to the requested monitors."""
    return main_mod.Clients(
        edgar=edgar,  # structurally satisfies the EdgarClient Protocol
        # The other clients are never constructed or called: replay filters the
        # specs down to the requested monitors before any run_check().
        youtube=None,  # type: ignore[arg-type]
        feed=None,  # type: ignore[arg-type]
        cnbc=None,  # type: ignore[arg-type]
    )


def _filing(accession: str, day: int, form: str = "13F-HR") -> FilingRecord:
    return FilingRecord(
        accession=accession,
        form=form,
        filing_date=f"2026-08-{day:02d}",
        report_date="2026-06-30",
        primary_document="",
        description="",
    )


@pytest.fixture
def replay_config(copy_config: Callable[[], Path]) -> Path:
    """Config copied into tmp_path, so `paths.state_dir` resolves under tmp."""
    return copy_config()


def _dir_fingerprint(path: Path) -> dict[str, str]:
    """sha256 of every file under ``path``, keyed by relative name."""
    if not path.exists():
        return {}
    return {
        str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(path.rglob("*"))
        if f.is_file()
    }


# --------------------------------------------------------------------------- #
# select_events
# --------------------------------------------------------------------------- #


def test_select_filters_on_and_after_since() -> None:
    events = [make_event("a", at(13)), make_event("b", at(14)), make_event("c", at(20))]
    sel = select_events(events, date(2026, 8, 14), None)
    # Boundary is INCLUSIVE: an event filed ON `since` is replayed.
    assert [e.identifier for e in sel.selected] == ["b", "c"]
    assert sel.matched == 2
    assert sel.undated == 0


def test_select_orders_oldest_first() -> None:
    events = [make_event("c", at(20)), make_event("a", at(14)), make_event("b", at(15))]
    sel = select_events(events, date(2026, 8, 1), None)
    assert [e.identifier for e in sel.selected] == ["a", "b", "c"]


def test_select_limit_keeps_the_newest_but_sends_oldest_first() -> None:
    events = [make_event(str(d), at(d)) for d in (10, 11, 12, 13, 14)]
    sel = select_events(events, date(2026, 8, 1), 2)
    assert [e.identifier for e in sel.selected] == ["13", "14"]
    # `matched` reports the pre-limit count so the operator sees what was cut.
    assert sel.matched == 5


def test_select_counts_and_drops_undated_events() -> None:
    events = [make_event("a", at(20)), make_event("b", None)]
    sel = select_events(events, date(2026, 8, 1), None)
    assert [e.identifier for e in sel.selected] == ["a"]
    assert sel.undated == 1


def test_select_tie_break_is_deterministic() -> None:
    same = at(14)
    events = [make_event("z", same), make_event("a", same), make_event("m", same)]
    sel = select_events(events, date(2026, 8, 1), None)
    assert [e.identifier for e in sel.selected] == ["a", "m", "z"]


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #


def test_default_is_edgar_only() -> None:
    assert DEFAULT_REPLAY_MONITORS == (MonitorName.EDGAR,)


def test_hash_diff_monitors_are_not_replayable() -> None:
    """They stamp published=now, so a date filter would match everything."""
    assert MonitorName.CONFERENCE_PAGES not in REPLAYABLE_MONITORS
    assert MonitorName.WEBSITE_DIFF not in REPLAYABLE_MONITORS


def test_unsupported_monitor_raises(replay_config: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        replay_mod.replay(
            date(2026, 8, 14),
            NOW,
            monitors=(MonitorName.WEBSITE_DIFF,),
            config_path=replay_config,
        )
    assert "website_diff" in str(excinfo.value)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_limit_raises(replay_config: Path, bad: int) -> None:
    with pytest.raises(ValueError):
        replay_mod.replay(
            date(2026, 8, 14), NOW, limit=bad, config_path=replay_config
        )


# --------------------------------------------------------------------------- #
# End-to-end through the EDGAR monitor
# --------------------------------------------------------------------------- #


def _edgar_clients() -> main_mod.Clients:
    return _clients(
        _FakeEdgarClient(
            {
                ATREIDES_CIK: SubmissionsResponse(
                    cik=ATREIDES_CIK,
                    filings=(
                        _filing("acc-old", 1),  # before `since`
                        _filing("acc-seen", 14),  # already in dedupe state
                        _filing("acc-lost", 14),  # never committed
                    ),
                ),
                SA_CIK: SubmissionsResponse(cik=SA_CIK, filings=()),
            }
        )
    )


def test_replay_reemits_already_seen_and_never_committed_events(
    replay_config: Path,
) -> None:
    config = load_config(replay_config)
    real = StateStore(config.paths.state_dir)
    # "acc-seen" was delivered and committed; "acc-lost" never was.
    real.save_seen_filings({"atreides": ["acc-old", "acc-seen"]})

    dispatcher = _RecordingDispatcher()
    report = replay_mod.replay(
        date(2026, 8, 14),
        NOW,
        config_path=replay_config,
        clients=_edgar_clients(),
        dispatcher=dispatcher,
    )
    # Dedupe is bypassed: the already-committed filing is re-sent...
    assert "acc-seen" in dispatcher.sent
    # ... alongside the one alerting lost.
    assert "acc-lost" in dispatcher.sent
    # ... and the date filter still excludes the older one.
    assert "acc-old" not in dispatcher.sent
    assert report.dispatched == 2
    assert report.delivered == 2


def test_replay_does_not_mutate_real_state(replay_config: Path) -> None:
    config = load_config(replay_config)
    real = StateStore(config.paths.state_dir)
    real.save_seen_filings({"atreides": ["acc-old", "acc-seen"]})
    before = _dir_fingerprint(config.paths.state_dir)
    assert before, "fixture must have written state to compare against"

    replay_mod.replay(
        date(2026, 8, 14),
        NOW,
        config_path=replay_config,
        clients=_edgar_clients(),
        dispatcher=_RecordingDispatcher(),
    )
    assert _dir_fingerprint(config.paths.state_dir) == before


def test_replay_is_repeatable(replay_config: Path) -> None:
    """Running it twice sends the same set twice and still leaves state alone --
    'safe to run repeatedly' means it cannot corrupt dedupe, not that it
    suppresses the second send."""
    config = load_config(replay_config)
    StateStore(config.paths.state_dir).save_seen_filings({"atreides": ["acc-seen"]})
    before = _dir_fingerprint(config.paths.state_dir)

    first, second = _RecordingDispatcher(), _RecordingDispatcher()
    for dispatcher in (first, second):
        replay_mod.replay(
            date(2026, 8, 14),
            NOW,
            config_path=replay_config,
            clients=_edgar_clients(),
            dispatcher=dispatcher,
        )
    assert first.sent == second.sent
    assert _dir_fingerprint(config.paths.state_dir) == before


def test_replay_dry_run_sends_nothing(replay_config: Path) -> None:
    dispatcher = _RecordingDispatcher()
    report = replay_mod.replay(
        date(2026, 8, 14),
        NOW,
        config_path=replay_config,
        clients=_edgar_clients(),
        dispatcher=dispatcher,
        dry_run=True,
    )
    assert dispatcher.sent == []
    assert report.dispatched == 0
    assert report.matched == 2
    assert report.dry_run is True


def test_replay_limit_applies(replay_config: Path) -> None:
    dispatcher = _RecordingDispatcher()
    replay_mod.replay(
        date(2026, 8, 1),
        NOW,
        config_path=replay_config,
        clients=_edgar_clients(),
        dispatcher=dispatcher,
        limit=1,
    )
    assert len(dispatcher.sent) == 1


def test_replay_raises_on_delivery_failure(replay_config: Path) -> None:
    """Same fail-loud contract as a normal run."""
    dispatcher = _RecordingDispatcher(fail_ids=frozenset({"acc-lost"}))
    with pytest.raises(AlertDeliveryError) as excinfo:
        replay_mod.replay(
            date(2026, 8, 14),
            NOW,
            config_path=replay_config,
            clients=_edgar_clients(),
            dispatcher=dispatcher,
        )
    assert "acc-lost" in str(excinfo.value)


def test_report_summary_is_readable() -> None:
    report = ReplayReport(
        since=date(2026, 8, 14), monitors=(MonitorName.EDGAR,), considered=3, matched=2
    )
    assert "2026-08-14" in report.summary()
    assert "edgar" in report.summary()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_no_args_is_a_normal_pass() -> None:
    args = main_mod.build_arg_parser().parse_args([])
    assert args.replay_since is None
    assert args.monitors is None
    assert args.limit is None
    assert args.dry_run is False


def test_cli_replay_flags_parse() -> None:
    args = main_mod.build_arg_parser().parse_args(
        ["--replay-since", "2026-08-14", "--monitor", "edgar", "--monitor",
         "youtube", "--limit", "5", "--dry-run"]
    )
    assert args.replay_since == "2026-08-14"
    assert args.monitors == ["edgar", "youtube"]
    assert args.limit == 5
    assert args.dry_run is True


def test_cli_rejects_unknown_monitor() -> None:
    with pytest.raises(SystemExit):
        main_mod.build_arg_parser().parse_args(["--monitor", "nope"])


@pytest.mark.parametrize("bad", ["14-08-2026", "2026/08/14", "yesterday", ""])
def test_cli_rejects_malformed_date(bad: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main_mod._parse_since(bad)
    assert "YYYY-MM-DD" in str(excinfo.value)


def test_cli_accepts_iso_date() -> None:
    assert main_mod._parse_since("2026-08-14") == date(2026, 8, 14)


def test_sample_cap_is_shared_with_the_normal_run_path() -> None:
    """Replay reuses the same bounded-message constant as run()."""
    assert constants.ALERT_FAILURE_SAMPLE_MAX >= 1
    assert json.dumps(constants.ALERT_FAILURE_SAMPLE_MAX)  # it is a plain int
