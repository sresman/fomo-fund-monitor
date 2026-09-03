from __future__ import annotations

"""SEC EDGAR filing monitor (Monitor 1).

Reads the EDGAR structured submissions JSON (never HTML), zips the parallel
``filings.recent`` arrays into typed per-filing records (row-tolerant), filters /
classifies / dedupes against ``StateStore``, seeds state silently on first run
for an entity, and returns a ``list[DetectedEvent]``. It does NOT send alerts
(the orchestrator does, Prompt 6). Everything is dependency-injected (client +
store + ``now`` + ``sleep``) so tests never touch the network and time is
deterministic.

Import surface for the orchestrator::

    from monitors.edgar import check_edgar, EdgarClient, EdgarHttpClient

Contract (Option B): ``check_edgar`` persists ONLY first-run backlog seeds. It
NEVER marks normal new filings seen -- the orchestrator must call
``store.mark_filing_seen(ev.entity_key, ev.identifier)`` after a successful
dispatch. Returned filing events are therefore not yet marked seen and will be
re-emitted next run until the orchestrator marks them.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

import requests

from config import AppConfig, EntityConfig
from constants import (
    EDGAR_FILING_INDEX_URL,
    EDGAR_MIN_REQUEST_INTERVAL_SECONDS,
    EDGAR_RETRY_BACKOFF_SECONDS,
    EDGAR_RETRY_STATUS,
    EDGAR_SUBMISSIONS_URL,
    FILING_TYPE_EVENT,
    FILING_TYPE_PRIORITY,
    FORM_AMENDMENT_SUFFIX,
    FORM_BASE_ALIASES,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
)
from errors import MonitorError
from models import Confidence, DetectedEvent, EventType, Priority
from state_manager import StateStore

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Typed submissions / filing models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingRecord:
    """One zipped filing row from ``filings.recent``.

    ``accession`` is the spine / dedupe key. All other fields are as-received
    strings that may be empty or (for dates) unparseable -- handled downstream.
    """

    accession: str  # dashed accession number (dedupe key)
    form: str  # RAW EDGAR form string, pre-normalization
    filing_date: str  # "YYYY-MM-DD" as received (may be "" / unparseable)
    report_date: str  # "YYYY-MM-DD" or "" (period; may be absent)
    primary_document: str  # filename or ""
    description: str  # primaryDocDescription or ""


@dataclass(frozen=True)
class SubmissionsResponse:
    cik: str  # padded CIK for link building (echoed-if-clean, else requested)
    filings: tuple[FilingRecord, ...]  # zipped; bad rows dropped, de-duplicated


# --------------------------------------------------------------------------- #
# JSON boundary narrowing helpers
# --------------------------------------------------------------------------- #
# TODO: extract shared _as_* narrowing helpers (3rd copy alongside config.py /
# state_manager.py). Note only -- intentionally NOT refactored this prompt to
# keep modules decoupled; a shared-utils module is out of scope for Prompt 3.


def _as_dict(v: object, ctx: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise MonitorError(f"{ctx}: expected an object, got {type(v).__name__}")
    result: dict[str, object] = {}
    for k, val in v.items():
        if not isinstance(k, str):
            raise MonitorError(
                f"{ctx}: object keys must be strings, got {type(k).__name__}"
            )
        result[k] = val
    return result


def _as_list(v: object, ctx: str) -> list[object]:
    if not isinstance(v, list):
        raise MonitorError(f"{ctx}: expected a list, got {type(v).__name__}")
    return list(v)


def _as_str(v: object, ctx: str) -> str:
    if not isinstance(v, str):
        raise MonitorError(f"{ctx}: expected a string, got {type(v).__name__}")
    return v


def _coerce_str(v: object) -> str:
    """Lenient per-row coercion for non-accession fields: non-str -> ``""``."""
    return v if isinstance(v, str) else ""


# --------------------------------------------------------------------------- #
# Submissions parser (row-tolerant)
# --------------------------------------------------------------------------- #

# Optional parallel arrays under ``filings.recent`` (default to [] when absent).
_OPTIONAL_ARRAYS: tuple[str, ...] = (
    "form",
    "filingDate",
    "reportDate",
    "primaryDocument",
    "primaryDocDescription",
)


def _parse_submissions(obj: object, requested_cik: str) -> SubmissionsResponse:
    """Narrow raw submissions JSON into a typed ``SubmissionsResponse``.

    Structural faults raise ``MonitorError``; individual bad rows are skipped
    (with an aggregate WARNING). ``accessionNumber`` is the mandatory spine; a
    missing/blank/non-str accession makes a row unusable.
    """
    root = _as_dict(obj, "submissions")

    # CIK for URL building: use the echoed CIK only if a clean digit string after
    # strip; else fall back to the (already padded) requested CIK.
    cik_for_url = requested_cik
    echoed = root.get("cik")
    if isinstance(echoed, (str, int)) and not isinstance(echoed, bool):
        candidate = str(echoed).strip()
        if candidate.isdigit():
            cik_for_url = candidate.zfill(10)

    filings_obj = root.get("filings")
    if filings_obj is None:
        raise MonitorError("submissions: missing 'filings' object")
    filings = _as_dict(filings_obj, "submissions.filings")

    recent_obj = filings.get("recent")
    if recent_obj is None:
        raise MonitorError("submissions.filings: missing 'recent' object")
    recent = _as_dict(recent_obj, "submissions.filings.recent")

    if "accessionNumber" not in recent:
        raise MonitorError(
            "submissions.filings.recent: missing 'accessionNumber' array "
            "(the spine/dedupe key)"
        )
    accession_list = _as_list(
        recent["accessionNumber"], "submissions.filings.recent.accessionNumber"
    )

    optional: dict[str, list[object]] = {}
    for key in _OPTIONAL_ARRAYS:
        optional[key] = _as_list(
            recent.get(key, []), f"submissions.filings.recent.{key}"
        )

    def at(arr: list[object], i: int) -> object:
        return arr[i] if i < len(arr) else ""

    records: list[FilingRecord] = []
    seen_accessions: set[str] = set()
    skipped = 0
    spine = len(accession_list)
    for i in range(spine):
        raw_acc = accession_list[i]
        if not isinstance(raw_acc, str) or raw_acc.strip() == "":
            skipped += 1
            continue
        accession = raw_acc.strip()
        if accession in seen_accessions:
            _log.warning(
                "EDGAR: dropped duplicate accession %s within a single "
                "response (first occurrence kept)",
                accession,
            )
            continue
        seen_accessions.add(accession)
        records.append(
            FilingRecord(
                accession=accession,
                form=_coerce_str(at(optional["form"], i)),
                filing_date=_coerce_str(at(optional["filingDate"], i)),
                report_date=_coerce_str(at(optional["reportDate"], i)),
                primary_document=_coerce_str(at(optional["primaryDocument"], i)),
                description=_coerce_str(at(optional["primaryDocDescription"], i)),
            )
        )

    if skipped > 0:
        _log.warning(
            "EDGAR: skipped %d malformed filing rows for CIK %s",
            skipped,
            cik_for_url,
        )

    return SubmissionsResponse(cik=cik_for_url, filings=tuple(records))


# --------------------------------------------------------------------------- #
# Form normalization + classification
# --------------------------------------------------------------------------- #


def _normalize_form(raw: str) -> str:
    """Canonicalize a raw EDGAR form string to the config/map spelling.

    Two stages:

    1. Hygiene -- strip, collapse internal whitespace runs to a single ASCII
       space (so ``"SC 13G"`` keeps its single space), then uppercase (map keys
       are uppercase; ``"4"`` is unaffected). A blank result simply fails the
       filing-type filter downstream.
    2. Aliasing -- EDGAR spells the Schedule 13D/G forms ``"SCHEDULE 13D"`` /
       ``"SCHEDULE 13G"``; ``config.yaml`` and ``FILING_TYPE_*`` use ``"SC 13D"``
       / ``"SC 13G"``. A trailing ``FORM_AMENDMENT_SUFFIX`` is split off FIRST,
       the BASE form is aliased via ``FORM_BASE_ALIASES``, and the suffix is then
       re-appended.

    The suffix round-trip is deliberate: an amendment must stay a DISTINCT form
    from its base so ``"SCHEDULE 13G/A"`` classifies as ``SC 13G/A`` and is never
    collapsed into ``SC 13G``. Forms with no alias entry pass through unchanged,
    so ``"13F-HR/A"`` and ``"4"`` are unaffected.

    Applied to BOTH sides of the tracked-form comparison (raw EDGAR forms and
    the configured ``filing_types``), so a config written in either spelling
    resolves to the same canonical key.
    """
    collapsed = " ".join(raw.split()).upper()
    if collapsed.endswith(FORM_AMENDMENT_SUFFIX):
        base = collapsed[: -len(FORM_AMENDMENT_SUFFIX)].rstrip()
        return FORM_BASE_ALIASES.get(base, base) + FORM_AMENDMENT_SUFFIX
    return FORM_BASE_ALIASES.get(collapsed, collapsed)


# --------------------------------------------------------------------------- #
# DetectedEvent payload notes
# --------------------------------------------------------------------------- #

_NOTE_DEFAULT = "Filing available at the link."
_NOTE_BY_EVENT: dict[EventType, str] = {
    EventType.FILING_13F: "Quarterly holdings report; position data at the link.",
    EventType.FILING_SC13: "Beneficial-ownership disclosure; details at the link.",
    EventType.FILING_FORM4: "Insider transaction report; details at the link.",
}


def _note_for(event_type: EventType) -> str:
    return _NOTE_BY_EVENT.get(event_type, _NOTE_DEFAULT)


# --------------------------------------------------------------------------- #
# EDGAR HTTP client (Protocol + concrete)
# --------------------------------------------------------------------------- #


class EdgarClient(Protocol):
    def fetch_submissions(self, cik: str) -> SubmissionsResponse: ...


class EdgarHttpClient:
    """Concrete EDGAR submissions client over ``requests``.

    Total attempts = ``1 + HTTP_MAX_RETRIES`` (``HTTP_MAX_RETRIES`` counts
    retries AFTER the first attempt). Retries ONLY on ``requests.Timeout`` /
    ``requests.ConnectionError`` and HTTP statuses in ``EDGAR_RETRY_STATUS``;
    ``403``/``404`` (and any other non-listed status) fail immediately. Backoff
    before retry N (1-based) is ``EDGAR_RETRY_BACKOFF_SECONDS * N`` via the
    injected ``sleep``. The same injected ``sleep`` is used for SEC rate-limit
    spacing, so a no-op test sleep disables all real-time waits.

    ``last_request_time`` is stamped in a ``finally`` after EVERY attempt so
    failed attempts and retries all respect SEC spacing (request-end ->
    next-request-start). The limiter is instance-local (fine for the
    single-process, ~1-req-per-entity operation here).
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        # Monotonic timestamp of the last request's completion; None until first.
        self._last_request_time: float | None = None

    def _respect_rate_limit(self) -> None:
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = EDGAR_MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def fetch_submissions(self, cik: str) -> SubmissionsResponse:
        url = EDGAR_SUBMISSIONS_URL.format(cik=cik)
        total_attempts = 1 + HTTP_MAX_RETRIES
        last_exc: Exception | None = None
        for attempt in range(total_attempts):
            if attempt > 0:
                # attempt is 0-based over the loop; retry number == attempt.
                self._sleep(EDGAR_RETRY_BACKOFF_SECONDS * attempt)
            self._respect_rate_limit()
            try:
                response = self._session.get(
                    url, headers=self._headers, timeout=HTTP_TIMEOUT_SECONDS
                )
                status = response.status_code
                if status in EDGAR_RETRY_STATUS:
                    last_exc = MonitorError(
                        f"EDGAR fetch failed for CIK {cik}: HTTP {status}"
                    )
                    continue
                if status != 200:
                    # Non-retryable (403/404/other) -> fail immediately.
                    raise MonitorError(
                        f"EDGAR fetch failed for CIK {cik}: HTTP {status}"
                    )
                try:
                    obj: object = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise MonitorError(
                        f"EDGAR fetch failed for CIK {cik}: invalid JSON: {exc}"
                    ) from exc
                return _parse_submissions(obj, requested_cik=cik)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                continue
            except requests.RequestException as exc:
                # Other transport errors are not retried.
                raise MonitorError(
                    f"EDGAR fetch failed for CIK {cik}: {exc}"
                ) from exc
            finally:
                self._last_request_time = time.monotonic()

        # Exhausted retries.
        if isinstance(last_exc, MonitorError):
            raise last_exc
        raise MonitorError(
            f"EDGAR fetch failed for CIK {cik}: retries exhausted"
        ) from last_exc


