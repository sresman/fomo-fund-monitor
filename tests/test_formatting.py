from __future__ import annotations

"""Tests for alerting.formatting -- pure subject/body/SMS builders."""

from datetime import datetime, timedelta, timezone

import pytest

from alerting.formatting import (
    SUBJECT_PREFIX_BY_EVENT,
    _EMAIL_BODY_BY_EVENT,
    build_alert,
    sms_body,
)
from config import AppConfig, load_config
from constants import SMS_MAX_LENGTH, SMS_TRUNCATION_ELLIPSIS
from models import Confidence, DetectedEvent, EventType, Priority
from tests.conftest import SAMPLE_CONFIG


def make_event(
    event_type: EventType,
    *,
    entity_key: str = "atreides",
    source: str = "SEC EDGAR",
    title: str = "Sample Title",
    url: str = "https://example.com/item",
    identifier: str = "id-123",
    published: datetime | None = datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc),
    priority: Priority = Priority.HIGH,
    confidence: Confidence = Confidence.HIGH,
    payload: dict[str, str] | None = None,
) -> DetectedEvent:
    return DetectedEvent(
        event_type=event_type,
        entity_key=entity_key,
        source=source,
        title=title,
        url=url,
        identifier=identifier,
        published=published,
        priority=priority,
        confidence=confidence,
        payload=payload if payload is not None else {},
    )


@pytest.fixture
def config() -> AppConfig:
    return load_config(SAMPLE_CONFIG)


# --------------------------------------------------------------------------- #
# Exhaustiveness
# --------------------------------------------------------------------------- #


def test_subject_prefix_table_is_exact() -> None:
    assert set(SUBJECT_PREFIX_BY_EVENT) == set(EventType)


def test_email_body_table_is_exact() -> None:
    assert set(_EMAIL_BODY_BY_EVENT) == set(EventType)


@pytest.mark.parametrize("event_type", list(EventType))
def test_every_event_type_formats(
    event_type: EventType, config: AppConfig
) -> None:
    alert = build_alert(make_event(event_type), config)
    assert alert.subject.startswith(SUBJECT_PREFIX_BY_EVENT[event_type])
    assert alert.channels == config.alert_routing[event_type]
    # Body renders published in the deterministic UTC form.
    assert "2026-07-22 14:30 UTC" in alert.body
    # sms_body never raises and respects the cap.
    body = sms_body(make_event(event_type))
    assert len(body) <= SMS_MAX_LENGTH


# --------------------------------------------------------------------------- #
# Subjects / fallback chain
# --------------------------------------------------------------------------- #


def test_youtube_subject_uses_person(config: AppConfig) -> None:
    ev = make_event(
        EventType.YOUTUBE_HIGH,
        source="YouTube",
        title="Chips talk",
        payload={"person": "Gavin Baker"},
    )
    assert build_alert(ev, config).subject == '[NEW VIDEO] Gavin Baker on YouTube — "Chips talk"'


def test_youtube_subject_falls_back_to_entity_key(config: AppConfig) -> None:
    ev = make_event(
        EventType.YOUTUBE_HIGH,
        entity_key="atreides",
        source="YouTube",
        title="Chips talk",
        payload={},
    )
    subject = build_alert(ev, config).subject
    assert "atreides" in subject
    # never a leading-blank name
    assert "[NEW VIDEO]  " not in subject


def test_youtube_subject_falls_back_to_source(config: AppConfig) -> None:
    ev = make_event(
        EventType.YOUTUBE_HIGH,
        entity_key="",
        source="YouTube",
        title="Chips talk",
        payload={},
    )
    subject = build_alert(ev, config).subject
    assert subject == '[NEW VIDEO] YouTube on YouTube — "Chips talk"'


def test_filing_subject_fallback_when_no_filing_type(config: AppConfig) -> None:
    ev = make_event(EventType.FILING_13F, source="Atreides", payload={})
    assert build_alert(ev, config).subject == "[SEC FILING] Atreides — new filing"


def test_filing_subject_with_filing_type(config: AppConfig) -> None:
    ev = make_event(
        EventType.FILING_13F, source="Atreides", payload={"filing_type": "13F-HR"}
    )
    assert build_alert(ev, config).subject == "[SEC FILING] Atreides — 13F-HR filed"


# --------------------------------------------------------------------------- #
# Payload line omission / raw display
# --------------------------------------------------------------------------- #


def test_absent_payload_line_omitted(config: AppConfig) -> None:
    ev = make_event(EventType.FILING_13F, payload={})
    body = build_alert(ev, config).body
    assert "Period:" not in body
    assert "Filing type:" not in body


def test_whitespace_only_payload_treated_as_empty(config: AppConfig) -> None:
    ev = make_event(EventType.FILING_13F, payload={"period": "   ", "filing_type": "13F-HR"})
    body = build_alert(ev, config).body
    assert "Period:" not in body
    assert "Filing type: 13F-HR" in body


def test_nonempty_payload_displayed_raw(config: AppConfig) -> None:
    ev = make_event(EventType.FILING_13F, payload={"period": "2026-Q2"})
    body = build_alert(ev, config).body
    assert "Period: 2026-Q2" in body


