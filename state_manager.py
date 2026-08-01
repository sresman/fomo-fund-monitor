from __future__ import annotations

"""State manager + interval gating for fomo-fund-monitor.

Handles the three state files under ``config.paths.state_dir`` via a thin
``StateStore`` class constructed with a ``state_dir: Path`` (so tests point at
``tmp_path`` with no hidden globals). Interval-gating methods (``should_run`` /
``record_run``) live here because they read/write ``last_run.json``.

Read policy (text-first): a missing OR empty/whitespace-only/0-byte file is a
benign first-run and returns a fresh default -- ``json.loads`` is never called on
blank content. Non-empty invalid JSON, wrong container/element shape, or an
unparseable/naive/non-string persisted timestamp raises ``StateError`` (never
silently reset, which would re-alert on everything already seen).

Write policy (atomic): serialize to ``<name>.tmp`` in ``state_dir``, then
``os.replace`` onto the target, wrapped in try/finally that removes the temp on
failure. Prevents partial/torn target files. No fsync/durability claim.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Literal

import constants
from errors import StateError

AppearanceKind = Literal["youtube", "rss_guids", "urls"]

# Typed representations of the on-disk shapes.
SeenFilings = dict[str, list[str]]
LastRun = dict[str, str]  # monitor -> ISO timestamp; plain alias (no NewType)


@dataclass
class ConferenceSnapshot:
    hash: str
    text: str


@dataclass
class SeenAppearances:
    youtube: list[str] = field(default_factory=list)
    rss_guids: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    conference_hashes: dict[str, ConferenceSnapshot] = field(default_factory=dict)
    # Seeding / scheduling metadata (SD-P4-1). NOT appearance ids; the dedupe
    # buckets and ``mark_appearance_seen`` never touch this. Maps a marker key
    # (str) -> a small str value (an ISO date for seed/sweep markers).
    markers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# JSON boundary narrowing helpers (StateError on any violation)
# --------------------------------------------------------------------------- #


def _as_dict(v: object, ctx: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise StateError(f"{ctx}: expected an object, got {type(v).__name__}")
    result: dict[str, object] = {}
    for k, val in v.items():
        if not isinstance(k, str):
            raise StateError(
                f"{ctx}: object keys must be strings, got {type(k).__name__}"
            )
        result[k] = val
    return result


def _as_list(v: object, ctx: str) -> list[object]:
    if not isinstance(v, list):
        raise StateError(f"{ctx}: expected a list, got {type(v).__name__}")
    return list(v)


def _as_str(v: object, ctx: str) -> str:
    if not isinstance(v, str):
        raise StateError(f"{ctx}: expected a string, got {type(v).__name__}")
    return v


def _as_str_list(v: object, ctx: str) -> list[str]:
    items = _as_list(v, ctx)
    return [_as_str(item, f"{ctx}[{i}]") for i, item in enumerate(items)]


def _as_str_str_dict(v: object, ctx: str) -> dict[str, str]:
    d = _as_dict(v, ctx)  # enforces dict + str keys, else StateError
    result: dict[str, str] = {}
    for k, val in d.items():
        # JSON object keys are ALWAYS str after json.loads -- no key-type check.
        result[k] = _as_str(val, f"{ctx}.{k}")  # each value must be str
    return result


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #


def _snapshot_to_dict(snap: ConferenceSnapshot) -> dict[str, str]:
    return {"hash": snap.hash, "text": snap.text}


def _snapshot_from_obj(v: object, ctx: str) -> ConferenceSnapshot:
    d = _as_dict(v, ctx)
    if "hash" not in d:
        raise StateError(f"{ctx}: missing 'hash'")
    if "text" not in d:
        raise StateError(f"{ctx}: missing 'text'")
    return ConferenceSnapshot(
        hash=_as_str(d["hash"], f"{ctx}.hash"),
        text=_as_str(d["text"], f"{ctx}.text"),
    )


def _appearances_to_dict(app: SeenAppearances) -> dict[str, object]:
    return {
        "youtube": list(app.youtube),
        "rss_guids": list(app.rss_guids),
        "urls": list(app.urls),
        "conference_hashes": {
            k: _snapshot_to_dict(v) for k, v in app.conference_hashes.items()
        },
        "markers": dict(app.markers),  # copy, not alias
    }


def _appearances_from_obj(v: object, ctx: str) -> SeenAppearances:
    d = _as_dict(v, ctx)
    youtube = _as_str_list(d.get("youtube", []), f"{ctx}.youtube")
    rss_guids = _as_str_list(d.get("rss_guids", []), f"{ctx}.rss_guids")
    urls = _as_str_list(d.get("urls", []), f"{ctx}.urls")
    ch_raw = _as_dict(d.get("conference_hashes", {}), f"{ctx}.conference_hashes")
    conference_hashes: dict[str, ConferenceSnapshot] = {
        k: _snapshot_from_obj(val, f"{ctx}.conference_hashes.{k}")
        for k, val in ch_raw.items()
    }
    # Old state files predate `markers`; .get default -> {} (backward-compat).
    markers = _as_str_str_dict(d.get("markers", {}), f"{ctx}.markers")
    return SeenAppearances(
        youtube=youtube,
        rss_guids=rss_guids,
        urls=urls,
        conference_hashes=conference_hashes,
        markers=markers,
    )


def _seen_filings_from_obj(v: object, ctx: str) -> SeenFilings:
    d = _as_dict(v, ctx)
    result: SeenFilings = {}
    for k, val in d.items():
        result[k] = _as_str_list(val, f"{ctx}.{k}")
    return result


def _last_run_from_obj(v: object, ctx: str) -> LastRun:
    d = _as_dict(v, ctx)
    result: LastRun = {}
    for k, val in d.items():
        result[k] = _as_str(val, f"{ctx}.{k}")
    return result


# --------------------------------------------------------------------------- #
# StateStore
# --------------------------------------------------------------------------- #


class StateStore:
    """Typed read/write for the three state files plus interval gating."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._monitor_names = constants.MONITOR_NAMES

    # -- low-level read / write ------------------------------------------- #

    def _path(self, filename: str) -> Path:
        return self._state_dir / filename

    def _read_json(
        self, filename: str, default_factory: Callable[[], object]
    ) -> object:
        path = self._path(filename)
        if not path.exists():
            return default_factory()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateError(f"state file unreadable: {path}: {exc}") from exc
        if text.strip() == "":
            # 0-byte / all-whitespace -> benign first-run; never call json.loads.
            return default_factory()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(f"corrupt JSON in state file {path}: {exc}") from exc

    def _write_json(self, filename: str, data: object) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        target = self._path(filename)
        tmp = self._path(f"{filename}.tmp")
        try:
            tmp.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()

    # -- seen_filings ----------------------------------------------------- #

    def load_seen_filings(self) -> SeenFilings:
        raw = self._read_json(
            constants.STATE_FILE_SEEN_FILINGS, lambda: {}
        )
        return _seen_filings_from_obj(raw, "seen_filings")

    def save_seen_filings(self, data: SeenFilings) -> None:
        self._write_json(constants.STATE_FILE_SEEN_FILINGS, data)

    def is_filing_seen(self, entity_key: str, accession: str) -> bool:
        data = self.load_seen_filings()
        return accession in data.get(entity_key, [])

    def mark_filing_seen(self, entity_key: str, accession: str) -> None:
        data = self.load_seen_filings()
        seen = data.setdefault(entity_key, [])
        if accession not in seen:
            seen.append(accession)
            self.save_seen_filings(data)

    # -- seen_appearances ------------------------------------------------- #

    def load_seen_appearances(self) -> SeenAppearances:
        raw = self._read_json(
            constants.STATE_FILE_SEEN_APPEARANCES, lambda: {}
        )
        return _appearances_from_obj(raw, "seen_appearances")

    def save_seen_appearances(self, data: SeenAppearances) -> None:
        self._write_json(
            constants.STATE_FILE_SEEN_APPEARANCES, _appearances_to_dict(data)
        )

    def _appearance_list(
        self, data: SeenAppearances, kind: AppearanceKind
    ) -> list[str]:
        if kind == "youtube":
            return data.youtube
        if kind == "rss_guids":
            return data.rss_guids
        return data.urls

    def is_appearance_seen(
        self, kind: AppearanceKind, identifier: str
    ) -> bool:
        data = self.load_seen_appearances()
        return identifier in self._appearance_list(data, kind)

    def mark_appearance_seen(
        self, kind: AppearanceKind, identifier: str
    ) -> None:
        data = self.load_seen_appearances()
        target = self._appearance_list(data, kind)
        if identifier not in target:
            target.append(identifier)
            self.save_seen_appearances(data)

    def get_conference_snapshot(self, key: str) -> ConferenceSnapshot | None:
        data = self.load_seen_appearances()
        return data.conference_hashes.get(key)

    def set_conference_snapshot(
        self, key: str, snapshot: ConferenceSnapshot
    ) -> None:
        data = self.load_seen_appearances()
        data.conference_hashes[key] = snapshot
        self.save_seen_appearances(data)

    # -- last_run --------------------------------------------------------- #

    def load_last_run(self) -> LastRun:
        raw = self._read_json(constants.STATE_FILE_LAST_RUN, lambda: {})
        return _last_run_from_obj(raw, "last_run")

    def save_last_run(self, data: LastRun) -> None:
        self._write_json(constants.STATE_FILE_LAST_RUN, data)

    # -- interval gating -------------------------------------------------- #

    def should_run(
        self,
        monitor_name: str,
        now: datetime,
        intervals: dict[str, int],
    ) -> bool:
        interval_min = intervals.get(monitor_name)
        if interval_min is None:
            raise ValueError(f"unknown monitor: {monitor_name!r}")
        if now.tzinfo is None:
            raise ValueError("`now` must be timezone-aware")

        data = self.load_last_run()
        last = data.get(monitor_name)
        if last is None:
            return True  # never run -> always due (first-run runs everything)

        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError as exc:
            raise StateError(
                f"corrupt last_run timestamp for {monitor_name!r}: {last!r}"
            ) from exc
        if last_dt.tzinfo is None:
            raise StateError(
                f"persisted last_run timestamp for {monitor_name!r} is "
                f"tz-naive (corrupt state): {last!r}"
            )

        return (now - last_dt) >= timedelta(minutes=interval_min)

    def record_run(self, monitor_name: str, now: datetime) -> None:
        if monitor_name not in self._monitor_names:
            raise ValueError(f"unknown monitor: {monitor_name!r}")
        if now.tzinfo is None:
            raise ValueError("`now` must be timezone-aware")
        data = self.load_last_run()
        data[monitor_name] = now.isoformat()
        self.save_last_run(data)
