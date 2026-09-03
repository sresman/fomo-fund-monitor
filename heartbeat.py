from __future__ import annotations

"""Weekly heartbeat: prove the monitor is alive, so silence means "nothing
happened" rather than "it has been broken for three weeks".

This exists because the system ran for 25 days detecting events and delivering
nothing, on jobs GitHub reported as successful. Absence of alerts was
indistinguishable from absence of news. The heartbeat closes that gap by
reporting, once a week:

  * runs executed in the window (and how many FAILED, when the Actions API is
    reachable);
  * events committed -- i.e. alerts actually delivered, since dedupe state is
    only written after a successful dispatch;
  * per-monitor staleness -- when each monitor last successfully observed its
    sources, from ``state/last_run.json``;
  * an explicit healthy / NOT healthy verdict.

THRESHOLDS ARE CALIBRATED ON OBSERVED CADENCE (see the constants block). The
cron says ``*/15`` = 96 runs/day; GitHub actually delivers ~6 per weekday. A
heartbeat that alarmed against the cron spec would cry wolf every week.

Data sources, in order of preference:
  * GitHub Actions API -- exact run count AND failure count. Requires
    ``GITHUB_TOKEN`` + ``GITHUB_REPOSITORY``, both present inside Actions.
  * git history of ``state/`` -- always available, but counts only runs that
    CHANGED state and cannot distinguish a failed run from a successful one.
    Used as the fallback, and the report says so.

Requires full git history: the workflow must check out with ``fetch-depth: 0``
(a shallow clone would report one run per window).
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import requests

import constants
from config import AppConfig, load_config
from errors import AlertError
from state_manager import DigestEntry, StateStore

logger = logging.getLogger("fomo_monitor.heartbeat")

_GIT_TIMEOUT_SECONDS = 30


# --------------------------------------------------------------------------- #
# Report model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MonitorStatus:
    name: str
    last_run: datetime | None
    age_minutes: float | None  # None when the monitor has never run
    interval_minutes: int
    stale_after_minutes: float
    stale: bool


@dataclass(frozen=True)
class HeartbeatReport:
    window_days: int
    window_start: datetime
    now: datetime
    runs_executed: int
    runs_failed: int | None  # None when the Actions API was not consulted
    runs_source: str  # "actions-api" | "state-commits"
    events_committed: int
    monitors: tuple[MonitorStatus, ...]
    problems: tuple[str, ...]
    # Everything captured silently since the last heartbeat (MEDIUM YouTube,
    # google_news, website_diff, filing_other). Never affects `healthy`.
    digest: tuple[DigestEntry, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


def _git(args: list[str], repo_root: Path) -> str:
    """Run a read-only git command. Returns stdout; "" on any failure.

    Never raises: a heartbeat that dies because git was unavailable would
    reintroduce exactly the silence it exists to prevent.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("heartbeat: git %s failed: %s", " ".join(args), exc)
        return ""
    if proc.returncode != 0:
        logger.warning(
            "heartbeat: git %s exited %d: %s",
            " ".join(args),
            proc.returncode,
            proc.stderr.strip()[:200],
        )
        return ""
    return proc.stdout


def _state_commits_since(repo_root: Path, since: datetime) -> int:
    out = _git(
        ["log", f"--since={since.isoformat()}", "--format=%H", "--", "state/"],
        repo_root,
    )
    return len([line for line in out.splitlines() if line.strip()])


def _baseline_commit(repo_root: Path, since: datetime) -> str:
    """Newest state commit at or before the window start (the 'before' side)."""
    out = _git(
        [
            "log",
            f"--until={since.isoformat()}",
            "-1",
            "--format=%H",
            "--",
            "state/",
        ],
        repo_root,
    )
    return out.strip()


def _json_at(repo_root: Path, commit: str, path: str) -> object:
    """``git show <commit>:<path>`` parsed as JSON; ``None`` if absent/invalid."""
    if not commit:
        return None
    out = _git(["show", f"{commit}:{path}"], repo_root)
    if out.strip() == "":
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        logger.warning("heartbeat: %s at %s is not valid JSON", path, commit[:8])
        return None


def _filing_ids(obj: object) -> set[str]:
    if not isinstance(obj, dict):
        return set()
    ids: set[str] = set()
    for entity, accessions in obj.items():
        if isinstance(accessions, list):
            ids |= {f"{entity}:{a}" for a in accessions if isinstance(a, str)}
    return ids


