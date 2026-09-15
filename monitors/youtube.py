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
import re
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
    YOUTUBE_VIDEOS_BATCH_MAX,
    YOUTUBE_VIDEOS_PART,
    YOUTUBE_WATCH_URL,
)
from errors import MonitorError
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._outcome import UnitTally
from monitors._common import (
    excerpt,
    is_first_party_appearance,
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


@dataclass(frozen=True)
class VideoDetails:
    """What videos.list adds on top of a search hit.

    ``duration_seconds`` is None when unresolved/unparseable -- UNKNOWN, never 0.
    ``description`` is the FULL description; search.list truncates its copy to
    roughly 120 characters, which is not enough to find guest framing reliably.
    """

    duration_seconds: int | None
    description: str


# --------------------------------------------------------------------------- #
# Service-chain Protocols (mypy seam over the dynamic discovery client)
# --------------------------------------------------------------------------- #


class RequestLike(Protocol):
    def execute(self) -> object: ...


class SearchResourceLike(Protocol):
    def list(self, **kwargs: object) -> RequestLike: ...


class VideosResourceLike(Protocol):
    def list(self, **kwargs: object) -> RequestLike: ...


class YouTubeServiceLike(Protocol):
    def search(self) -> SearchResourceLike: ...

    def videos(self) -> VideosResourceLike: ...


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

    def details(self, video_ids: tuple[str, ...]) -> dict[str, VideoDetails]:
        """Map video id -> VideoDetails, for the ids that resolved.

        An id absent from the result means "unknown", NOT "zero-length" and NOT
        "no description": callers must treat absence as missing metadata and
        fall back to what search.list gave them.
        """
        ...


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

    def details(self, video_ids: tuple[str, ...]) -> dict[str, VideoDetails]:
        """Resolve duration + FULL description via videos.list, batched at the
        API's 50-id limit.

        A failing BATCH is logged and skipped rather than raised: this is an
        enrichment, and an unresolved id degrades to the search.list snippet
        plus an unknown duration -- never a lost event.
        """
        wanted = tuple(dict.fromkeys(v for v in video_ids if v != ""))
        if not wanted:
            return {}
        service = self._get_service()
        resolved: dict[str, VideoDetails] = {}
        for start in range(0, len(wanted), YOUTUBE_VIDEOS_BATCH_MAX):
            batch = wanted[start : start + YOUTUBE_VIDEOS_BATCH_MAX]
            try:
                request = service.videos().list(
                    part=YOUTUBE_VIDEOS_PART,
                    id=",".join(batch),
                )
                resolved.update(_parse_videos_response(request.execute()))
            except Exception:  # noqa: BLE001 -- enrichment; degrades, never loses
                _log.exception(
                    "YouTube: videos.list failed for %d id(s); they fall back "
                    "to the search snippet and an unknown duration",
                    len(batch),
                )
        return resolved


# ISO-8601 durations as YouTube emits them: "PT1H14M26S", "PT45S", "P1DT2H".
_ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def parse_iso8601_duration(value: str) -> int | None:
    """Seconds, or None when the string is absent/unparseable.

    None means UNKNOWN. It is never collapsed to 0 -- a caller applying a
    minimum-length floor must be able to tell "shorter than the floor" from
    "we could not find out".
    """
    text = value.strip()
    if text == "":
        return None
    match = _ISO_DURATION_RE.fullmatch(text)
    if match is None:
        return None
    groups = match.groups()
    if all(g is None for g in groups):
        # "P" / "PT": every component optional in the grammar, so these match
        # while carrying no duration at all. Degenerate, and must read as
        # UNKNOWN -- returning 0 here would put them below every floor.
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in groups)
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _parse_videos_response(response: object) -> dict[str, VideoDetails]:
    root = _as_dict(response, "youtube.videos")
    items = _as_list(root.get("items", []), "youtube.videos.items")
    out: dict[str, VideoDetails] = {}
    for i, item_obj in enumerate(items):
        item = _as_dict(item_obj, f"youtube.videos.items[{i}]")
        video_id = _as_str(item.get("id"))
        if video_id == "":
            continue
        content_obj = item.get("contentDetails")
        content = content_obj if isinstance(content_obj, dict) else {}
        snippet_obj = item.get("snippet")
        snippet = snippet_obj if isinstance(snippet_obj, dict) else {}
        out[video_id] = VideoDetails(
            duration_seconds=parse_iso8601_duration(_as_str(content.get("duration"))),
            description=_as_str(snippet.get("description")),
        )
    return out


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
    description: str,
) -> _Classification | None:
    """Return the classification, or None to EXCLUDE.

    CHANNEL IS EVALUATED FIRST (changed 2026-09-15). It used to be gated behind
    a surname-in-title check, which meant an allowlisted publisher's own upload
    was thrown away whenever the publisher did not put the guest's surname in
    the title. That is a real shape, not a hypothetical: a16z published a
    74-minute Gavin Baker interview on 2026-08-31 titled "Why AI Demand Is
    Outrunning Compute Supply", and it never entered the YouTube dedupe bucket
    at all. The a16z podcast FEED caught it -- but seven allowlisted channels
    have no feed configured, so on those it would simply have vanished.

    On an ALLOWLISTED channel the publisher is the first-party signal, so the
    title need not name anyone. Two ways to qualify:

    1. The surname is in the title (the original rule, preserved exactly).
    2. ``is_first_party_appearance`` finds the full name in the title, or near a
       guest-framing stem in the DESCRIPTION -- "David George sits down with
       Gavin Baker", "Gavin Baker, Ben Shapiro and Phil Deutch join the show".

    Channel membership ALONE is deliberately not enough. Measured over the back
    catalogue of all 17 allowlisted channels, admitting on membership alone
    would newly alert on 61 videos; a 20-minute duration floor only cuts that to
    25, because the noise on these channels is long-form too -- 95-minute
    Bloomberg market shows, 100-minute All-In panels that merely mention the
    name, Dwarkesh and ILTB episodes with entirely different guests. The
    description gate cuts the same 61 to 4. Duration separates clips from shows;
    it cannot separate "he is on it" from "they talked about him".

    OFF the allowlist nothing changed: the surname must be in the title, and the
    result is MEDIUM -- "the name is in the title but the publisher is not one we
    recognise". Framing keywords in the title do NOT promote, because a title is
    written by whoever uploaded it: a third-party channel can call its 90-second
    cut "Gavin Baker interview" and inherit HIGH. Every YouTube event that
    reached the inbox on 2026-09-03 was of exactly that shape. MEDIUM routes to
    no channels (see ``alert_routing.youtube_medium``), so it is captured,
    committed, and silent.
    """
    surname = surname_of(person)
    surname_in_title = surname != "" and surname in title.lower()

    channel_norm = channel_title.strip().lower()
    known_channel = channel_norm in {c.strip().lower() for c in known_channels}

    if known_channel:
        if surname_in_title or is_first_party_appearance(
            title, description, (person,)
        ):
            return _Classification(
                EventType.YOUTUBE_HIGH, Confidence.HIGH, Priority.HIGH
            )
        return None

    if not surname_in_title:
        return None
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
    duration_seconds: int | None,
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
            # From the batched videos.list lookup; "" means UNKNOWN.
            "duration": "" if duration_seconds is None else str(duration_seconds),
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