# --------------------------------------------------------------------------- #
# DetectedEvent construction
# --------------------------------------------------------------------------- #


def _build_url(cik: str, accession: str) -> str:
    cik_int = cik.lstrip("0") or "0"
    return EDGAR_FILING_INDEX_URL.format(
        cik_int=cik_int,
        acc_nodash=accession.replace("-", ""),
        acc_dash=accession,
    )


def _parse_published(filing_date: str) -> datetime | None:
    try:
        parsed = datetime.strptime(filing_date, "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _build_event(
    entity: EntityConfig,
    sub: SubmissionsResponse,
    record: FilingRecord,
    form_norm: str,
    published: datetime,
) -> DetectedEvent:
    event_type = FILING_TYPE_EVENT.get(form_norm, EventType.FILING_OTHER)
    priority = FILING_TYPE_PRIORITY.get(form_norm, Priority.LOW)
    payload: dict[str, str] = {
        "filing_type": form_norm,
        "period": record.report_date,
        "note": _note_for(event_type),
    }
    return DetectedEvent(
        event_type=event_type,
        entity_key=entity.key,
        source=entity.name,
        title=f"{form_norm} filed by {entity.name}",
        url=_build_url(sub.cik, record.accession),
        identifier=record.accession,
        published=published,
        priority=priority,
        confidence=Confidence.HIGH,
        payload=payload,
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def check_edgar(
    config: AppConfig,
    store: StateStore,
    client: EdgarClient,
    now: datetime,
) -> list[DetectedEvent]:
    """Fetch + classify EDGAR filings for every configured entity.

    Returned filing events are NOT yet marked seen. The orchestrator must mark
    each seen after a successful dispatch. Only first-run backlog seeds are
    persisted by this function.

    ``now`` must be timezone-aware (validated first, before state load, so a
    naive ``now`` raises rather than being swallowed per-entity). ``now`` is
    currently used only for this validation and is retained for interface
    symmetry with future monitors (e.g. a max-age filter).
    """
    # 1. Validate now FIRST (outside the per-entity catch; a naive now must raise).
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    # 2. Initial state load -- a failure here is FATAL (propagates).
    seen = store.load_seen_filings()

    events: list[DetectedEvent] = []
    pending_seeds: dict[str, list[str]] = {}

    # 3. Loop over entities in config order.
    for entity in config.entities:
        try:
            events.extend(
                _process_entity(entity, store, client, seen, pending_seeds)
            )
        except Exception:  # noqa: BLE001 -- per-entity isolation for unattended cron
            _log.exception(
                "EDGAR: failed to process entity %s (CIK %s); skipping",
                entity.key,
                entity.cik,
            )
            continue

    # 4. Final seed write (exactly one save, re-load-and-merge). Non-fatal.
    if pending_seeds:
        try:
            fresh = store.load_seen_filings()
            for key, accs in pending_seeds.items():
                fresh[key] = list(dict.fromkeys(fresh.get(key, []) + accs))
            store.save_seen_filings(fresh)
        except Exception:  # noqa: BLE001 -- non-fatal; entities re-seed next run
            _log.exception(
                "EDGAR: failed to persist first-run seeds; entities will "
                "re-seed on a later run (no data loss)"
            )

    return events


def _process_entity(
    entity: EntityConfig,
    store: StateStore,
    client: EdgarClient,
    seen: dict[str, list[str]],
    pending_seeds: dict[str, list[str]],
) -> list[DetectedEvent]:
    """Process one entity. Raises on any fault (caller isolates per-entity)."""
    sub = client.fetch_submissions(entity.cik)
    all_accessions = [f.accession for f in sub.filings]

    # First run: key absent.
    if entity.key not in seen:
        if all_accessions:
            pending_seeds[entity.key] = list(dict.fromkeys(all_accessions))
            _log.info(
                "EDGAR: first-run seed for %s -- %d filings suppressed",
                entity.key,
                len(pending_seeds[entity.key]),
            )
        # Empty first-run payload -> do NOT seed (flood safety); stays first-run.
        return []

    # Not first run: emit NEW tracked filings.
    already_seen = seen.get(entity.key, [])
    tracked_forms = {_normalize_form(ft) for ft in entity.filing_types}

    events: list[DetectedEvent] = []
    tracked_candidates = 0
    date_skipped = 0
    for record in sub.filings:
        if record.accession in already_seen:
            continue
        form_norm = _normalize_form(record.form)
        if form_norm not in tracked_forms:
            continue
        # A NEW, tracked filing == a candidate for emission.
        tracked_candidates += 1
        published = _parse_published(record.filing_date)
        if published is None:
            date_skipped += 1
            _log.warning(
                "EDGAR: skipping %s (%s) for %s -- unparseable filing_date %r",
                record.accession,
                record.form,
                entity.key,
                record.filing_date,
            )
            continue
        events.append(_build_event(entity, sub, record, form_norm, published))

    # Date-drift circuit breaker: >=1 candidate but 100% date-skipped.
    if tracked_candidates > 0 and date_skipped == tracked_candidates:
        _log.error(
            "EDGAR: all %d new tracked filings for %s were skipped due to "
            "unparseable filing dates -- possible SEC date-format change; no "
            "alerts emitted for this entity",
            tracked_candidates,
            entity.key,
        )

    return events
