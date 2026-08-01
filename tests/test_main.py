from __future__ import annotations

"""Tests for main.py -- the orchestrator.

All collaborators are injected fakes: no network, no real state writes, no
alerts. ``now`` is deterministic. The suite exercises interval gating,
per-monitor isolation, the startup state-probe, record_run-on-failure, the
5-case mark-seen table via the REAL ``build_monitor_specs`` routing, the
Option-A no-op-commit-but-bridge-fires path, website_diff mixed routing,
dry-run (force-no-commit + real-state-untouched + temp cleanup), and the bridge
(fire-after-commit, one retry accounting handled in test_dispatch_bridge,
auth-short-circuit, per-event gating), plus an in-process ``main()`` integration
and the no-side-effect import guard.
"""

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, cast

import pytest

import main
from config import AppConfig, load_config
from errors import DispatchBridgeAuthError, DispatchBridgeError, StateError
from main import MonitorSpec, build_monitor_specs, run
from models import (
    AlertChannel,
    Confidence,
    DetectedEvent,
    EventType,
    MonitorName,
    Priority,
)
from state_manager import AppearanceKind, StateStore

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CONFIG = FIXTURES / "sample_config.yaml"
BRIDGE_ENABLED_CONFIG = FIXTURES / "sample_config_bridge_enabled.yaml"

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def bridge_enabled_config() -> AppConfig:
    return load_config(BRIDGE_ENABLED_CONFIG)


# --------------------------------------------------------------------------- #
# Event + result helpers
# --------------------------------------------------------------------------- #


def make_event(
    *,
    event_type: EventType = EventType.FILING_13F,
    entity_key: str = "atreides",
    identifier: str = "id-1",
) -> DetectedEvent:
    return DetectedEvent(
        event_type=event_type,
        entity_key=entity_key,
        source="src",
        title="t",
        url="https://example.com/x",
        identifier=identifier,
        published=NOW,
        priority=Priority.HIGH,
        confidence=Confidence.HIGH,
        payload={},
    )


class FakeResult:
    """Duck-types DispatchResultLike (reads: errors, event_error)."""

    def __init__(
        self,
        event: DetectedEvent,
        errors: dict[AlertChannel, str] | None = None,
        event_error: str | None = None,
    ) -> None:
        self.event = event
        self.errors: dict[AlertChannel, str] = errors or {}
        self.event_error = event_error


def make_result(
    event: DetectedEvent,
    *,
    errors: dict[AlertChannel, str] | None = None,
    event_error: str | None = None,
) -> FakeResult:
    return FakeResult(event, errors=errors, event_error=event_error)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeStore:
    """In-memory StoreLike. ``should_run`` returns True unless configured; can be
    told to raise (StateError) for a specific monitor."""

    def __init__(
        self,
        *,
        due: set[str] | None = None,
        should_run_raises: dict[str, Exception] | None = None,
        probe_raises: Exception | None = None,
        record_run_raises: set[str] | None = None,
    ) -> None:
        self._due = due  # None => all due
        self._should_run_raises = should_run_raises or {}
        self._probe_raises = probe_raises
        self._record_run_raises = record_run_raises or set()
        self.recorded: list[str] = []
        self.filings_marked: list[tuple[str, str]] = []
        self.appearances_marked: list[tuple[str, str]] = []

    def should_run(
        self, monitor_name: str, now: datetime, intervals: dict[str, int]
    ) -> bool:
        if monitor_name in self._should_run_raises:
            raise self._should_run_raises[monitor_name]
        if self._due is None:
            return True
        return monitor_name in self._due

    def record_run(self, monitor_name: str, now: datetime) -> None:
        if monitor_name in self._record_run_raises:
            raise RuntimeError(f"record_run boom for {monitor_name}")
        self.recorded.append(monitor_name)

    def load_last_run(self) -> dict[str, str]:
        if self._probe_raises is not None:
            raise self._probe_raises
        return {}

    def load_seen_appearances(self) -> object:
        return {}

    def mark_filing_seen(self, entity_key: str, accession: str) -> None:
        self.filings_marked.append((entity_key, accession))

    def mark_appearance_seen(
        self, kind: AppearanceKind, identifier: str
    ) -> None:
        self.appearances_marked.append((kind, identifier))