@dataclass(frozen=True)
class _Candidate:
    """A search hit that survived dedupe, awaiting enrichment + classification."""

    entity: EntityConfig
    result: VideoResult


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
    candidates: list[_Candidate] = []
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
            # Normal query: dedupe vs bucket + manifest. Classification is
            # DEFERRED to a second pass -- it needs the full description, which
            # only videos.list carries (search.list truncates to ~120 chars).
            if video_id in already_seen or video_id in manifest_ids:
                continue
            candidates.append(_Candidate(item.entity, result))
            handled.add(video_id)

        if first_run:
            for vid in query_seeds:
                if vid not in pending_bucket_seeds:
                    pending_bucket_seeds.append(vid)
            new_markers[seed_key] = today_utc  # even if zero found

    # PASS 2. ONE batched videos.list for every surviving candidate, then
    # classify. This call is what makes the full description available: an
    # allowlisted channel's upload qualifies on guest framing in the
    # description, and search.list's ~120-char truncation is not enough to find
    # it reliably. An id that does not resolve degrades to the search snippet
    # and an unknown duration -- never a dropped candidate.
    details: dict[str, VideoDetails] = {}
    if candidates:
        try:
            details = client.details(
                tuple(c.result.video_id for c in candidates)
            )
        except Exception:  # noqa: BLE001 -- enrichment must never fail the run
            _log.exception(
                "YouTube: videos.list lookup failed; falling back to search "
                "snippets and unknown durations for %d candidate(s)",
                len(candidates),
            )

    for candidate in candidates:
        detail = details.get(candidate.result.video_id)
        description = candidate.result.description
        duration_seconds: int | None = None
        if detail is not None:
            duration_seconds = detail.duration_seconds
            if detail.description != "":
                description = detail.description
        classification = _classify(
            candidate.entity.person,
            candidate.result.title,
            candidate.result.channel_title,
            config.youtube.known_channels,
            description,
        )
        if classification is None:
            _log.debug(
                "YouTube: EXCLUDE %r (channel %r)",
                candidate.result.title,
                candidate.result.channel_title,
            )
            continue
        events.append(
            _build_event(
                candidate.entity,
                candidate.result,
                classification,
                duration_seconds,
            )
        )

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
