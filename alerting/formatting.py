from __future__ import annotations

"""Pure presentation for the alerting layer -- no I/O, no env.

``build_alert(event, config) -> Alert`` produces subject + full email body +
routed channels. ``sms_body(event) -> str`` produces a unified, URL-preserving,
``SMS_MAX_LENGTH``-bounded SMS projection.

Subject prefixes (``SUBJECT_PREFIX_BY_EVENT``) and the per-``EventType`` email
body formatter table both have key sets equal to ``set(EventType)`` EXACTLY (a
generic fallback is retained as a runtime safety net, but tests prove full
coverage). ``published`` renders deterministically as ``%Y-%m-%d %H:%M UTC``
(astimezone-UTC, literal "UTC"); ``None`` -> ``"unknown"``. Snippet fields
(diff / excerpt / description) are code-point-capped at
``EMAIL_SNIPPET_MAX_LENGTH``.

Payload-key contract (populated by Prompts 3-5) -- formatters read ONLY these
optional ``payload`` keys, all via ``.get(k, "")`` with whitespace-only treated
as empty and the whole line omitted when empty:

    | EventType(s)                                             | payload keys read           |
    |----------------------------------------------------------|-----------------------------|
    | FILING_13F / FILING_SC13 / FILING_FORM4 / FILING_OTHER   | filing_type, period, note   |
    | YOUTUBE_HIGH / YOUTUBE_MEDIUM                             | person, duration, description |
    | PODCAST_RSS                                               | person, audio_url, description |
    | GOOGLE_NEWS                                               | query                       |
    | CNBC_VIDEO                                                | (none beyond event fields)  |
    | CONFERENCE_CHANGE                                         | diff                        |
    | LEOPOLD_POST                                              | excerpt                     |
    | WEBSITE_DIFF                                              | diff                        |

Full set of keys any formatter may read: person, filing_type, period, note,
duration, description, audio_url, query, diff, excerpt. ``payload`` is
``dict[str, str]`` (Prompt 1 stringifies lists/dates), so formatters only
interpolate strings.
"""

from datetime import timezone
from typing import Callable

from config import AppConfig
from constants import (
    EMAIL_SNIPPET_MAX_LENGTH,
    SMS_MAX_LENGTH,
    SMS_TRUNCATION_ELLIPSIS,
)
from models import Alert, DetectedEvent, EventType

_PUBLISHED_UNKNOWN = "unknown"

# Categories whose natural SMS lead is a person / entity name (not the title).
_PERSON_BEARING: frozenset[EventType] = frozenset(
    {
        EventType.FILING_13F,
        EventType.FILING_SC13,
        EventType.FILING_FORM4,
        EventType.FILING_OTHER,
        EventType.YOUTUBE_HIGH,
        EventType.YOUTUBE_MEDIUM,
        EventType.PODCAST_RSS,
    }
)

