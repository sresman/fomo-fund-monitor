from __future__ import annotations

"""YouTube Data API v3 search monitor (Monitor: youtube, Prompt 4).

Searches per-entity queries (broad every run + a once/day sweep), classifies
each hit by a surname-in-title confidence gate, dedupes against state + the
master manifest, seeds first-run backlog silently PER QUERY, and returns a
``list[DetectedEvent]``. Does NOT send alerts (the orchestrator does, Prompt 6).

Contract (Option B): ``check_youtube`` writes state ONLY for per-query first-run
seeds (bucket seeds + the query's seed marker in ``markers``) and the single
``markers["youtube_sweep"]`` scheduling key. It NEVER marks normal new videos
seen -- the orchestrator calls ``store.mark_appearance_seen("youtube", ev.identifier)``
after a successful dispatch. Returned events are re-emitted next run until then.

Priority is informational; alert routing is resolved from EventType via
``config.alert_routing`` (email+sms for YOUTUBE_HIGH, email for YOUTUBE_MEDIUM).

Quota (documented, NOT enforced): ``search.list`` costs
``YOUTUBE_SEARCH_COST_UNITS`` (100); daily quota ``YOUTUBE_DAILY_QUOTA`` (10000).
Sweep queries are PER-ENTITY, so daily cost scales with the entity count. With
the current config (1 broad + N sweep per entity, 2 entities): broad ~= 2 x 12
runs/day = 2,400 units; sweep ~= (per-entity sweep count summed) x 1/day; total
well under 10,000. A per-query API error (incl. quota exhaustion) is caught by
per-query isolation and -- because a failed query does not set its seed/sweep
marker -- retried next run.

Heavy import (``googleapiclient.discovery.build``) is DEFERRED into the default
build path so importing this module does not require the library. The API key is
resolved from ``os.environ`` INSIDE the concrete client, lazily and once.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from config import AppConfig, EntityConfig
from constants import (
    ENV_YOUTUBE_API_KEY,
    FEED_DESCRIPTION_EXCERPT_MAX,
    MARKER_YOUTUBE_SWEEP,
    YOUTUBE_API_SERVICE_NAME,
    YOUTUBE_API_VERSION,
    YOUTUBE_SEARCH_ORDER,
    YOUTUBE_SEARCH_PART,
    YOUTUBE_SEARCH_TYPE,
    YOUTUBE_WATCH_URL,
)
from errors import MonitorError
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._outcome import UnitTally
from monitors._common import (
    excerpt,
    merge_appearances,
    surname_of,
    youtube_seed_key,
)
from monitors.manifest import load_manifest_youtube_ids
from state_manager import StateStore

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Typed narrowing record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VideoResult:
    video_id: str
    title: str
    channel_title: str
    published_at: str  # raw ISO8601 string from API (parsed downstream)
    description: str


# --------------------------------------------------------------------------- #
# Service-chain Protocols (mypy seam over the dynamic discovery client)
# --------------------------------------------------------------------------- #


class RequestLike(Protocol):
    def execute(self) -> object: ...


class SearchResourceLike(Protocol):
    def list(self, **kwargs: object) -> RequestLike: ...


class YouTubeServiceLike(Protocol):
    def search(self) -> SearchResourceLike: ...


# --------------------------------------------------------------------------- #
# JSON boundary narrowing helpers (Nth copy of edgar's; extraction out of scope)
# --------------------------------------------------------------------------- #


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


def _as_str(v: object) -> str:
    return v if isinstance(v, str) else ""


# --------------------------------------------------------------------------- #
# Client seam (Protocol + concrete build-once, key-inside client)
# --------------------------------------------------------------------------- #


class YouTubeClient(Protocol):
    def search(self, query: str, max_results: int) -> tuple[VideoResult, ...]: ...


class YouTubeApiClient:
    """Concrete YouTube client over the discovery API.

    ``build_fn`` (the discovery build FACTORY) is injectable for testing; when
    None, the real ``googleapiclient.discovery.build`` is resolved via a DEFERRED
    import in the default build path. The service is built ONCE and cached on the
    instance. The API key is read from ``os.environ`` INSIDE the client, lazily,
    exactly once (guarded by the ``_service is None`` cache); a missing/blank key
    raises ``MonitorError`` (a loud deploy-misconfig signal, per-query isolated).
    """

    def __init__(self, build_fn: Callable[..., object] | None = None) -> None:
        self._build_fn = build_fn
        self._service: YouTubeServiceLike | None = None

    def _get_service(self) -> YouTubeServiceLike:
        if self._service is not None:
            return self._service

        if self._build_fn is not None:
            # Injected test seam: no real API, no env required.
            build_fn: Callable[..., object] = self._build_fn
            api_key = os.environ.get(ENV_YOUTUBE_API_KEY, "").strip() or "test-key"
        else:
            # Default path: resolve the real deferred build + require the key.
            api_key = os.environ.get(ENV_YOUTUBE_API_KEY, "").strip()
            if api_key == "":
                raise MonitorError("YOUTUBE_API_KEY not set")
            try:
                from googleapiclient.discovery import (  # type: ignore[import-untyped]
                    build,
                )
            except ModuleNotFoundError as exc:
                raise MonitorError(
                    "google-api-python-client not installed"
                ) from exc
            build_fn = build

        try:
            service_obj = build_fn(
                YOUTUBE_API_SERVICE_NAME,
                YOUTUBE_API_VERSION,
                developerKey=api_key,
            )
        except Exception as exc:  # noqa: BLE001 -- wrap discovery build faults
            raise MonitorError(f"YouTube service build failed: {exc}") from exc
        # Structural: the discovery client exposes .search().list().execute().
        service: YouTubeServiceLike = service_obj  # type: ignore[assignment]
        self._service = service
        return service

    def search(self, query: str, max_results: int) -> tuple[VideoResult, ...]:
        service = self._get_service()
        try:
            request = service.search().list(
                q=query,
                part=YOUTUBE_SEARCH_PART,
                type=YOUTUBE_SEARCH_TYPE,
                order=YOUTUBE_SEARCH_ORDER,
                maxResults=max_results,
            )
            response = request.execute()
        except MonitorError:
            raise
        except Exception as exc:  # noqa: BLE001 -- wrap Google API faults
            raise MonitorError(f"YouTube search failed for {query!r}: {exc}") from exc
        return _parse_search_response(response)


def _parse_search_response(response: object) -> tuple[VideoResult, ...]:
    root = _as_dict(response, "youtube.search")
    items = _as_list(root.get("items", []), "youtube.search.items")
    results: list[VideoResult] = []
    skipped = 0
    for i, item_obj in enumerate(items):
        item = _as_dict(item_obj, f"youtube.search.items[{i}]")
        id_obj = item.get("id")
        video_id = ""
        if isinstance(id_obj, dict):
            video_id = _as_str(id_obj.get("videoId"))
        if video_id == "":
            skipped += 1
            continue
        snippet_obj = item.get("snippet")
        snippet = snippet_obj if isinstance(snippet_obj, dict) else {}
        results.append(
            VideoResult(
                video_id=video_id,
                title=_as_str(snippet.get("title")),
                channel_title=_as_str(snippet.get("channelTitle")),
                published_at=_as_str(snippet.get("publishedAt")),
                description=_as_str(snippet.get("description")),
            )
        )
    if skipped > 0:
        _log.warning("YouTube: skipped %d search rows missing videoId", skipped)
    return tuple(results)


# --------------------------------------------------------------------------- #
# Confidence classification (surname gate; normalize at match time)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Classification:
    event_type: EventType
    confidence: Confidence
    priority: Priority


def _classify(
    person: str,
    title: str,
    channel_title: str,
    known_channels: tuple[str, ...],
    framing_keywords: tuple[str, ...],
) -> _Classification | None:
    """Return the classification, or None to EXCLUDE (surname not in title)."""
    surname = surname_of(person)
    title_lower = title.lower()
    surname_in_title = surname != "" and surname in title_lower
    if not surname_in_title:
        return None

    channel_norm = channel_title.strip().lower()
    known_channel = channel_norm in {c.strip().lower() for c in known_channels}
    framed = any(
        kw.strip().lower() != "" and kw.strip().lower() in title_lower
        for kw in framing_keywords
    )

    if known_channel or framed:
        return _Classification(
            EventType.YOUTUBE_HIGH, Confidence.HIGH, Priority.HIGH
        )
    return _Classification(
        EventType.YOUTUBE_MEDIUM, Confidence.MEDIUM, Priority.MEDIUM
    )


# --------------------------------------------------------------------------- #
# published parse (timezone guard)
# --------------------------------------------------------------------------- #


def _parse_published(published_at: str) -> datetime | None:
    if published_at == "":
        return None
    try:
        dt = datetime.fromisoformat(published_at)  # Py3.11 parses trailing 'Z'
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        # naive -> do NOT .astimezone() (it would assume local time).
        return None
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# DetectedEvent construction
# --------------------------------------------------------------------------- #


def _build_event(
    entity: EntityConfig,
    result: VideoResult,
    classification: _Classification,
) -> DetectedEvent:
    published = _parse_published(result.published_at)
    if published is None:
        _log.warning(
            "YouTube: unparseable/absent publishedAt %r for video %s (event "
            "still emitted)",
            result.published_at,
            result.video_id,
        )
    return DetectedEvent(
        event_type=classification.event_type,
        entity_key=entity.key,
        source=result.channel_title,
        title=result.title,
        url=YOUTUBE_WATCH_URL.format(video_id=result.video_id),
        identifier=result.video_id,
        published=published,
        priority=classification.priority,
        confidence=classification.confidence,
        payload={
            "person": entity.person,
            "duration": "",  # DEFERRED: search.list omits duration
            "description": excerpt(result.description, FEED_DESCRIPTION_EXCERPT_MAX),
        },
    )


# --------------------------------------------------------------------------- #
# Query plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _PlanItem:
    entity: EntityConfig
    query: str
    is_sweep: bool


def _build_plan(config: AppConfig, run_sweep: bool) -> list[_PlanItem]:
    plan: list[_PlanItem] = []
    for entity in config.entities:
        qset = config.youtube.queries_by_entity.get(entity.key)
        if qset is None:
            # Entity intentionally NOT YouTube-monitored.
            continue
        for q in qset.broad_queries:
            plan.append(_PlanItem(entity, q, is_sweep=False))
        if run_sweep:
            for q in qset.sweep_queries:
                plan.append(_PlanItem(entity, q, is_sweep=True))
    return plan


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def check_youtube(
    config: AppConfig,
    store: StateStore,
    client: YouTubeClient,
    now: datetime,
) -> list[DetectedEvent]:
    """Search YouTube for every planned query and return new-video events.

    First-run per query (seed marker absent): seed ALL observed video ids
    (including manifest-matched ones) into the ``youtube`` bucket + set the
    query's seed marker, emit nothing. A SUCCESSFUL fetch with zero matches STILL
    completes first-run. A FAILED fetch leaves the query first-run to retry.
    Sweep queries run once/UTC-day, gated by ``markers["youtube_sweep"]``.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    seen = store.load_seen_appearances()  # fatal on StateError
    already_seen = set(seen.youtube)
    markers = seen.markers

    manifest_ids = load_manifest_youtube_ids(config.youtube.master_manifest_path)

    today_utc = now.astimezone(timezone.utc).date().isoformat()
    run_sweep = markers.get(MARKER_YOUTUBE_SWEEP) != today_utc

    plan = _build_plan(config, run_sweep)

    handled: set[str] = set()
    events: list[DetectedEvent] = []
    pending_bucket_seeds: list[str] = []
    new_markers: dict[str, str] = {}
    sweep_ran_ok = False
    tally = UnitTally("youtube")

    for item in plan:
        seed_key = youtube_seed_key(item.query)
        first_run = seed_key not in markers
        try:
            results = client.search(item.query, config.youtube.max_results_per_query)
        except Exception:  # noqa: BLE001 -- per-query isolation
            tally.record_failure()
            _log.exception(
                "YouTube: query %r for entity %s failed; skipping (stays "
                "first-run if not yet seeded)",
                item.query,
                item.entity.key,
            )
            continue
        tally.record_success()

        # Successful observation.
        if item.is_sweep:
            sweep_ran_ok = True

        query_seeds: list[str] = []
        for result in results:
            video_id = result.video_id
            if video_id == "":
                continue
            if video_id in handled:
                continue
            if first_run:
                # Seed ALL observed ids INCLUDING manifest-matched ones.
                query_seeds.append(video_id)
                handled.add(video_id)
                continue
            # Normal query: dedupe vs bucket + manifest.
            if video_id in already_seen or video_id in manifest_ids:
                continue
            classification = _classify(
                item.entity.person,
                result.title,
                result.channel_title,
                config.youtube.known_channels,
                config.youtube.framing_keywords,
            )
            if classification is None:
                _log.debug("YouTube: EXCLUDE (surname absent) %r", result.title)
                continue
            events.append(_build_event(item.entity, result, classification))
            handled.add(video_id)

        if first_run:
            for vid in query_seeds:
                if vid not in pending_bucket_seeds:
                    pending_bucket_seeds.append(vid)
            new_markers[seed_key] = today_utc  # even if zero found

    if run_sweep and sweep_ran_ok:
        new_markers[MARKER_YOUTUBE_SWEEP] = today_utc

    # Batched reload-merge-save (runs even when events == []).
    if pending_bucket_seeds or new_markers:
        try:
            fresh = store.load_seen_appearances()
            merged = merge_appearances(
                fresh, "youtube", pending_bucket_seeds, new_markers
            )
            store.save_seen_appearances(merged)
        except Exception:  # noqa: BLE001 -- non-fatal; re-seeds next run
            _log.exception(
                "YouTube: failed to persist first-run seeds / sweep marker; "
                "will retry next run (no data loss)"
            )

    # Every planned query dead (quota exhausted, bad key, API outage) => this
    # run observed nothing; do not advance last_run.
    tally.raise_if_total_failure()
    return events