class FakeDispatcher:
    """DispatcherLike: returns one make_result per event; per-event errors can be
    scripted by identifier."""

    def __init__(self, errors_by_id: dict[str, dict[AlertChannel, str]] | None = None) -> None:
        self._errors_by_id = errors_by_id or {}
        self.dispatched: list[DetectedEvent] = []

    def dispatch_events(
        self, events: Sequence[DetectedEvent], config: AppConfig
    ) -> Sequence[FakeResult]:
        self.dispatched.extend(events)
        return [
            make_result(e, errors=self._errors_by_id.get(e.identifier))
            for e in events
        ]


class FakeBridge:
    """DispatchBridge fake. Records fires; can be scripted to raise per call."""

    def __init__(
        self,
        *,
        pat: bool = True,
        raises: list[Exception | None] | None = None,
    ) -> None:
        self._pat = pat
        self._raises = list(raises) if raises is not None else None
        self.fired: list[tuple[str, str, dict[str, object]]] = []
        self.pat_probes = 0

    def pat_present(self) -> bool:
        self.pat_probes += 1
        return self._pat

    def fire(
        self, repo: str, event_type: str, payload: dict[str, object]
    ) -> None:
        self.fired.append((repo, event_type, payload))
        if self._raises:
            exc = self._raises.pop(0)
            if exc is not None:
                raise exc


def _spec(
    name: MonitorName,
    events: list[DetectedEvent],
    *,
    commit_log: list[tuple[str, str]] | None = None,
    retryable: bool = True,
    raises: Exception | None = None,
) -> MonitorSpec:
    def run_check() -> list[DetectedEvent]:
        if raises is not None:
            raise raises
        return events

    def commit(event: DetectedEvent) -> None:
        if commit_log is not None:
            commit_log.append((name.value, event.identifier))

    return MonitorSpec(
        name=name,
        run_check=run_check,
        commit=commit,
        retryable=lambda _e: retryable,
    )


def _run(
    *,
    config_path: Path = SAMPLE_CONFIG,
    store: FakeStore,
    dispatcher: FakeDispatcher | None = None,
    bridge: FakeBridge | None = None,
    monitors: list[MonitorSpec],
    dry_run: bool = False,
    now: datetime = NOW,
) -> int:
    return run(
        now,
        dry_run=dry_run,
        config_path=config_path,
        store=store,
        dispatcher=dispatcher or FakeDispatcher(),
        bridge=bridge or FakeBridge(pat=False),
        monitors=monitors,
    )


# --------------------------------------------------------------------------- #
# import / no side effects
# --------------------------------------------------------------------------- #


