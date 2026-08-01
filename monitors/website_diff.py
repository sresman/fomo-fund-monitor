from __future__ import annotations

"""Website content-hash diff + optional Substack RSS monitor (website_diff, P5).

Handles two site kinds via ONE injected ``feed_client`` (used for BOTH the
page-hash fetch and the RSS ``/feed`` fetch -- a single param removes the
two-same-type-param swap hazard):

- ``check_rss=False`` (page-hash, Option A): fetch -> normalize -> WAF/min-length
  guard -> hash against the namespaced ``website:<key>`` snapshot in the shared
  ``conference_hashes`` dict. First run stages the snapshot, emits nothing.
  Changed hash -> line-oriented diff from OLD text, truly-changed-line keyword
  gate (empty keywords -> alert on ANY change), stage the advance, emit
  WEBSITE_DIFF (unique-per-change id ``website:<key>@<hash12>``). These are
  content_events -> returned ONLY if the batched save succeeded.

- ``check_rss=True`` (RSS PRIMARY, Option B): build ``<site.url>/feed``, fetch,
  parse, VALID-FEED gate (feedparser must recognize a feed), per-site seeding
  (``seeded:website_rss:<key>``), dedupe against ``rss_guids``, emit LEOPOLD_POST
  per new post. These are feed_events -> returned REGARDLESS of the save.

Site -> EventType is a small CODE map keyed by ``site.key`` (a closed two-entry
set tied to formatter payload key + routing). The RSS branch is v1-restricted to
LEOPOLD_POST: a ``check_rss=True`` site mapping to any OTHER type is
WARNING+SKIP (a feed-derived non-LEOPOLD payload is undefined in v1).

FLAG-RSS-PRIMARY (LOUD): when ``check_rss=True``, RSS is the SOLE signal and the
page-hash diff is SKIPPED for that site. Consequence: a static-page change NOT
reflected in the feed is MISSED. Accepted for v1 because a new post ALSO changes
the homepage hash, so checking both would double-alert every post. Also assumes
the Substack feed path is ``<site.url>/feed`` -- CONFIRM situational-awareness.com's
actual feed URL (custom-domain Substacks).

Persist-then-emit: ONE batched save (skipped if nothing pending). content_events
(page-hash WEBSITE_DIFF) returned only if the save succeeded (else ERROR +
suppress, re-detected next run). feed_events (RSS LEOPOLD_POST) returned
regardless (orchestrator-managed dedup; a failed seed just re-seeds next run).

Single-process / sequential assumption: NO file locking; the reload-merge
mitigates same-run bucket clobbering; concurrent processes are out of v1 scope.

HTML scraping is ALLOWED here (EDGAR-specific rule). Scoped exception.
"""

import logging
from datetime import datetime, timezone

from config import AppConfig
from constants import (
    DIFF_SNIPPET_MAX,
    FEED_DESCRIPTION_EXCERPT_MAX,
)
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._common import (
    FeedClient,
    excerpt,
    matches_keywords,
    merge_appearances,
    parse_feed,
    website_rss_seed_key,
    website_snapshot_key,
)
from monitors._content_hash import (
    changed_lines,
    content_hash,
    extract_normalized_text,
    is_suspect_content,
    make_diff,
)
from state_manager import ConferenceSnapshot, StateStore

_log = logging.getLogger(__name__)

# Site.key -> EventType. Default (unmapped) -> WEBSITE_DIFF.
_SITE_EVENT_TYPE: dict[str, EventType] = {
    "situational_awareness_com": EventType.LEOPOLD_POST,
}


def _event_type_for(site_key: str) -> EventType:
    return _SITE_EVENT_TYPE.get(site_key, EventType.WEBSITE_DIFF)


def _feed_is_valid(content: bytes) -> bool:
    """True iff feedparser RECOGNIZES ``content`` as a real feed (SD-P5-8).

    parse_feed is lenient and yields entries for whatever it can, so a challenge
    / HTML / redirect page can slip through with zero entries. Guard by checking
    feedparser's ``version`` (non-empty for a recognized RSS/Atom feed). Uses the
    deferred feedparser import (kept out of module top like ``_common``)."""
    try:
        import feedparser  # type: ignore[import-untyped]  # no stubs; deferred
    except ModuleNotFoundError:
        return False
    parsed = feedparser.parse(content)
    version_obj: object = getattr(parsed, "version", "")
    return isinstance(version_obj, str) and version_obj != ""


def check_website_diff(
    config: AppConfig,
    store: StateStore,
    feed_client: FeedClient,
    now: datetime,
) -> list[DetectedEvent]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    seen = store.load_seen_appearances()  # fatal on StateError
    conference_hashes = seen.conference_hashes
    already_seen_rss = set(seen.rss_guids)
    markers = seen.markers
    today_utc = now.astimezone(timezone.utc).date().isoformat()

    handled_rss: set[str] = set()  # rss guids emitted OR seeded THIS run
    snapshot_updates: dict[str, ConferenceSnapshot] = {}
    pending_rss_seeds: list[str] = []
    new_markers: dict[str, str] = {}
    content_events: list[DetectedEvent] = []
    feed_events: list[DetectedEvent] = []

    for site in config.website_diff:
        try:
            if site.check_rss:
                _check_rss_site(
                    config=config,
                    site_key=site.key,
                    site_url=site.url,
                    feed_client=feed_client,
                    markers=markers,
                    already_seen_rss=already_seen_rss,
                    handled_rss=handled_rss,
                    pending_rss_seeds=pending_rss_seeds,
                    new_markers=new_markers,
                    feed_events=feed_events,
                    today_utc=today_utc,
                )
            else:
                _check_page_hash_site(
                    site_key=site.key,
                    site_url=site.url,
                    site_keywords=site.keywords,
                    feed_client=feed_client,
                    conference_hashes=conference_hashes,
                    snapshot_updates=snapshot_updates,
                    content_events=content_events,
                    now=now,
                )
        except Exception:  # noqa: BLE001 -- per-site isolation
            _log.exception(
                "website_diff: site %s (%s) failed; skipping",
                site.key,
                site.url,
            )
            continue

    pending_state = bool(
        snapshot_updates or pending_rss_seeds or new_markers
    )
    save_ok = True
    if pending_state:
        try:
            fresh = store.load_seen_appearances()
            merged = merge_appearances(
                fresh,
                "rss_guids",
                pending_rss_seeds,
                new_markers,
                conference_hashes=snapshot_updates,
            )
            store.save_seen_appearances(merged)
        except Exception:  # noqa: BLE001 -- content events suppressed; feed events kept
            save_ok = False
            _log.error(
                "website_diff: failed to persist state; SUPPRESSING %d content "
                "event(s) (re-detected next run); %d feed event(s) still "
                "returned (site stays first-run)",
                len(content_events),
                len(feed_events),
                exc_info=True,
            )

    if save_ok:
        return content_events + feed_events
    return feed_events


