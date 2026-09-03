from __future__ import annotations

"""CNBC video-search HTML scrape monitor (Monitor: cnbc, Prompt 5).

Appearance-style: per configured query, scrape CNBC's video search results,
dedupe by CANONICALIZED video URL in the shared ``urls`` bucket, seed the
first-run backlog silently PER QUERY, and return a ``list[DetectedEvent]``.
Reuses the ``HttpGetter`` fetch seam + ``matches_keywords`` semantics from
``_common``. Does NOT send alerts (orchestrator, Prompt 6).

Contract (Option B): writes state ONLY for per-query first-run seeds (``urls``
bucket seeds + ``seeded:cnbc:<query>`` markers). It NEVER marks normal new videos
seen -- the orchestrator calls ``store.mark_appearance_seen("urls", ev.identifier)``
after a successful dispatch (identical to google_news). Emitted CNBC events are
feed_events -> returned REGARDLESS of the batched seed save.

Single-process / sequential assumption: monitors run one-at-a-time in one
process; there is NO file locking on the state file. The reload-merge at save
time mitigates same-run bucket clobbering; concurrent processes are out of v1
scope.

FLAG-CNBC-JS (LOUD): CNBC search is very likely JS-rendered / bot-protected. A
plain ``requests`` + bs4 fetch may return an empty or challenge shell with ZERO
parseable results. ``_parse_search_html``'s selectors (anchors whose href
contains ``/video/``) and the structural sentinel are a BEST-GUESS that needs
LIVE validation against a real CNBC search page (or CNBC's JSON search endpoint
if one is discoverable). The monitor DEGRADES GRACEFULLY: zero results is a valid
non-crashing outcome. Also unverified: whether CNBC video ids live in the URL
PATH (safe to drop the query string in canonicalization, as done here) or in a
QUERY PARAM (in which case dropping the query would collapse distinct videos to
one id -> canonicalization must change). The WHOLE CNBC integration needs live
validation before it can be relied on.

HTML scraping is ALLOWED here: CNBC has no public structured feed for search; the
project's "do not scrape HTML" rule is EDGAR-specific (EDGAR has structured
XML/JSON; CNBC search does not). Scoped exception.
"""

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from bs4 import BeautifulSoup, Tag

from config import AppConfig
from constants import (
    CNBC_BASE_URL,
    CNBC_SEARCH_URL,
    CNBC_SOURCE_LABEL,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
)
from errors import MonitorError
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._outcome import UnitTally
from monitors._common import (
    HttpGetter,
    cnbc_seed_key,
    merge_appearances,
    surname_of,
)
from state_manager import StateStore

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CnbcResult:
    url: str  # RESOLVED (absolute, NON-canonicalized) video URL
    title: str
    published: datetime | None  # tz-aware, else None


@dataclass(frozen=True)
class _ParsedSearch:
    results: tuple[CnbcResult, ...]
    recognized: bool  # HTML had a recognizable CNBC search-results structure


class CnbcClient(Protocol):
    """Seam that isolates the fragile CNBC scrape from the monitor logic."""

    def search(self, query: str) -> tuple[CnbcResult, ...]: ...

    def had_recognizable_structure(self) -> bool: ...


def _canonicalize_url(resolved_url: str) -> str:
    """Dedupe identifier from a RESOLVED url: scheme + LOWERCASED netloc + path;
    DROP query + fragment; NORMALIZE trailing slash.

    FLAG-CNBC-JS caveat: dropping the query assumes video ids are PATH-based. If
    CNBC actually encodes the video id in a query param, this collapses distinct
    videos to one id and MUST be revisited during live validation.
    """
    if resolved_url == "":
        return ""
    parts = urllib.parse.urlsplit(resolved_url)
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc.lower(), path, "", "")
    )


def _parse_search_html(html: bytes) -> _ParsedSearch:
    """Extract ``(url, title, published?)`` video rows from CNBC search HTML.

    Isolated single function so a layout change is a one-place fix. Best-guess
    selectors (FLAG-CNBC-JS):
      - a recognizable results CONTAINER (any element with a class token
        containing ``searchresult`` / ``search-result``, case-insensitive) marks
        the structure as recognized (the structural sentinel) INDEPENDENT of
        whether individual rows parse.
      - each result = an anchor whose ``href`` contains ``/video/``; the anchor
        text is the title. Relative hrefs are resolved against ``CNBC_BASE_URL``.
    ``published`` is best-effort -> None for relative / no-tz / unparseable dates
    (CNBC search markup exposes no reliable machine date here).

    Malformed rows (missing href or empty resolved url) are skipped. Zero results
    with no recognizable structure -> ``recognized=False`` (a blocked/empty
    shell), which the monitor uses to REFUSE first-run seeding.
    """
    soup = BeautifulSoup(html, "html.parser")

    recognized = _has_results_container(soup)

    results: list[CnbcResult] = []
    seen_local: set[str] = set()
    for anchor in soup.find_all("a"):
        if not isinstance(anchor, Tag):
            continue
        href_val = anchor.get("href")
        if not isinstance(href_val, str):
            continue
        if "/video/" not in href_val:
            continue
        resolved = urllib.parse.urljoin(CNBC_BASE_URL, href_val)
        if resolved == "":
            continue
        title = anchor.get_text(strip=True)
        # De-dup identical resolved urls WITHIN the parse (a page may repeat an
        # anchor for image + text); keep the first with a non-empty title.
        if resolved in seen_local:
            continue
        seen_local.add(resolved)
        results.append(
            CnbcResult(url=resolved, title=title, published=None)
        )
    return _ParsedSearch(results=tuple(results), recognized=recognized)


