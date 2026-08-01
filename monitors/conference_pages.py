from __future__ import annotations

"""Conference speaker-page content-hash diff monitor (conference_pages, Prompt 5).

Per configured conference page: fetch -> extract normalized text -> WAF/min-length
guard -> hash. First run (namespaced snapshot key ABSENT) stages the snapshot and
emits nothing. Unchanged hash -> nothing. Changed hash -> compute a line-oriented
diff from the OLD text, gate on the TRULY-changed keyword lines (added-not-in-old
UNION removed-not-in-new), stage the snapshot advance, and if gated-in build a
CONFERENCE_CHANGE event. Persist-then-emit: ONE batched save at the end; content
events are returned ONLY if the save succeeded (else suppressed + re-detected next
run -- no double-alert). Does NOT send alerts (orchestrator, Prompt 6).

Snapshot timing = Option A (monitor advances the snapshot ON DETECTION): there is
no per-item id to hand the orchestrator, and advancing on detection avoids the
re-fire-forever loop. Snapshot keys are NAMESPACED (``conference:<key>``) in the
shared ``conference_hashes`` dict so they can never collide with website_diff
page-hash keys. The identifier is UNIQUE PER CHANGE (``conference:<key>@<hash12>``,
deterministic, no clock) so the orchestrator can dedupe it like any other event;
the snapshot advance remains the primary dedupe.

Accepted tradeoff (FLAG-LOST-ALERT): a content-diff whose DISPATCH fails
(Prompt 6, after we return it) is NOT retried -- the snapshot already advanced and
persisted, so the next genuine change re-alerts. Acceptable for content diffs.

Single-process / sequential assumption: NO file locking on the state file; the
reload-merge at save time mitigates same-run bucket clobbering; concurrent
processes are out of v1 scope.

HTML scraping is ALLOWED here (the "do not scrape HTML" rule is EDGAR-specific;
conference speaker pages have no structured feed). Scoped exception.
"""

import logging
from datetime import datetime

from config import AppConfig
from constants import DIFF_SNIPPET_MAX
from models import Confidence, DetectedEvent, EventType, Priority
from monitors._common import (
    FeedClient,
    conference_snapshot_key,
    matches_keywords,
    merge_appearances,
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


def _in_season(page_month_tuple: tuple[int, ...], now: datetime) -> bool:
    if not page_month_tuple:
        return True  # no season restriction -> always "in season"
    return now.month in page_month_tuple


def check_conference_pages(
    config: AppConfig,
    store: StateStore,
    feed_client: FeedClient,
    now: datetime,
) -> list[DetectedEvent]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware")

    seen = store.load_seen_appearances()  # fatal on StateError
    conference_hashes = seen.conference_hashes

    snapshot_updates: dict[str, ConferenceSnapshot] = {}
    content_events: list[DetectedEvent] = []

    for page in config.conference_pages:
        # season_months is informational only in v1 (DEBUG log; do NOT skip --
        # skipping off-season would miss early speaker-list announcements). The
        # "12h in-season" tuning is a future cron-frequency concern (Prompt 6).
        _log.debug(
            "conference_pages: page %s (%s) in_season=%s (season_months=%s)",
            page.key,
            page.conference,
            _in_season(page.season_months, now),
            page.season_months,
        )
        try:
            content = feed_client.fetch(page.url)
            new_text = extract_normalized_text(content)
        except Exception:  # noqa: BLE001 -- per-page isolation
            _log.exception(
                "conference_pages: page %s (%s) failed; skipping (stays "
                "first-run if not yet seeded)",
                page.key,
                page.url,
            )
            continue

        # WAF / min-length guard: do NOT seed, do NOT diff (SD-P5-8).
        if is_suspect_content(new_text):
            _log.warning(
                "conference_pages: page %s (%s) returned suspect/short content "
                "(len=%d); skipping (not seeded)",
                page.key,
                page.url,
                len(new_text),
            )
            continue

        new_hash = content_hash(new_text)
        nkey = conference_snapshot_key(page.key)
        snap = conference_hashes.get(nkey)

        if snap is None:
            # First run -> stage snapshot, emit nothing.
            snapshot_updates[nkey] = ConferenceSnapshot(new_hash, new_text)
            continue
        if snap.hash == new_hash:
            continue  # unchanged

        # Hash changed -> advance the snapshot (Option A) regardless of the gate.
        old_text = snap.text
        snapshot_updates[nkey] = ConferenceSnapshot(new_hash, new_text)

        changed = changed_lines(old_text, new_text)
        changed_text = "\n".join(changed)
        if matches_keywords((changed_text,), page.keywords):
            content_events.append(
                DetectedEvent(
                    event_type=EventType.CONFERENCE_CHANGE,
                    entity_key="",  # a page spans both people / neither
                    source=page.conference,
                    title=f"{page.conference} speaker page changed",
                    url=page.url,
                    identifier=f"{nkey}@{new_hash[:12]}",
                    published=now,
                    priority=Priority.LOW,
                    confidence=Confidence.MEDIUM,
                    payload={
                        "diff": make_diff(old_text, new_text, DIFF_SNIPPET_MAX)
                    },
                )
            )
        # else: snapshot advanced (silent update); emit nothing.

    if not snapshot_updates:
        return content_events  # empty-save skip; nothing pending

    try:
        fresh = store.load_seen_appearances()
        merged = merge_appearances(
            fresh, None, (), {}, conference_hashes=snapshot_updates
        )
        store.save_seen_appearances(merged)
    except Exception:  # noqa: BLE001 -- content events SUPPRESSED, re-detected next run
        _log.error(
            "conference_pages: failed to persist snapshot updates; SUPPRESSING "
            "%d content event(s) this run (re-detected next run; no "
            "double-alert)",
            len(content_events),
            exc_info=True,
        )
        return []

    return content_events