# Subject prefixes. Key set MUST equal set(EventType) exactly (asserted by test).
SUBJECT_PREFIX_BY_EVENT: dict[EventType, str] = {
    EventType.FILING_13F: "[SEC FILING]",
    EventType.FILING_SC13: "[SEC FILING]",
    EventType.FILING_FORM4: "[SEC FILING]",
    EventType.FILING_OTHER: "[SEC FILING]",
    EventType.YOUTUBE_HIGH: "[NEW VIDEO]",
    EventType.YOUTUBE_MEDIUM: "[NEW VIDEO]",
    EventType.PODCAST_RSS: "[NEW PODCAST]",
    EventType.GOOGLE_NEWS: "[NEWS]",
    EventType.CNBC_VIDEO: "[CNBC]",
    EventType.CONFERENCE_CHANGE: "[CONFERENCE]",
    EventType.LEOPOLD_POST: "[LEOPOLD]",
    EventType.WEBSITE_DIFF: "[WEBSITE]",
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _clean(value: str) -> str:
    """Strip a value; whitespace-only ⇒ empty (consistent with env layer)."""
    return value.strip()


def _payload(event: DetectedEvent, key: str) -> str:
    """Read a payload key, stripped; whitespace-only ⇒ empty."""
    return _clean(event.payload.get(key, ""))


def _render_published(event: DetectedEvent) -> str:
    """Deterministic UTC render: ``%Y-%m-%d %H:%M UTC``; ``None`` -> ``unknown``.

    Always normalizes to UTC and hardcodes the literal ``"UTC"`` suffix so the
    output is stable across machines/locales.
    """
    if event.published is None:
        return _PUBLISHED_UNKNOWN
    return event.published.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _cap_snippet(text: str) -> str:
    """Code-point-cap a raw snippet at ``EMAIL_SNIPPET_MAX_LENGTH``.

    If longer, slice to the cap minus room for the ellipsis, ``rstrip`` trailing
    whitespace, then append the ellipsis. Measured in code points, so multiline
    diffs cannot blow up SMTP.
    """
    if len(text) <= EMAIL_SNIPPET_MAX_LENGTH:
        return text
    room = EMAIL_SNIPPET_MAX_LENGTH - len(SMS_TRUNCATION_ELLIPSIS)
    if room < 0:
        room = 0
    return text[:room].rstrip() + SMS_TRUNCATION_ELLIPSIS


def _line(label: str, value: str) -> list[str]:
    """One ``"Label: value"`` line, or nothing when value is empty."""
    return [f"{label}: {value}"] if value else []


def _lead_name(event: DetectedEvent) -> str:
    """Person-bearing lead name: payload[person] -> entity_key -> source.

    Never returns a leading-blank name (source is a non-empty human label).
    """
    person = _payload(event, "person")
    if person:
        return person
    entity = _clean(event.entity_key)
    if entity:
        return entity
    return _clean(event.source)


# --------------------------------------------------------------------------- #
# Subject builders
# --------------------------------------------------------------------------- #


def _prefix(event: DetectedEvent) -> str:
    return SUBJECT_PREFIX_BY_EVENT.get(event.event_type, "[ALERT]")


def _subject_filing(event: DetectedEvent) -> str:
    filing_type = _payload(event, "filing_type")
    source = _clean(event.source)
    tail = f"{filing_type} filed" if filing_type else "new filing"
    if source:
        return f"{_prefix(event)} {source} — {tail}"
    return f"{_prefix(event)} {tail}"


def _subject_youtube(event: DetectedEvent) -> str:
    name = _lead_name(event)
    source = _clean(event.source)
    title = _clean(event.title)
    where = f" on {source}" if source else ""
    return f'{_prefix(event)} {name}{where} — "{title}"'


def _subject_podcast(event: DetectedEvent) -> str:
    name = _lead_name(event)
    source = _clean(event.source)
    title = _clean(event.title)
    where = f" on {source}" if source else ""
    return f'{_prefix(event)} {name}{where} — "{title}"'


def _subject_news(event: DetectedEvent) -> str:
    return f"{_prefix(event)} {_clean(event.title)}"


def _subject_cnbc(event: DetectedEvent) -> str:
    return f"{_prefix(event)} {_clean(event.title)}"


def _subject_conference(event: DetectedEvent) -> str:
    return f"{_prefix(event)} {_clean(event.source)} speaker page changed"


def _subject_leopold(event: DetectedEvent) -> str:
    return f'{_prefix(event)} New post — "{_clean(event.title)}"'


def _subject_website(event: DetectedEvent) -> str:
    return f"{_prefix(event)} {_clean(event.source)} changed"


_SUBJECT_BY_EVENT: dict[EventType, Callable[[DetectedEvent], str]] = {
    EventType.FILING_13F: _subject_filing,
    EventType.FILING_SC13: _subject_filing,
    EventType.FILING_FORM4: _subject_filing,
    EventType.FILING_OTHER: _subject_filing,
    EventType.YOUTUBE_HIGH: _subject_youtube,
    EventType.YOUTUBE_MEDIUM: _subject_youtube,
    EventType.PODCAST_RSS: _subject_podcast,
    EventType.GOOGLE_NEWS: _subject_news,
    EventType.CNBC_VIDEO: _subject_cnbc,
    EventType.CONFERENCE_CHANGE: _subject_conference,
    EventType.LEOPOLD_POST: _subject_leopold,
    EventType.WEBSITE_DIFF: _subject_website,
}


# --------------------------------------------------------------------------- #
# Email body builders
# --------------------------------------------------------------------------- #


def _body_filing(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Filing type", _payload(event, "filing_type"))
    lines += _line("Entity", _clean(event.source))
    lines += _line("Filed", _render_published(event))
    lines += _line("Period", _payload(event, "period"))
    lines += _line("Accession", _clean(event.identifier))
    lines += _line("Link", _clean(event.url))
    body = "\n".join(lines)
    note = _payload(event, "note")
    if note:
        body = f"{body}\n\n{note}"
    return body


def _body_youtube(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Title", _clean(event.title))
    lines += _line("Channel", _clean(event.source))
    lines += _line("Published", _render_published(event))
    lines += _line("Duration", _payload(event, "duration"))
    lines += _line("URL", _clean(event.url))
    lines += _line("Confidence", event.confidence.value.upper())
    body = "\n".join(lines)
    description = _payload(event, "description")
    if description:
        body = f"{body}\n\nDescription excerpt: {_cap_snippet(description)}"
    return body


def _body_podcast(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Show", _clean(event.source))
    lines += _line("Episode", _clean(event.title))
    lines += _line("Published", _render_published(event))
    lines += _line("Audio URL", _payload(event, "audio_url"))
    lines += _line("Link", _clean(event.url))
    body = "\n".join(lines)
    description = _payload(event, "description")
    if description:
        body = f"{body}\n\nDescription: {_cap_snippet(description)}"
    return body


def _body_news(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Headline", _clean(event.title))
    lines += _line("Source", _clean(event.source))
    lines += _line("Published", _render_published(event))
    lines += _line("URL", _clean(event.url))
    lines += _line("Query matched", _payload(event, "query"))
    return "\n".join(lines)


def _body_cnbc(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Title", _clean(event.title))
    lines += _line("Source", _clean(event.source))
    lines += _line("Published", _render_published(event))
    lines += _line("URL", _clean(event.url))
    return "\n".join(lines)


def _body_conference(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Conference", _clean(event.source))
    lines += _line("URL", _clean(event.url))
    lines += _line("Detected", _render_published(event))
    diff = _payload(event, "diff")
    if diff:
        lines += _line("Change", _cap_snippet(diff))
    return "\n".join(lines)


def _body_leopold(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Title", _clean(event.title))
    lines += _line("Source", _clean(event.source))
    lines += _line("Published", _render_published(event))
    lines += _line("URL", _clean(event.url))
    body = "\n".join(lines)
    excerpt = _payload(event, "excerpt")
    if excerpt:
        body = f"{body}\n\nExcerpt: {_cap_snippet(excerpt)}"
    return body


def _body_website(event: DetectedEvent) -> str:
    lines: list[str] = []
    lines += _line("Site", _clean(event.source))
    lines += _line("URL", _clean(event.url))
    lines += _line("Detected", _render_published(event))
    diff = _payload(event, "diff")
    if diff:
        lines += _line("Change", _cap_snippet(diff))
    return "\n".join(lines)


_EMAIL_BODY_BY_EVENT: dict[EventType, Callable[[DetectedEvent], str]] = {
    EventType.FILING_13F: _body_filing,
    EventType.FILING_SC13: _body_filing,
    EventType.FILING_FORM4: _body_filing,
    EventType.FILING_OTHER: _body_filing,
    EventType.YOUTUBE_HIGH: _body_youtube,
    EventType.YOUTUBE_MEDIUM: _body_youtube,
    EventType.PODCAST_RSS: _body_podcast,
    EventType.GOOGLE_NEWS: _body_news,
    EventType.CNBC_VIDEO: _body_cnbc,
    EventType.CONFERENCE_CHANGE: _body_conference,
    EventType.LEOPOLD_POST: _body_leopold,
    EventType.WEBSITE_DIFF: _body_website,
}


def _generic_body(event: DetectedEvent) -> str:
    """Runtime safety net for a hypothetical unmapped EventType. Tests prove the
    table is exhaustive, so this is never reached for defined types."""
    lines: list[str] = []
    lines += _line("Title", _clean(event.title))
    lines += _line("Source", _clean(event.source))
    lines += _line("Published", _render_published(event))
    lines += _line("URL", _clean(event.url))
    return "\n".join(lines)


def _subject(event: DetectedEvent) -> str:
    builder = _SUBJECT_BY_EVENT.get(event.event_type)
    if builder is None:  # pragma: no cover - exhaustiveness asserted by tests
        return f"{_prefix(event)} {_clean(event.title)}"
    return builder(event)


def _email_body(event: DetectedEvent) -> str:
    builder = _EMAIL_BODY_BY_EVENT.get(event.event_type, _generic_body)
    return builder(event)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def build_alert(event: DetectedEvent, config: AppConfig) -> Alert:
    """Pure: subject + full email body + routed channels (from routing).

    ``config.alert_routing`` is exact-set over all ``EventType`` (Prompt 1), so
    the lookup cannot ``KeyError``.
    """
    subject = _subject(event)
    body = _email_body(event)
    channels = config.alert_routing[event.event_type]
    return Alert(event=event, subject=subject, body=body, channels=channels)


def _truncate_text(text: str, budget: int) -> str:
    """Trim ``text`` to ``budget`` code points, reserving room for the ellipsis.

    ``budget`` is the maximum length of the RESULT. Returns ``""`` when no room.
    """
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    ell = SMS_TRUNCATION_ELLIPSIS
    if budget <= len(ell):
        return ell[:budget]
    return text[: budget - len(ell)].rstrip() + ell


def sms_body(event: DetectedEvent) -> str:
    """Unified, URL-preserving SMS projection, ``<= SMS_MAX_LENGTH``.

    Person-bearing categories lead with the person/entity name; non-person
    categories lead with the event ``title`` (never duplicating ``source``).
    Degradation ladder when over the cap: drop source, then drop name/title,
    then (last resort) truncate the URL. The URL is appended last and preserved
    intact whenever possible.
    """
    prefix = _prefix(event)
    url = _clean(event.url)

    if event.event_type in _PERSON_BEARING:
        lead = _lead_name(event)
    else:
        lead = _clean(event.title)
    source = _clean(event.source)

    # Full assembled form.
    if lead and source:
        full = f"{prefix} {lead} — {source} · {url}"
    elif lead:
        full = f"{prefix} {lead} · {url}"
    elif url:
        full = f"{prefix} · {url}"
    else:
        full = prefix
    if len(full) <= SMS_MAX_LENGTH:
        return full

    # Step 1: drop source.
    if lead:
        no_source = f"{prefix} {lead} · {url}"
        if len(no_source) <= SMS_MAX_LENGTH:
            return no_source

    # Step 2: drop name/title too.
    no_lead = f"{prefix} · {url}"
    if len(no_lead) <= SMS_MAX_LENGTH:
        return no_lead

    # Step 3: URL alone still too long -> truncate URL (broken-link fallback).
    return url[:SMS_MAX_LENGTH]