def _appearance_ids(obj: object) -> set[str]:
    if not isinstance(obj, dict):
        return set()
    ids: set[str] = set()
    for bucket in ("youtube", "rss_guids", "urls"):
        values = obj.get(bucket)
        if isinstance(values, list):
            ids |= {f"{bucket}:{v}" for v in values if isinstance(v, str)}
    return ids


def _events_committed_since(
    repo_root: Path, state_dir: Path, since: datetime
) -> int:
    """Count dedupe ids added since the window start.

    Dedupe state is written ONLY after a successful dispatch, so this is a count
    of alerts actually DELIVERED -- the single most meaningful number in the
    report. Compares the baseline commit's state files against the working tree
    by set difference, which is immune to key reordering in the JSON.
    """
    base = _baseline_commit(repo_root, since)
    before = _filing_ids(
        _json_at(repo_root, base, "state/seen_filings.json")
    ) | _appearance_ids(_json_at(repo_root, base, "state/seen_appearances.json"))

    store = StateStore(state_dir)
    try:
        now_ids = _filing_ids(store.load_seen_filings()) | _appearance_ids(
            _appearances_as_dict(store)
        )
    except Exception as exc:  # noqa: BLE001 -- a corrupt state file is a finding
        logger.warning("heartbeat: could not read current state: %s", exc)
        return 0
    return len(now_ids - before)


def _appearances_as_dict(store: StateStore) -> dict[str, object]:
    app = store.load_seen_appearances()
    return {
        "youtube": list(app.youtube),
        "rss_guids": list(app.rss_guids),
        "urls": list(app.urls),
    }


# --------------------------------------------------------------------------- #
# GitHub Actions API (optional enrichment)
# --------------------------------------------------------------------------- #


def _actions_run_counts(since: datetime) -> tuple[int, int] | None:
    """``(executed, failed)`` for the monitoring workflow, or ``None``.

    ``None`` means the API was not consulted or was unreachable -- the caller
    falls back to counting state commits. Never raises.
    """
    token = os.environ.get(constants.ENV_GITHUB_TOKEN, "").strip()
    repo = os.environ.get(constants.ENV_GITHUB_REPOSITORY, "").strip()
    if not token or not repo:
        return None

    url = constants.GITHUB_WORKFLOW_RUNS_URL.format(
        owner_repo=repo, workflow=constants.HEARTBEAT_MONITOR_WORKFLOW_FILE
    )
    headers = {
        "Accept": constants.GITHUB_API_ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": constants.GITHUB_API_VERSION,
        "User-Agent": constants.USER_AGENT,
    }
    executed = 0
    failed = 0
    try:
        for page in range(1, constants.GITHUB_RUNS_MAX_PAGES + 1):
            params: dict[str, str] = {
                "created": f">={since.date().isoformat()}",
                "per_page": str(constants.GITHUB_RUNS_PER_PAGE),
                "page": str(page),
            }
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=constants.HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                logger.warning(
                    "heartbeat: Actions API returned HTTP %d; falling back to "
                    "state-commit counting",
                    response.status_code,
                )
                return None
            payload = response.json()
            runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
            if not isinstance(runs, list) or not runs:
                break
            for run in runs:
                if not isinstance(run, dict):
                    continue
                executed += 1
                if run.get("conclusion") not in ("success", None):
                    failed += 1
            if len(runs) < constants.GITHUB_RUNS_PER_PAGE:
                break
    except Exception as exc:  # noqa: BLE001 -- enrichment must never break the beat
        logger.warning("heartbeat: Actions API query failed: %s", exc)
        return None
    return executed, failed


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