def test_main_importable_no_side_effects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import importlib

    import main as _m

    importlib.reload(_m)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_import_main_pulls_no_network_stack() -> None:
    # Fresh process: importing main must pull neither the twilio nor feedparser
    # stack (deferred imports live inside run()). Checked in a subprocess so a
    # prior in-process real-wiring test cannot pollute sys.modules.
    code = (
        "import sys, main; "
        "assert 'twilio' not in sys.modules, 'twilio imported at module load'; "
        "assert 'feedparser' not in sys.modules, 'feedparser imported at module load'; "
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


# --------------------------------------------------------------------------- #
# happy path + commit + mark-seen
# --------------------------------------------------------------------------- #


def test_run_dispatches_commits_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    commit_log: list[tuple[str, str]] = []
    ev = make_event(identifier="e1")
    store = FakeStore()
    dispatcher = FakeDispatcher()
    spec = _spec(MonitorName.EDGAR, [ev], commit_log=commit_log)
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 0
    assert dispatcher.dispatched == [ev]
    assert commit_log == [("edgar", "e1")]
    assert store.recorded == ["edgar"]


def test_failed_alert_retryable_leaves_uncommitted() -> None:
    commit_log: list[tuple[str, str]] = []
    ev = make_event(identifier="e1")
    store = FakeStore()
    dispatcher = FakeDispatcher(errors_by_id={"e1": {AlertChannel.SMS: "smtp down"}})
    spec = _spec(MonitorName.EDGAR, [ev], commit_log=commit_log, retryable=True)
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 0
    assert commit_log == []  # NOT committed -> re-fires next run
    assert store.recorded == ["edgar"]  # but the monitor still ran


def test_failed_alert_nonretryable_still_commits() -> None:
    commit_log: list[tuple[str, str]] = []
    ev = make_event(identifier="e1")
    store = FakeStore()
    dispatcher = FakeDispatcher(errors_by_id={"e1": {AlertChannel.SMS: "smtp down"}})
    # Non-retryable (Option A / undefined shape) commits even on alert failure.
    spec = _spec(MonitorName.CONFERENCE_PAGES, [ev], commit_log=commit_log, retryable=False)
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 0
    assert commit_log == [("conference_pages", "e1")]


# --------------------------------------------------------------------------- #
# interval gating
# --------------------------------------------------------------------------- #


def test_not_due_monitor_skipped_no_record() -> None:
    store = FakeStore(due=set())  # nothing due
    dispatcher = FakeDispatcher()
    spec = _spec(MonitorName.EDGAR, [make_event()])
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 0
    assert dispatcher.dispatched == []
    assert store.recorded == []  # not due => never ran => no record_run


def test_due_subset_runs_only_due() -> None:
    store = FakeStore(due={"edgar"})
    dispatcher = FakeDispatcher()
    specs = [
        _spec(MonitorName.EDGAR, [make_event(identifier="a")]),
        _spec(MonitorName.YOUTUBE, [make_event(identifier="b")]),
    ]
    rc = _run(store=store, dispatcher=dispatcher, monitors=specs)
    assert rc == 0
    assert store.recorded == ["edgar"]
    assert [e.identifier for e in dispatcher.dispatched] == ["a"]


# --------------------------------------------------------------------------- #
# per-monitor isolation
# --------------------------------------------------------------------------- #


def test_one_monitor_crash_does_not_abort_others() -> None:
    store = FakeStore()
    dispatcher = FakeDispatcher()
    specs = [
        _spec(MonitorName.EDGAR, [], raises=RuntimeError("boom")),
        _spec(MonitorName.YOUTUBE, [make_event(identifier="ok")]),
    ]
    rc = _run(store=store, dispatcher=dispatcher, monitors=specs)
    assert rc == 0  # a per-monitor failure never changes the exit code
    assert [e.identifier for e in dispatcher.dispatched] == ["ok"]


def test_should_run_stateerror_skips_only_that_monitor() -> None:
    store = FakeStore(
        should_run_raises={"edgar": StateError("corrupt last_run for edgar")}
    )
    dispatcher = FakeDispatcher()
    specs = [
        _spec(MonitorName.EDGAR, [make_event(identifier="a")]),
        _spec(MonitorName.YOUTUBE, [make_event(identifier="b")]),
    ]
    rc = _run(store=store, dispatcher=dispatcher, monitors=specs)
    assert rc == 0
    # edgar never ran (should_run raised BEFORE the body) => no dispatch, no record.
    assert [e.identifier for e in dispatcher.dispatched] == ["b"]
    assert store.recorded == ["youtube"]  # edgar not recorded (never reached body)


def test_record_run_happens_even_when_body_raises() -> None:
    store = FakeStore()
    dispatcher = FakeDispatcher()
    spec = _spec(MonitorName.EDGAR, [], raises=RuntimeError("mid-run boom"))
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 0
    # ran=True was set before run_check() raised => record_run in finally.
    assert store.recorded == ["edgar"]


def test_record_run_failure_is_isolated() -> None:
    store = FakeStore(record_run_raises={"edgar"})
    dispatcher = FakeDispatcher()
    spec = _spec(MonitorName.EDGAR, [make_event()])
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 0  # a record_run failure is logged, not fatal


# --------------------------------------------------------------------------- #
# startup state-probe -> fatal exit 2
# --------------------------------------------------------------------------- #


def test_startup_probe_failure_exits_2() -> None:
    store = FakeStore(probe_raises=StateError("corrupt seen_appearances"))
    dispatcher = FakeDispatcher()
    spec = _spec(MonitorName.EDGAR, [make_event()])
    rc = _run(store=store, dispatcher=dispatcher, monitors=[spec])
    assert rc == 2
    assert dispatcher.dispatched == []  # never reached monitors


def test_config_load_failure_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("entities: [unbalanced\n", encoding="utf-8")
    store = FakeStore()
    rc = run(
        NOW,
        config_path=bad,
        store=store,
        dispatcher=FakeDispatcher(),
        bridge=FakeBridge(pat=False),
        monitors=[_spec(MonitorName.EDGAR, [make_event()])],
    )
    assert rc == 2


# --------------------------------------------------------------------------- #
# bridge -- fire after commit, gating, auth short-circuit
# --------------------------------------------------------------------------- #


def test_bridge_fires_after_commit_per_committed_event(
    bridge_enabled_config: AppConfig, tmp_path: Path
) -> None:
    ev = make_event(identifier="e1")
    store = FakeStore()
    dispatcher = FakeDispatcher()
    bridge = FakeBridge(pat=True)
    spec = _spec(MonitorName.EDGAR, [ev])
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        dispatcher=dispatcher,
        bridge=bridge,
        monitors=[spec],
    )
    assert rc == 0
    assert len(bridge.fired) == 1
    repo, event_type, payload = bridge.fired[0]
    assert repo == bridge_enabled_config.dispatch_bridge.repo
    assert event_type == bridge_enabled_config.dispatch_bridge.event_type
    assert payload["schema_version"] == "1"
    assert bridge.pat_probes == 1  # probed ONCE per run