# --------------------------------------------------------------------------- #
# published rendering
# --------------------------------------------------------------------------- #


def test_published_normalized_to_utc(config: AppConfig) -> None:
    est = timezone(timedelta(hours=-5))
    ev = make_event(
        EventType.GOOGLE_NEWS,
        published=datetime(2026, 7, 22, 9, 30, tzinfo=est),
    )
    body = build_alert(ev, config).body
    # 09:30 EST == 14:30 UTC
    assert "2026-07-22 14:30 UTC" in body


def test_published_none_renders_unknown(config: AppConfig) -> None:
    ev = make_event(EventType.GOOGLE_NEWS, published=None)
    body = build_alert(ev, config).body
    assert "Published: unknown" in body


# --------------------------------------------------------------------------- #
# Snippet cap
# --------------------------------------------------------------------------- #


def test_snippet_capped(config: AppConfig) -> None:
    from constants import EMAIL_SNIPPET_MAX_LENGTH

    long_diff = "line\n" * (EMAIL_SNIPPET_MAX_LENGTH // 2)  # well over the cap
    ev = make_event(EventType.WEBSITE_DIFF, payload={"diff": long_diff})
    body = build_alert(ev, config).body
    change_idx = body.index("Change: ")
    snippet = body[change_idx + len("Change: ") :]
    assert len(snippet) <= EMAIL_SNIPPET_MAX_LENGTH
    assert snippet.endswith(SMS_TRUNCATION_ELLIPSIS)


# --------------------------------------------------------------------------- #
# SMS body -- content per category
# --------------------------------------------------------------------------- #


def test_sms_person_bearing_contains_name_source_url() -> None:
    ev = make_event(
        EventType.PODCAST_RSS,
        source="Invest Like the Best",
        url="https://pod.example/ep1",
        payload={"person": "Gavin Baker"},
    )
    body = sms_body(ev)
    assert "Gavin Baker" in body
    assert "Invest Like the Best" in body
    assert "https://pod.example/ep1" in body


def test_sms_non_person_uses_title_not_duplicated_source() -> None:
    ev = make_event(
        EventType.CNBC_VIDEO,
        source="CNBC",
        title="Baker on chip supply",
        url="https://cnbc.example/v",
    )
    body = sms_body(ev)
    assert "Baker on chip supply" in body
    assert "CNBC — CNBC" not in body


def test_sms_non_person_omits_empty_title() -> None:
    ev = make_event(
        EventType.GOOGLE_NEWS, source="News", title="", url="https://n.example/a"
    )
    body = sms_body(ev)
    assert body == "[NEWS] · https://n.example/a"


# --------------------------------------------------------------------------- #
# SMS truncation ladder
# --------------------------------------------------------------------------- #


def test_sms_exactly_at_limit_unchanged() -> None:
    # Construct a person-bearing message exactly SMS_MAX_LENGTH long.
    url = "https://ex.example/" + ("u" * 40)
    prefix = "[NEW PODCAST]"
    source = "Show"
    # message = "{prefix} {name} — {source} · {url}"
    fixed = f"{prefix}  — {source} · {url}"  # spaces around name placeholder
    name_len = SMS_MAX_LENGTH - len(fixed)
    name = "n" * name_len
    ev = make_event(
        EventType.PODCAST_RSS,
        source=source,
        url=url,
        payload={"person": name},
    )
    body = sms_body(ev)
    assert len(body) == SMS_MAX_LENGTH
    assert body == f"{prefix} {name} — {source} · {url}"


def test_sms_one_over_drops_source() -> None:
    url = "https://ex.example/x"
    prefix = "[NEW PODCAST]"
    source = "Show"
    fixed = f"{prefix}  — {source} · {url}"
    name_len = SMS_MAX_LENGTH - len(fixed) + 1  # one over
    name = "n" * name_len
    ev = make_event(
        EventType.PODCAST_RSS, source=source, url=url, payload={"person": name}
    )
    body = sms_body(ev)
    assert len(body) <= SMS_MAX_LENGTH
    assert source not in body  # source dropped
    assert url in body  # URL intact
    assert body == f"{prefix} {name} · {url}"


def test_sms_drops_name_when_still_over() -> None:
    url = "https://ex.example/y"
    prefix = "[NEW PODCAST]"
    # Make even "{prefix} {name} · {url}" exceed the cap so name is dropped.
    no_source_fixed = f"{prefix}  · {url}"
    name_len = SMS_MAX_LENGTH - len(no_source_fixed) + 1
    name = "n" * name_len
    ev = make_event(
        EventType.PODCAST_RSS, source="Show", url=url, payload={"person": name}
    )
    body = sms_body(ev)
    assert len(body) <= SMS_MAX_LENGTH
    assert body == f"{prefix} · {url}"


def test_sms_huge_url_truncated_last_resort() -> None:
    url = "https://ex.example/" + ("z" * (SMS_MAX_LENGTH + 100))
    ev = make_event(
        EventType.PODCAST_RSS, source="Show", url=url, payload={"person": "Baker"}
    )
    body = sms_body(ev)
    assert len(body) == SMS_MAX_LENGTH
    assert body == url[:SMS_MAX_LENGTH]