def _monitor_statuses(
    config: AppConfig, now: datetime
) -> tuple[MonitorStatus, ...]:
    store = StateStore(config.paths.state_dir)
    try:
        last_run = store.load_last_run()
    except Exception as exc:  # noqa: BLE001 -- reported as a problem, not a crash
        logger.warning("heartbeat: could not read last_run.json: %s", exc)
        last_run = {}

    statuses: list[MonitorStatus] = []
    for name in sorted(config.monitor_intervals):
        interval = config.monitor_intervals[name]
        # A 15-minute interval cannot beat a ~4-hour delivery cadence, so the
        # staleness budget is driven by whichever is SLOWER.
        budget = max(interval, constants.HEARTBEAT_OBSERVED_RUN_GAP_MINUTES)
        stale_after = float(budget * constants.HEARTBEAT_STALE_INTERVAL_MULTIPLIER)

        raw = last_run.get(name)
        parsed: datetime | None = None
        if raw is not None:
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                logger.warning("heartbeat: unparseable last_run for %s: %r", name, raw)
        age: float | None = None
        if parsed is not None and parsed.tzinfo is not None:
            age = (now - parsed).total_seconds() / 60.0
        statuses.append(
            MonitorStatus(
                name=name,
                last_run=parsed,
                age_minutes=age,
                interval_minutes=interval,
                stale_after_minutes=stale_after,
                stale=age is None or age > stale_after,
            )
        )
    return tuple(statuses)


def collect(
    now: datetime,
    *,
    window_days: int = constants.HEARTBEAT_WINDOW_DAYS,
    config_path: str | Path | None = None,
    repo_root: Path | None = None,
) -> HeartbeatReport:
    """Gather the window's health picture. Never raises on a data-source fault."""
    config = load_config(config_path)
    root = repo_root if repo_root is not None else Path(__file__).resolve().parent
    since = now - timedelta(days=window_days)

    store = StateStore(config.paths.state_dir)
    try:
        digest = tuple(store.load_digest_queue())
    except Exception as exc:  # noqa: BLE001 -- a bad queue must not kill the beat
        logger.warning("heartbeat: could not read the digest queue: %s", exc)
        digest = ()

    counts = _actions_run_counts(since)
    if counts is not None:
        runs_executed, failed_opt = counts[0], counts[1]
        runs_failed: int | None = failed_opt
        runs_source = "actions-api"
    else:
        runs_executed = _state_commits_since(root, since)
        runs_failed = None
        runs_source = "state-commits"

    events = _events_committed_since(root, config.paths.state_dir, since)
    monitors = _monitor_statuses(config, now)

    problems: list[str] = []
    if runs_executed < constants.HEARTBEAT_MIN_RUNS_PER_WINDOW:
        problems.append(
            f"only {runs_executed} run(s) in {window_days} days "
            f"(expected at least {constants.HEARTBEAT_MIN_RUNS_PER_WINDOW} at the "
            f"observed ~6/weekday cadence)"
        )
    if runs_failed:
        problems.append(f"{runs_failed} run(s) failed")
    for status in monitors:
        if status.stale:
            age = (
                "never run"
                if status.age_minutes is None
                else f"{status.age_minutes / 60:.1f}h ago"
            )
            problems.append(
                f"monitor {status.name} last observed {age} "
                f"(budget {status.stale_after_minutes / 60:.1f}h)"
            )

    return HeartbeatReport(
        window_days=window_days,
        window_start=since,
        now=now,
        runs_executed=runs_executed,
        runs_failed=runs_failed,
        runs_source=runs_source,
        events_committed=events,
        monitors=monitors,
        problems=tuple(problems),
        digest=digest,
    )


# --------------------------------------------------------------------------- #
# Rendering + sending
# --------------------------------------------------------------------------- #


def render(report: HeartbeatReport) -> tuple[str, str]:
    """``(subject, body)``. The verdict is in the SUBJECT so it is readable from
    a notification without opening the mail."""
    verdict = "OK" if report.healthy else "CHECK"
    subject = (
        f"{constants.HEARTBEAT_SUBJECT_PREFIX} {verdict} — "
        f"{report.runs_executed} runs, {report.events_committed} alerts "
        f"in {report.window_days}d"
    )

    lines = [
        f"Window: {report.window_start:%Y-%m-%d %H:%M} → "
        f"{report.now:%Y-%m-%d %H:%M} UTC ({report.window_days} days)",
        "",
        f"Runs executed:     {report.runs_executed}"
        + (f"  ({report.runs_failed} failed)" if report.runs_failed is not None else ""),
        f"Alerts delivered:  {report.events_committed}",
        f"Run count source:  {report.runs_source}",
    ]
    if report.runs_source == "state-commits":
        lines.append(
            "  (GitHub Actions API unavailable; this counts runs that CHANGED "
            "state and cannot see failures.)"
        )

    lines += ["", "Per-monitor last successful observation:"]
    for status in report.monitors:
        if status.age_minutes is None:
            age = "never run"
        else:
            age = f"{status.age_minutes / 60:6.1f}h ago"
        flag = "  STALE" if status.stale else ""
        lines.append(
            f"  {status.name:<18} {age:>14}"
            f"   (interval {status.interval_minutes}m){flag}"
        )

    lines += [""]
    if report.healthy:
        lines.append(
            "No problems detected. Silence since the last heartbeat means no "
            "events, not a broken monitor."
        )
    else:
        lines.append("PROBLEMS:")
        lines += [f"  - {p}" for p in report.problems]

    lines += _render_digest(report.digest)

    lines += [
        "",
        "Note: alerts delivered is counted from dedupe-state additions, which "
        "are written only after a successful dispatch. Per-run error detail is "
        "in the GitHub Actions logs.",
    ]
    return subject, "\n".join(lines)