def test_bridge_not_fired_when_disabled() -> None:
    ev = make_event(identifier="e1")
    store = FakeStore()
    bridge = FakeBridge(pat=True)
    spec = _spec(MonitorName.EDGAR, [ev])
    # SAMPLE_CONFIG has dispatch_bridge absent => disabled.
    rc = _run(store=store, bridge=bridge, monitors=[spec])
    assert rc == 0
    assert bridge.fired == []
    assert bridge.pat_probes == 0  # never even probed when disabled


def test_bridge_skipped_when_pat_absent() -> None:
    ev = make_event(identifier="e1")
    store = FakeStore()
    bridge = FakeBridge(pat=False)  # enabled config but no PAT
    spec = _spec(MonitorName.EDGAR, [ev])
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        bridge=bridge,
        monitors=[spec],
    )
    assert rc == 0
    assert bridge.fired == []
    assert bridge.pat_probes == 1  # probed once, found absent, skipped all


def test_bridge_not_fired_for_uncommitted_failed_alert() -> None:
    ev = make_event(identifier="e1")
    store = FakeStore()
    dispatcher = FakeDispatcher(errors_by_id={"e1": {AlertChannel.SMS: "smtp down"}})
    bridge = FakeBridge(pat=True)
    spec = _spec(MonitorName.EDGAR, [ev], retryable=True)
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        dispatcher=dispatcher,
        bridge=bridge,
        monitors=[spec],
    )
    assert rc == 0
    assert bridge.fired == []  # nothing committed => nothing fired


def test_bridge_auth_error_short_circuits_remaining_fires() -> None:
    commit_log: list[tuple[str, str]] = []
    e1 = make_event(identifier="e1")
    e2 = make_event(identifier="e2")
    store = FakeStore()
    dispatcher = FakeDispatcher()
    # First fire raises auth error; the second must NOT be attempted.
    bridge = FakeBridge(pat=True, raises=[DispatchBridgeAuthError("bad pat"), None])
    spec = _spec(MonitorName.EDGAR, [e1, e2], commit_log=commit_log)
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        dispatcher=dispatcher,
        bridge=bridge,
        monitors=[spec],
    )
    assert rc == 0
    assert len(bridge.fired) == 1  # short-circuited after the auth failure
    # Both events still committed (bridge failure never affects mark-seen).
    assert commit_log == [("edgar", "e1"), ("edgar", "e2")]


