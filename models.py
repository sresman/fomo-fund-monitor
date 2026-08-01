from __future__ import annotations

"""Shared cross-cutting dataclasses and enums consumed by later prompts.

Leaf module: imports nothing from the app (so ``constants.py`` and ``config.py``
can import from it without cycles). The single intra-app import direction is
``constants -> models`` and ``config -> models``; ``models`` imports nothing
back.

Enums are string-valued for clean YAML/JSON round-trips.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EventType(Enum):
    """Kinds of detected events. Values mirror the ``alert_routing`` keys in
    ``config.yaml``; the config loader parses routing keys into these members
    and enforces an exact-set match."""

    FILING_13F = "filing_13f"
    FILING_SC13 = "filing_sc13"
    FILING_FORM4 = "filing_form4"
    FILING_OTHER = "filing_other"
    YOUTUBE_HIGH = "youtube_high"
    YOUTUBE_MEDIUM = "youtube_medium"
    PODCAST_RSS = "podcast_rss"
    GOOGLE_NEWS = "google_news"
    CNBC_VIDEO = "cnbc_video"
    CONFERENCE_CHANGE = "conference_change"
    LEOPOLD_POST = "leopold_post"
    WEBSITE_DIFF = "website_diff"


class Priority(Enum):
    """Alert priority."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Confidence(Enum):
    """Match confidence (e.g. the YouTube filter uses this to split
    MEDIUM/HIGH)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertChannel(Enum):
    """Delivery channels; values match the ``alert_routing`` channel strings."""

    EMAIL = "email"
    SMS = "sms"


class MonitorName(Enum):
    """Monitor identifiers. Values match ``monitor_intervals`` keys in
    ``config.yaml``. Drives ``constants.MONITOR_NAMES``, which the loader uses to
    require/validate interval keys and which ``StateStore`` uses to validate
    ``record_run``."""

    EDGAR = "edgar"
    YOUTUBE = "youtube"
    PODCAST_RSS = "podcast_rss"
    GOOGLE_NEWS = "google_news"
    CNBC = "cnbc"
    CONFERENCE_PAGES = "conference_pages"
    WEBSITE_DIFF = "website_diff"


@dataclass(frozen=True)
class DetectedEvent:
    """Raw output every monitor produces.

    Shallow-frozen: the ``payload`` dict is itself mutable per its own type; we
    do not deep-freeze it.

    Payload convention: monitors stringify richer values at their boundary
    before constructing the event -- join list values with ``"\\n"`` and
    ISO-format datetimes (``dt.isoformat()``). This gives the diff snippets and
    alert formatters a single, predictable string-in/string-out contract, so the
    model stays ``Any``-free as ``dict[str, str]``.
    """

    event_type: EventType
    entity_key: str  # which entity (e.g. "atreides"), or "" if N/A
    source: str  # human label, e.g. "All-In Podcast", "SEC EDGAR"
    title: str
    url: str
    identifier: str  # dedupe key (accession / video id / guid / url / hash)
    published: datetime | None  # filed/published date (tz-aware); None if unknown
    priority: Priority
    confidence: Confidence
    payload: dict[str, str]  # monitor-specific extras; string-in/string-out


@dataclass(frozen=True)
class Alert:
    """A routed, formatted alert the alerting layer sends."""

    event: DetectedEvent
    subject: str
    body: str
    channels: tuple[AlertChannel, ...]  # resolved from alert_routing
