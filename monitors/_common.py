from __future__ import annotations

"""Shared helpers for the RSS-family FEED monitors (Prompt 4).

Holds the feed-fetch transport client, keyword matching, the feed parser, the
per-source seed-key helpers, ``excerpt``, ``surname_of``, and the reload-merge
helper ``merge_appearances``. The three FEED monitors (youtube, podcast_rss,
google_news) share this scaffold.

mypy seams (strict, no bare ``Any`` at boundaries):
- ``HttpGetter`` / ``ResponseLike`` Protocols for the feed transport, so fakes
  implement the Protocols directly (no subclassing ``requests``).
- ``parse_feed`` reads feedparser's dynamic output defensively into ``object``
  locals then narrows to ``str``/``None``. Not fully Any-free but CONTAINED to
  ``parse_feed``.

Deferred heavy imports: ``feedparser`` is imported INSIDE ``parse_feed`` (so
importing this module -- and youtube.py, which imports helpers from here -- does
NOT require feedparser). ``requests`` is a hard dep and imported at module top
only for the default adapter's exception types + the real HTTP call.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Mapping, Protocol, Sequence

import requests

from constants import (
    APPEARANCE_FRAMING_STEMS,
    APPEARANCE_FRAMING_WINDOW_CHARS,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
)
from constants import (
    SEED_KEY_CNBC_PREFIX,
    SEED_KEY_NEWS_PREFIX,
    SEED_KEY_PODCAST_PREFIX,
    SEED_KEY_WEBSITE_RSS_PREFIX,
    SEED_KEY_YOUTUBE_PREFIX,
)
from errors import MonitorError
from state_manager import ConferenceSnapshot, SeenAppearances

_log = logging.getLogger(__name__)

# Suffix tokens dropped when picking a surname (case-insensitive, trailing dot
# stripped).
_SURNAME_SUFFIXES: frozenset[str] = frozenset(
    {"jr", "sr", "ii", "iii", "iv"}
)


# --------------------------------------------------------------------------- #
# Feed transport (Protocol seams + default requests adapter)
# --------------------------------------------------------------------------- #


class ResponseLike(Protocol):
    """Minimal structural view of an HTTP response (requests.Response fits).

    ``content`` is a read-only property so ``requests.Response`` (whose
    ``content`` is a property) satisfies the Protocol structurally.
    """

    @property
    def content(self) -> bytes: ...

    def raise_for_status(self) -> None: ...


class HttpGetter(Protocol):
    """Structural view of the one HTTP call the feed client needs."""

    def get(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> ResponseLike: ...


class FeedClient(Protocol):
    """The seam the monitors depend on: URL in, raw bytes out."""

    def fetch(self, url: str) -> bytes: ...


class _RequestsAdapter:
    """Default ``HttpGetter`` over ``requests`` (typed; no ``type: ignore``)."""

    def get(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> ResponseLike:
        return requests.get(url, headers=dict(headers), timeout=timeout)


class RequestsFeedClient:
    """Concrete ``FeedClient`` over an injectable ``HttpGetter``.

    Tests inject a fake ``HttpGetter`` returning canned bytes (or raising), so no
    network is touched. NO retry loop (unlike EDGAR) -- ``HTTP_MAX_RETRIES`` is
    deliberately not used. Any transport fault raises ``MonitorError``.
    """

    def __init__(self, http: HttpGetter | None = None) -> None:
        self._http: HttpGetter = http if http is not None else _RequestsAdapter()

    def fetch(self, url: str) -> bytes:
        try:
            resp = self._http.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=float(HTTP_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            raise MonitorError(f"feed fetch failed for {url}: {exc}") from exc
        except MonitorError:
            raise
        except Exception as exc:  # noqa: BLE001 -- any getter fault -> MonitorError
            raise MonitorError(f"feed fetch failed for {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# FeedEntry + parse_feed (deferred feedparser)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeedEntry:
    guid: str  # entry id/guid, fallback to link (dedupe key); "" if none
    title: str
    link: str
    summary: str  # description/summary text
    published: datetime | None  # tz-aware UTC or None
    enclosure_url: str  # first (audio) enclosure href, or ""
    source_title: str  # entry <source> else feed <title> else link-domain, or ""


def _obj_str(v: object) -> str:
    """Narrow a dynamic feedparser value to ``str`` (non-str -> "")."""
    return v if isinstance(v, str) else ""


def parse_feed(content: bytes) -> tuple[FeedEntry, ...]:
    """Parse RSS/Atom BYTES into a tuple of ``FeedEntry``.

    Transport is the ``FeedClient``'s responsibility -- this operates on bytes
    only and NEVER raises ``MonitorError`` for malformed/bozo XML (feedparser is
    lenient; a bad document yields whatever entries it can, possibly none).
    Missing feedparser (deferred import fails) -> ``MonitorError``.
    """
    try:
        import feedparser  # type: ignore[import-untyped]  # no stubs; deferred
    except ModuleNotFoundError as exc:
        raise MonitorError("feedparser not installed") from exc

    parsed = feedparser.parse(content)
    feed_obj: object = getattr(parsed, "feed", None)
    feed_title = ""
    if isinstance(feed_obj, Mapping):
        feed_title = _obj_str(feed_obj.get("title"))

    entries_obj: object = getattr(parsed, "entries", None)
    if not isinstance(entries_obj, list):
        return ()

    results: list[FeedEntry] = []
    for entry_obj in entries_obj:
        if not isinstance(entry_obj, Mapping):
            continue
        entry: Mapping[str, object] = entry_obj

        guid = (
            _obj_str(entry.get("id"))
            or _obj_str(entry.get("guid"))
            or _obj_str(entry.get("link"))
        )
        if guid == "":
            continue

        title = _obj_str(entry.get("title"))
        link = _obj_str(entry.get("link"))
        summary = _obj_str(entry.get("summary")) or _obj_str(
            entry.get("description")
        )

        published = _parse_feed_published(entry.get("published_parsed"))
        enclosure_url = _first_enclosure_url(entry.get("enclosures"))
        source_title = _entry_source_title(entry.get("source"), feed_title, link)

        results.append(
            FeedEntry(
                guid=guid,
                title=title,
                link=link,
                summary=summary,
                published=published,
                enclosure_url=enclosure_url,
                source_title=source_title,
            )
        )
    return tuple(results)


def _parse_feed_published(value: object) -> datetime | None:
    """feedparser exposes ``published_parsed`` as a UTC ``time.struct_time`` (it
    normalizes ``*_parsed`` structs to UTC). Build a UTC-aware datetime directly
    -- never construct naive + ``.astimezone`` (which would assume local time)."""
    if value is None:
        return None
    try:
        year = int(value[0])  # type: ignore[index]
        month = int(value[1])  # type: ignore[index]
        day = int(value[2])  # type: ignore[index]
        hour = int(value[3])  # type: ignore[index]
        minute = int(value[4])  # type: ignore[index]
        second = int(value[5])  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return None
    try:
        return datetime(
            year, month, day, hour, minute, second, tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _first_enclosure_url(value: object) -> str:
    """First AUDIO enclosure href, else first enclosure href, else ""."""
    if not isinstance(value, list):
        return ""
    first_any = ""
    for enc in value:
        if not isinstance(enc, Mapping):
            continue
        href = _obj_str(enc.get("href")) or _obj_str(enc.get("url"))
        if href == "":
            continue
        if first_any == "":
            first_any = href
        enc_type = _obj_str(enc.get("type"))
        if enc_type.lower().startswith("audio"):
            return href
    return first_any


def _entry_source_title(source_obj: object, feed_title: str, link: str) -> str:
    """Prefer entry-level <source> title, else feed title, else link domain."""
    if isinstance(source_obj, Mapping):
        title = _obj_str(source_obj.get("title"))
        if title:
            return title
    if feed_title:
        return feed_title
    if link:
        import urllib.parse

        netloc = urllib.parse.urlparse(link).netloc
        if netloc:
            return netloc
    return ""


# --------------------------------------------------------------------------- #
# Matching / text helpers
# --------------------------------------------------------------------------- #


# Anything that looks like a URL is removed from a field before matching. Show
# notes are dense with links -- an episode's "Follow the crew" block alone can
# carry x.com/GavinSBaker -- and a name inside a URL is a citation, not an
# appearance. Deliberately greedy to the next whitespace: URLs do not contain
# spaces, and over-removing a token that merely looks like a URL is harmless
# next to a false alert.
_URL_IN_TEXT_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def matches_keywords(
    text_fields: Iterable[str], keywords: Sequence[str]
) -> bool:
    """True iff any non-blank ``keyword`` (case-insensitive) occurs as a WHOLE
    TOKEN in ANY single ``text_field`` (per-field, not a joined string), after
    URLs have been stripped from that field.

    Empty/whitespace keywords are ignored; empty ``keywords`` -> ``False``.

    Two guards, both added 2026-09-03 after an audit of every configured feed
    found 22 historical matches and ZERO of them genuine:

    * **URLs stripped.** ``emilybakerwhite`` in a BuzzFeed link matched "Baker";
      ``x.com/GavinSBaker`` in six All-In link dumps matched both "Baker" and
      "Gavin". A name inside a link is a citation, not an appearance.
    * **Whole-token matching.** ``bakeries``/``bakers``/``bakerlaw`` matched
      "Baker" as a bare substring. Boundaries are non-alphanumeric, so a keyword
      still matches across hyphens and punctuation ("Amodei-Gavin Baker",
      "Baker's", "Baker:") -- only alphanumeric run-ons are rejected.

    Multi-word keywords work unchanged: internal spaces are matched literally,
    with the token boundary applied at each end ("Gavin Baker" matches
    "Gavin Baker's", not "Gavin Bakerson").
    """
    fields_lower = [_URL_IN_TEXT_RE.sub(" ", f).lower() for f in text_fields]
    for kw in keywords:
        needle = kw.strip().lower()
        if needle == "":
            continue
        # (?<![a-z0-9]) / (?![a-z0-9]) rather than \b so that a keyword ending
        # in punctuation (e.g. "ep.") still behaves sensibly.
        pattern = re.compile(
            rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
        )
        for field_lower in fields_lower:
            if pattern.search(field_lower):
                return True
    return False


# Guest-framing stems, prefix-matched at a word boundary so "join" also covers
# joins / joined / joining. Compiled once.
_FRAMING_RE = re.compile(
    r"(?<![a-z])(?:"
    + "|".join(re.escape(s) for s in APPEARANCE_FRAMING_STEMS)
    + r")",
    re.IGNORECASE,
)


def is_first_party_appearance(
    title: str, summary: str, keywords: Sequence[str]
) -> bool:
    """True iff the episode looks like the PERSON APPEARING, not being mentioned.

    A configured feed is an official publisher, but an episode on it may merely
    reference the person: a show-notes link, a cross-reference to another
    podcast, a cited tweet. Two ways to qualify:

    1. The keyword is in the TITLE. Whoever is named in an episode title is the
       guest -- this covers 21 of the 30 qualifying episodes across the feeds.
    2. The keyword is in the description within
       ``APPEARANCE_FRAMING_WINDOW_CHARS`` of a guest-framing stem ("...Gavin
       Baker and Travis Kalanick JOIN the show!"). This covers the panel shows,
       All-In especially, that never name guests in the title.

    Audited over every configured feed: of 35 keyword matches, 30 qualify and 5
    do not -- two ILTB show-note cross-references to a Baker episode, an All-In
    tweet citation, a This Week in Startups link to a Baker tweet thread, and an
    Invested-by-Aleph clip retrospective. No genuine appearance is excluded;
    cross-checked against the transcript corpus.

    URLs are stripped by ``matches_keywords`` before matching, so a name inside
    a link never qualifies via either route.
    """
    if matches_keywords((title,), keywords):
        return True
    if not matches_keywords((summary,), keywords):
        return False

    haystack = _URL_IN_TEXT_RE.sub(" ", summary).lower()
    window = APPEARANCE_FRAMING_WINDOW_CHARS
    for kw in keywords:
        needle = kw.strip().lower()
        if needle == "":
            continue
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])")
        for match in pattern.finditer(haystack):
            start = max(0, match.start() - window)
            if _FRAMING_RE.search(haystack[start : match.end() + window]):
                return True
    return False


def excerpt(text: str, limit: int) -> str:
    """Strip + collapse internal whitespace + code-point cap at ``limit``.

    Kept local (duplicates the alerting ``_cap_snippet`` INTENT) so monitors
    never import the alerting layer. ``excerpt("", limit) == ""``.
    """
    collapsed = " ".join(text.split())
    if limit <= 0:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit]


def surname_of(person: str) -> str:
    """Last non-suffix token of ``person``, lowercased.

    Drops trailing suffix tokens (Jr/Sr/II/III/IV, case-insensitive, trailing
    dot stripped). A single-token name -> the whole name lowercased. Multi-word
    surnames ("van Dijk") are not handled (our entities are clean single
    surnames).
    """
    tokens = person.split()
    while tokens:
        last_clean = tokens[-1].rstrip(".").lower()
        if last_clean in _SURNAME_SUFFIXES and len(tokens) > 1:
            tokens = tokens[:-1]
        else:
            break
    if not tokens:
        return ""
    return tokens[-1].lower()


# --------------------------------------------------------------------------- #
# Seed-key helpers (per-source markers keys; all normalize via .strip())
# --------------------------------------------------------------------------- #


def youtube_seed_key(query: str) -> str:  # PER-QUERY (v3)
    return SEED_KEY_YOUTUBE_PREFIX + query.strip()


def podcast_seed_key(feed_url: str) -> str:
    return SEED_KEY_PODCAST_PREFIX + feed_url.strip()


def news_seed_key(query: str) -> str:
    return SEED_KEY_NEWS_PREFIX + query.strip()


def cnbc_seed_key(query: str) -> str:  # PER-QUERY (Prompt 5), urls bucket
    return SEED_KEY_CNBC_PREFIX + query.strip()


def website_rss_seed_key(site_key: str) -> str:  # PER-SITE (Prompt 5), rss_guids
    return SEED_KEY_WEBSITE_RSS_PREFIX + site_key.strip()


# --------------------------------------------------------------------------- #
# Content-hash snapshot key helpers (namespace the shared conference_hashes dict
# so conference_pages and website_diff page-hash can never collide, Prompt 5).
# --------------------------------------------------------------------------- #


def conference_snapshot_key(page_key: str) -> str:
    return f"conference:{page_key}"


def website_snapshot_key(site_key: str) -> str:
    return f"website:{site_key}"


# --------------------------------------------------------------------------- #
# Reload-merge helper
# --------------------------------------------------------------------------- #


def merge_appearances(
    fresh: SeenAppearances,
    bucket: Literal["youtube", "rss_guids", "urls"] | None,
    add_ids: Sequence[str],
    new_markers: dict[str, str],
    conference_hashes: Mapping[str, ConferenceSnapshot] | None = None,
) -> SeenAppearances:
    """Merge one monitor's first-run seeds + markers (+ content-hash snapshot
    updates) into a freshly-reloaded ``SeenAppearances`` (read-modify-write).

    - If ``bucket`` is not None: append ``add_ids`` into that named bucket,
      order-preserving dedup, never reordering existing entries.
    - Apply ``new_markers`` on top of ``fresh.markers`` (new/updated keys win),
      preserving markers written by OTHER monitors.
    - If ``conference_hashes`` is given (Prompt 5): APPLY each update onto
      ``fresh.conference_hashes`` (updating changed keys, ADDING new keys,
      preserving untouched keys). Keys are already namespaced by the caller
      (``conference:<key>`` / ``website:<key>``).
    - All OTHER buckets + untouched ``conference_hashes`` are preserved.

    ``bucket=None`` (or empty ``add_ids``) + no ``conference_hashes`` ->
    markers-only merge.
    """
    if bucket is not None and add_ids:
        target = _bucket_list(fresh, bucket)
        existing = set(target)
        for identifier in add_ids:
            if identifier == "":
                continue
            if identifier not in existing:
                target.append(identifier)
                existing.add(identifier)
    fresh.markers.update(new_markers)
    if conference_hashes:
        for key, snapshot in conference_hashes.items():
            fresh.conference_hashes[key] = snapshot
    return fresh


def _bucket_list(app: SeenAppearances, bucket: str) -> list[str]:
    if bucket == "youtube":
        return app.youtube
    if bucket == "rss_guids":
        return app.rss_guids
    return app.urls