def test_bridge_transient_error_does_not_crash_run() -> None:
    e1 = make_event(identifier="e1")
    e2 = make_event(identifier="e2")
    store = FakeStore()
    bridge = FakeBridge(pat=True, raises=[DispatchBridgeError("502"), None])
    spec = _spec(MonitorName.EDGAR, [e1, e2])
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        bridge=bridge,
        monitors=[spec],
    )
    assert rc == 0
    # A non-auth error does NOT short-circuit: the second event still fires.
    assert len(bridge.fired) == 2


# --------------------------------------------------------------------------- #
# dry-run: no commits, no bridge, real state untouched, temp cleaned up
# --------------------------------------------------------------------------- #


def test_dry_run_forces_no_commit_no_bridge_no_record() -> None:
    commit_log: list[tuple[str, str]] = []
    ev = make_event(identifier="e1")
    store = FakeStore()
    dispatcher = FakeDispatcher()
    bridge = FakeBridge(pat=True)
    spec = _spec(MonitorName.EDGAR, [ev], commit_log=commit_log)
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        dispatcher=dispatcher,
        bridge=bridge,
        monitors=[spec],
        dry_run=True,
    )
    assert rc == 0
    assert commit_log == []
    assert bridge.fired == []
    assert store.recorded == []  # no record_run in dry-run
    assert dispatcher.dispatched == []  # dispatch short-circuited in dry-run


def test_dry_run_builds_temp_state_and_leaves_real_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a config whose state_dir points at a real dir seeded with a file.
    real_state = tmp_path / "state"
    real_state.mkdir()
    (real_state / "last_run.json").write_text("{}", encoding="utf-8")
    before = sorted(p.name for p in real_state.iterdir())

    # Point a config copy at this state dir.
    import yaml

    raw = yaml.safe_load(SAMPLE_CONFIG.read_text(encoding="utf-8"))
    raw["paths"]["state_dir"] = str(real_state)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    # No injected store => run builds a real StateStore against a temp copy.
    rc = run(
        NOW,
        dry_run=True,
        config_path=cfg_path,
        dispatcher=FakeDispatcher(),
        bridge=FakeBridge(pat=False),
        monitors=[_spec(MonitorName.EDGAR, [make_event()])],
    )
    assert rc == 0
    after = sorted(p.name for p in real_state.iterdir())
    assert after == before  # real state dir untouched by the dry run


# --------------------------------------------------------------------------- #
# REAL build_monitor_specs routing (5-case mark-seen table)
# --------------------------------------------------------------------------- #


def _specs_by_name(
    config: AppConfig, store: StateStore, now: datetime
) -> dict[MonitorName, MonitorSpec]:
    # The clients are bound into check lambdas that these commit-routing tests
    # never invoke, so any object stands in (cast to satisfy the typed fields).
    sentinel = cast("Any", object())
    clients = main.Clients(
        edgar=sentinel, youtube=sentinel, feed=sentinel, cnbc=sentinel
    )
    specs = build_monitor_specs(config, store, clients, now)
    return {s.name: s for s in specs}


def test_build_specs_commit_routing(tmp_path: Path, scrape_config: AppConfig) -> None:
    store = StateStore(tmp_path / "state")
    by_name = _specs_by_name(scrape_config, store, NOW)

    # 1. EDGAR -> seen_filings
    by_name[MonitorName.EDGAR].commit(
        make_event(event_type=EventType.FILING_13F, entity_key="atreides", identifier="ACC1")
    )
    assert store.is_filing_seen("atreides", "ACC1")

    # 2. youtube -> appearances["youtube"]
    by_name[MonitorName.YOUTUBE].commit(
        make_event(event_type=EventType.YOUTUBE_HIGH, identifier="vid1")
    )
    assert store.is_appearance_seen("youtube", "vid1")

    # 3. cnbc / google_news -> appearances["urls"]
    by_name[MonitorName.CNBC].commit(
        make_event(event_type=EventType.CNBC_VIDEO, identifier="cnbc1")
    )
    assert store.is_appearance_seen("urls", "cnbc1")
    by_name[MonitorName.GOOGLE_NEWS].commit(
        make_event(event_type=EventType.GOOGLE_NEWS, identifier="news1")
    )
    assert store.is_appearance_seen("urls", "news1")

    # 4. podcast_rss -> appearances["rss_guids"]
    by_name[MonitorName.PODCAST_RSS].commit(
        make_event(event_type=EventType.PODCAST_RSS, identifier="pod1")
    )
    assert store.is_appearance_seen("rss_guids", "pod1")

    # 5. conference_pages -> NO-OP (Option A)
    by_name[MonitorName.CONFERENCE_PAGES].commit(
        make_event(event_type=EventType.CONFERENCE_CHANGE, identifier="conf1")
    )
    # nothing marked; the identifier appears in no bucket
    assert not store.is_appearance_seen("urls", "conf1")
    assert not store.is_appearance_seen("rss_guids", "conf1")