def _has_results_container(soup: BeautifulSoup) -> bool:
    """Structural sentinel: any element carrying a class token that looks like a
    CNBC search-result wrapper. Recognizes the page structure even when zero rows
    parse (so a blocked/empty shell is distinguishable from a real empty result).
    """
    for element in soup.find_all(True):
        if not isinstance(element, Tag):
            continue
        class_val = element.get("class")
        tokens: list[str] = []
        if isinstance(class_val, str):
            tokens = class_val.split()
        elif isinstance(class_val, list):
            tokens = [c for c in class_val if isinstance(c, str)]
        for token in tokens:
            low = token.lower()
            if "searchresult" in low or "search-result" in low:
                return True
    return False


class CnbcHttpClient:
    """Concrete ``CnbcClient`` over an injectable ``HttpGetter`` (reuse the
    ``_common`` fetch seam). Tests inject a fake ``HttpGetter`` returning canned
    HTML bytes; no network is touched. Transport fault -> ``MonitorError``
    (mirrors ``RequestsFeedClient``)."""

    def __init__(self, http: HttpGetter | None = None) -> None:
        if http is not None:
            self._http: HttpGetter = http
        else:
            from monitors._common import _RequestsAdapter

            self._http = _RequestsAdapter()
        self._recognized: bool = False

    def search(self, query: str) -> tuple[CnbcResult, ...]:
        url = CNBC_SEARCH_URL.format(query=urllib.parse.quote_plus(query))
        try:
            resp = self._http.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=float(HTTP_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            content = resp.content
        except MonitorError:
            raise
        except Exception as exc:  # noqa: BLE001 -- any transport fault -> MonitorError
            raise MonitorError(f"cnbc fetch failed for {url}: {exc}") from exc
        parsed = _parse_search_html(content)
        self._recognized = parsed.recognized
        return parsed.results

    def had_recognizable_structure(self) -> bool:
        return self._recognized


def _map_query(config: AppConfig, query: str) -> str:
    """Best-effort entity_key: first ``config.entities`` entry whose person
    SURNAME (or full name) is a case-insensitive SUBSTRING of ``query``; else "".

    Non-load-bearing (routing is EventType-driven; dedupe is url-driven)."""
    q_lower = query.lower()
    for e in config.entities:
        surname = surname_of(e.person)
        if surname and surname in q_lower:
            return e.key
        if e.person and e.person.lower() in q_lower:
            return e.key
    return ""


def check_cnbc(
    config: AppConfig,
    store: StateStore,
    client: CnbcClient,
    now: datetime,
) -> list[DetectedEvent]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    seen = store.load_seen_appearances()  # fatal on StateError
    already_seen = set(seen.urls)
    markers = seen.markers
    today_utc = now.astimezone(timezone.utc).date().isoformat()

    handled: set[str] = set()  # canonical urls emitted OR seeded THIS run
    events: list[DetectedEvent] = []
    pending_bucket_seeds: list[str] = []
    new_markers: dict[str, str] = {}
    tally = UnitTally("cnbc")

    for query in config.cnbc.queries:
        seed_key = cnbc_seed_key(query)
        first_run = seed_key not in markers
        try:
            results = client.search(query)
            recognized = client.had_recognizable_structure()
        except Exception:  # noqa: BLE001 -- per-query isolation
            tally.record_failure()
            _log.exception(
                "cnbc: query %r failed; skipping (stays first-run if not yet "
                "seeded)",
                query,
            )
            continue
        tally.record_success()

        # Zero-after-nonzero warning: a NON-first-run query returning zero
        # results may signal a block / layout change / JS degradation.
        if not first_run and not results:
            _log.warning(
                "cnbc: query %r returned zero results (possible block / layout "
                "change / JS degradation)",
                query,
            )

        entity_key = _map_query(config, query)
        query_seeds: list[str] = []
        for result in results:
            identifier = _canonicalize_url(result.url)
            if identifier == "" or result.title == "":
                continue  # malformed row (missing url or title)
            if identifier in handled:
                continue
            if first_run:
                query_seeds.append(identifier)
                handled.add(identifier)
                continue
            if identifier in already_seen:
                continue
            events.append(
                DetectedEvent(
                    event_type=EventType.CNBC_VIDEO,
                    entity_key=entity_key,
                    source=CNBC_SOURCE_LABEL,
                    title=result.title,
                    url=result.url,  # RESOLVED, non-canonical
                    identifier=identifier,  # canonicalized
                    published=result.published,
                    priority=Priority.HIGH,
                    confidence=Confidence.HIGH,
                    payload={},
                )
            )
            handled.add(identifier)

        if first_run:
            # Structural first-run guard (SD-P5-8): only seed if we made a VALID
            # observation -- at least one result OR a recognizable structure. A
            # first-run zero-result scrape WITHOUT structure is a FAILED fetch:
            # do NOT write the marker -> stays first-run, retried next run.
            if results or recognized:
                for ident in query_seeds:
                    if ident not in pending_bucket_seeds:
                        pending_bucket_seeds.append(ident)
                new_markers[seed_key] = today_utc
            else:
                _log.warning(
                    "cnbc: query %r first-run zero results with no recognizable "
                    "structure; NOT seeding (treated as failed fetch)",
                    query,
                )

    if pending_bucket_seeds or new_markers:
        try:
            fresh = store.load_seen_appearances()
            merged = merge_appearances(
                fresh, "urls", pending_bucket_seeds, new_markers
            )
            store.save_seen_appearances(merged)
        except Exception:  # noqa: BLE001 -- non-fatal; feed events still returned
            _log.exception(
                "cnbc: failed to persist first-run seeds; will retry next run "
                "(no data loss; feed events still returned)"
            )

    # Every query dead => this run observed nothing; do not advance last_run.
    tally.raise_if_total_failure()
    return events
