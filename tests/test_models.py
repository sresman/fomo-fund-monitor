from __future__ import annotations

"""Tests for models.py enums."""

from models import AlertChannel, EventType, MonitorName


def test_event_type_value_matches_routing_convention() -> None:
    # Each EventType.value must be the lowercase routing-key string used in
    # config.yaml (drift guard).
    expected = {
        EventType.FILING_13F: "filing_13f",
        EventType.FILING_SC13: "filing_sc13",
        EventType.FILING_FORM4: "filing_form4",
        EventType.FILING_OTHER: "filing_other",
        EventType.YOUTUBE_HIGH: "youtube_high",
        EventType.YOUTUBE_MEDIUM: "youtube_medium",
        EventType.PODCAST_RSS: "podcast_rss",
        EventType.GOOGLE_NEWS: "google_news",
        EventType.CNBC_VIDEO: "cnbc_video",
        EventType.CONFERENCE_CHANGE: "conference_change",
        EventType.LEOPOLD_POST: "leopold_post",
        EventType.WEBSITE_DIFF: "website_diff",
    }
    # Every member covered and value-matched.
    assert set(expected.keys()) == set(EventType)
    for member, value in expected.items():
        assert member.value == value


def test_alert_channel_values() -> None:
    assert AlertChannel.EMAIL.value == "email"
    assert AlertChannel.SMS.value == "sms"


def test_monitor_name_values() -> None:
    expected = {
        MonitorName.EDGAR: "edgar",
        MonitorName.YOUTUBE: "youtube",
        MonitorName.PODCAST_RSS: "podcast_rss",
        MonitorName.GOOGLE_NEWS: "google_news",
        MonitorName.CNBC: "cnbc",
        MonitorName.CONFERENCE_PAGES: "conference_pages",
        MonitorName.WEBSITE_DIFF: "website_diff",
    }
    assert set(expected.keys()) == set(MonitorName)
    for member, value in expected.items():
        assert member.value == value