def test_build_specs_website_diff_mixed_routing(
    tmp_path: Path, scrape_config: AppConfig
) -> None:
    store = StateStore(tmp_path / "state")
    by_name = _specs_by_name(scrape_config, store, NOW)
    wd = by_name[MonitorName.WEBSITE_DIFF]

    # LEOPOLD_POST (Option B) -> rss_guids
    wd.commit(make_event(event_type=EventType.LEOPOLD_POST, identifier="leo1"))
    assert store.is_appearance_seen("rss_guids", "leo1")
    assert wd.retryable(make_event(event_type=EventType.LEOPOLD_POST, identifier="leo1")) is True

    # WEBSITE_DIFF (Option A) -> no-op, one-shot
    wd.commit(make_event(event_type=EventType.WEBSITE_DIFF, identifier="wd1"))
    assert not store.is_appearance_seen("rss_guids", "wd1")
    assert not store.is_appearance_seen("urls", "wd1")
    assert wd.retryable(make_event(event_type=EventType.WEBSITE_DIFF, identifier="wd1")) is False


def test_build_specs_website_diff_unexpected_type_warns(
    tmp_path: Path, scrape_config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    store = StateStore(tmp_path / "state")
    by_name = _specs_by_name(scrape_config, store, NOW)
    wd = by_name[MonitorName.WEBSITE_DIFF]
    # Case-5 WARNING: an undefined event_type for website_diff -> warn + no-op.
    with caplog.at_level(logging.WARNING, logger="fomo_monitor"):
        wd.commit(make_event(event_type=EventType.FILING_13F, identifier="weird1"))
    assert not store.is_appearance_seen("rss_guids", "weird1")
    assert not store.is_appearance_seen("urls", "weird1")
    assert any("unexpected event_type" in r.message for r in caplog.records)


def test_option_a_noop_commit_still_fires_bridge() -> None:
    # A conference_pages event: commit is a no-op, but the bridge STILL fires
    # because the event is considered committed (one-shot, alert clean).
    ev = make_event(event_type=EventType.CONFERENCE_CHANGE, identifier="conf1")
    store = FakeStore()
    bridge = FakeBridge(pat=True)
    spec = _spec(MonitorName.CONFERENCE_PAGES, [ev], retryable=False)
    rc = _run(
        config_path=BRIDGE_ENABLED_CONFIG,
        store=store,
        bridge=bridge,
        monitors=[spec],
    )
    assert rc == 0
    assert len(bridge.fired) == 1  # no-op commit still counts as committed


# --------------------------------------------------------------------------- #
# in-process main() integration + subprocess
# --------------------------------------------------------------------------- #


def test_run_default_wiring_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real wiring (real config + real clients) in DRY-RUN so no commit/alert/bridge,
    # against a temp state dir. Network calls may occur inside monitors but are
    # per-monitor isolated; the orchestrator must still exit 0.
    monkeypatch.delenv("DISPATCH_GITHUB_PAT", raising=False)
    rc = run(NOW, dry_run=True)
    assert rc == 0


def test_main_module_runnable_dry_run() -> None:
    # `main.py` must be importable + runnable via the module. We drive a DRY-RUN
    # in a subprocess (network-safe: no commits, no alerts, no bridge; temp
    # state dir) to confirm the entrypoint wiring works end-to-end without
    # touching the real state/ or sending anything.
    code = (
        "from main import run; "
        "import datetime; "
        "raise SystemExit(run(datetime.datetime.now(datetime.timezone.utc), dry_run=True))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