def _digest_sort_key(entry: DigestEntry) -> tuple[str, str]:
    return (entry.published or entry.captured_at, entry.title)


def _render_digest(entries: Sequence[DigestEntry]) -> list[str]:
    """Render the silent-capture digest, grouped by SUBJECT then SOURCE.

    Subject is the tracked entity the event was attributed to; events with no
    entity (a site diff, say) fall into "unattributed". Within a group the items
    are newest-last so the section reads chronologically. Each group is capped at
    ``DIGEST_MAX_PER_GROUP`` with a "+N more" tail, so a catch-up run that
    captures 150 news items cannot turn the digest into an unreadable wall.

    No action is expected from any of this -- it is the recovery path for a
    first-party appearance on a venue that is not allowlisted, and a way to see
    what google_news is holding without it arriving live.
    """
    if not entries:
        return [
            "",
            "Captured silently since the last heartbeat: nothing.",
        ]

    grouped: dict[tuple[str, str], list[DigestEntry]] = {}
    for entry in entries:
        subject = entry.entity_key or "unattributed"
        grouped.setdefault((subject, entry.source or "unknown source"), []).append(entry)

    lines = [
        "",
        "-" * 68,
        f"CAPTURED SILENTLY ({len(entries)} item(s)) — no action expected",
        "Routed to no channel by policy: MEDIUM YouTube, Google News, site "
        "diffs, other filings.",
        "-" * 68,
    ]
    for (subject, source) in sorted(grouped):
        items = sorted(grouped[(subject, source)], key=_digest_sort_key)
        lines.append("")
        lines.append(f"{subject} — {source}  ({len(items)})")
        shown = items[-constants.DIGEST_MAX_PER_GROUP :]
        hidden = len(items) - len(shown)
        if hidden > 0:
            lines.append(f"   … {hidden} older item(s) not shown")
        for item in shown:
            date = (item.published or item.captured_at)[:10]
            title = item.title.strip() or "(untitled)"
            if len(title) > constants.DIGEST_TITLE_MAX_CHARS:
                title = title[: constants.DIGEST_TITLE_MAX_CHARS - 1].rstrip() + "…"
            lines.append(f"   {date}  {title}")
            if item.url:
                lines.append(f"             {item.url}")
    return lines


def send(report: HeartbeatReport, *, config_path: str | Path | None = None) -> None:
    """Email the heartbeat through the real production sender.

    Raises ``AlertError`` if the send fails -- a heartbeat that cannot be
    delivered is itself the alarm, and must fail the workflow.
    """
    from alerting.email_alert import GmailSender
    from alerting.env import resolve_recipient

    config = load_config(config_path)
    subject, body = render(report)
    GmailSender().send(subject, body, resolve_recipient(config.alert_recipients.email_env))


def main(config_path: str | Path | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(config_path)
    report = collect(datetime.now(timezone.utc), config_path=config_path)
    subject, body = render(report)
    logger.info("heartbeat: %s", subject)
    logger.info("\n%s", body)

    # Send FIRST. The queue is drained only once the mail is away, so a send
    # failure loses nothing -- the next heartbeat carries the same items.
    send(report, config_path=config_path)
    if report.digest:
        try:
            StateStore(config.paths.state_dir).clear_digest_queue()
            logger.info(
                "heartbeat: drained %d digest item(s)", len(report.digest)
            )
        except Exception as exc:  # noqa: BLE001 -- a re-sent digest beats a lost one
            logger.error("heartbeat: failed to drain the digest queue: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