def _check_rss_site(
    *,
    config: AppConfig,
    site_key: str,
    site_url: str,
    feed_client: FeedClient,
    markers: dict[str, str],
    already_seen_rss: set[str],
    handled_rss: set[str],
    pending_rss_seeds: list[str],
    new_markers: dict[str, str],
    feed_events: list[DetectedEvent],
    today_utc: str,
) -> None:
    # RSS branch is v1-restricted to LEOPOLD_POST (undefined feed payload
    # otherwise) -> WARNING + SKIP.
    et = _event_type_for(site_key)
    if et != EventType.LEOPOLD_POST:
        _log.warning(
            "website_diff: check_rss site %s maps to %s (not LEOPOLD_POST); "
            "skipping RSS branch (undefined feed payload)",
            site_key,
            et.value,
        )
        return

    seed_key = website_rss_seed_key(site_key)
    first_run = seed_key not in markers
    feed_url = f"{site_url.rstrip('/')}/feed"

    content = feed_client.fetch(feed_url)  # MonitorError -> per-site except
    if not _feed_is_valid(content):
        # Not a real feed (challenge / HTML / redirect) -> FAILED fetch: do NOT
        # write the marker, stay first-run, emit nothing.
        _log.warning(
            "website_diff: %s /feed did not parse as a valid feed; not seeding "
            "(stays first-run)",
            site_key,
        )
        return

    entries = parse_feed(content)
    site_seeds: list[str] = []
    for entry in entries:
        identifier = entry.guid  # guid-or-link resolved in parse_feed
        if identifier == "":
            continue
        if identifier in handled_rss:
            continue
        if first_run:
            site_seeds.append(identifier)
            handled_rss.add(identifier)
            continue
        if identifier in already_seen_rss:
            continue
        feed_events.append(
            DetectedEvent(
                event_type=EventType.LEOPOLD_POST,
                entity_key="",
                source=entry.source_title or site_key,
                title=entry.title,
                url=entry.link,
                identifier=identifier,
                published=entry.published,
                priority=Priority.HIGH,
                confidence=Confidence.HIGH,
                payload={
                    "excerpt": excerpt(
                        entry.summary, FEED_DESCRIPTION_EXCERPT_MAX
                    )
                },
            )
        )
        handled_rss.add(identifier)

    if first_run:
        # Valid feed (even with zero entries) -> WRITE the marker (don't stay
        # first-run forever).
        for guid in site_seeds:
            if guid not in pending_rss_seeds:
                pending_rss_seeds.append(guid)
        new_markers[seed_key] = today_utc


def _check_page_hash_site(
    *,
    site_key: str,
    site_url: str,
    site_keywords: tuple[str, ...],
    feed_client: FeedClient,
    conference_hashes: dict[str, ConferenceSnapshot],
    snapshot_updates: dict[str, ConferenceSnapshot],
    content_events: list[DetectedEvent],
    now: datetime,
) -> None:
    content = feed_client.fetch(site_url)  # MonitorError -> per-site except
    new_text = extract_normalized_text(content)

    if is_suspect_content(new_text):
        _log.warning(
            "website_diff: site %s (%s) returned suspect/short content "
            "(len=%d); skipping (not seeded)",
            site_key,
            site_url,
            len(new_text),
        )
        return

    new_hash = content_hash(new_text)
    nkey = website_snapshot_key(site_key)
    snap = conference_hashes.get(nkey)

    if snap is None:
        snapshot_updates[nkey] = ConferenceSnapshot(new_hash, new_text)
        return
    if snap.hash == new_hash:
        return  # unchanged

    old_text = snap.text
    snapshot_updates[nkey] = ConferenceSnapshot(new_hash, new_text)  # Option A

    # Keyword gate: empty keywords -> alert on ANY change; else gate on the
    # truly-changed lines.
    gated_in = True
    if site_keywords:
        changed = changed_lines(old_text, new_text)
        changed_text = "\n".join(changed)
        gated_in = matches_keywords((changed_text,), site_keywords)

    if gated_in:
        content_events.append(
            DetectedEvent(
                event_type=EventType.WEBSITE_DIFF,
                entity_key="",
                source=site_key,
                title=f"{site_key} changed",
                url=site_url,
                identifier=f"{nkey}@{new_hash[:12]}",
                published=now,
                priority=Priority.MEDIUM,
                confidence=Confidence.MEDIUM,
                payload={"diff": make_diff(old_text, new_text, DIFF_SNIPPET_MAX)},
            )
        )
    # else: snapshot advanced (silent update); emit nothing.
