from __future__ import annotations

"""Tests for the conference_pages monitor (monitors/conference_pages.py).

Fake ``FeedClient`` by URL. No network. Content-hash Option-A semantics."""

from datetime import datetime, timezone

import pytest

from alerting.formatting import build_alert
from config import AppConfig
from constants import DIFF_SNIPPET_MAX
from errors import MonitorError
from models import AlertChannel, Confidence, EventType, Priority
from monitors._common import conference_snapshot_key, website_snapshot_key
from monitors._content_hash import content_hash, extract_normalized_text
from monitors.conference_pages import check_conference_pages
from state_manager import ConferenceSnapshot, SeenAppearances, StateStore

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)

# sample_config conference_pages: bic_speakers (keywords Baker, Aschenbrenner),
# fii_speakers (keywords Baker, Aschenbrenner).
BIC_URL = "https://bostoninvestmentconference.com/speakers"
FII_URL = "https://fii-institute.org"
BIC_KEY = "bic_speakers"


def _page(*speakers: str) -> bytes:
    body = "".join(f"<p>Speaker: {s}</p>" for s in speakers)
    return (
        f"<html><body><h1>Boston Investment Conference Speakers</h1>"
        f"{body}<footer>Conference footer text padding padding padding</footer>"
        f"</body></html>"
    ).encode("utf-8")


class FakeFeedClient:
    def __init__(
        self, by_url: dict[str, bytes], raise_for: frozenset[str] = frozenset()
    ) -> None:
        self._by_url = by_url
        self._raise_for = raise_for
        self.fetched: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        if url in self._raise_for:
            raise MonitorError(f"boom {url}")
        return self._by_url.get(url, _page("Placeholder Person One Two Three"))


def _both_urls(content: bytes) -> dict[str, bytes]:
    return {BIC_URL: content, FII_URL: _page("Neutral Person Padding Padding")}


def _seed_snapshot(store: StateStore, key: str, content: bytes) -> None:
    text = extract_normalized_text(content)
    app = store.load_seen_appearances()
    app.conference_hashes[conference_snapshot_key(key)] = ConferenceSnapshot(
        content_hash(text), text
    )
    store.save_seen_appearances(app)


# --------------------------------------------------------------------------- #


def test_now_naive_raises(scrape_config: AppConfig, store: StateStore) -> None:
    client = FakeFeedClient({})
    with pytest.raises(ValueError):
        check_conference_pages(scrape_config, store, client, datetime(2026, 7, 22))


def test_first_run_seeds_no_emit(
    scrape_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient(_both_urls(_page("Gavin Baker padding padding")))
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert conference_snapshot_key(BIC_KEY) in reloaded.conference_hashes


def test_min_length_page_not_seeded(
    scrape_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    tiny = b"<html><body>hi</body></html>"
    client = FakeFeedClient(_both_urls(tiny))
    with caplog.at_level("WARNING"):
        events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    assert conference_snapshot_key(BIC_KEY) not in reloaded.conference_hashes


def test_waf_challenge_page_not_seeded(
    scrape_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    waf = (
        b"<html><body><p>Please wait, checking your browser before "
        b"accessing this site. This will take a few seconds.</p></body></html>"
    )
    client = FakeFeedClient(_both_urls(waf))
    with caplog.at_level("WARNING"):
        check_conference_pages(scrape_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert conference_snapshot_key(BIC_KEY) not in reloaded.conference_hashes


def test_fetch_fail_not_seeded_isolated(
    scrape_config: AppConfig, store: StateStore
) -> None:
    client = FakeFeedClient({}, raise_for=frozenset({BIC_URL}))
    check_conference_pages(scrape_config, store, client, NOW)
    reloaded = store.load_seen_appearances()
    assert conference_snapshot_key(BIC_KEY) not in reloaded.conference_hashes
    # fii still seeded (isolation).
    assert conference_snapshot_key("fii_speakers") in reloaded.conference_hashes


def test_unchanged_hash_no_events(
    scrape_config: AppConfig, store: StateStore
) -> None:
    content = _page("Gavin Baker padding padding")
    _seed_snapshot(store, BIC_KEY, content)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    client = FakeFeedClient(_both_urls(content))
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []


def test_changed_keyword_added_emits(
    scrape_config: AppConfig, store: StateStore
) -> None:
    old = _page("Someone Else Padding Padding")
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    new = _page("Someone Else Padding Padding", "Gavin Baker keynote AI")
    client = FakeFeedClient(_both_urls(new))
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.CONFERENCE_CHANGE
    assert ev.payload["diff"]
    assert ev.identifier.startswith(f"{conference_snapshot_key(BIC_KEY)}@")
    # snapshot advanced.
    reloaded = store.load_seen_appearances()
    snap = reloaded.conference_hashes[conference_snapshot_key(BIC_KEY)]
    assert snap.hash == content_hash(extract_normalized_text(new))


def test_static_footer_keyword_unrelated_change_no_emit(
    scrape_config: AppConfig, store: StateStore
) -> None:
    """Keyword only in an UNCHANGED footer; the change is elsewhere -> ZERO
    events but snapshot advanced."""
    old = (
        b"<html><body><p>Agenda item one padding padding</p>"
        b"<footer>Organized by the Baker committee padding</footer>"
        b"</body></html>"
    )
    new = (
        b"<html><body><p>Agenda item TWO padding padding changed</p>"
        b"<footer>Organized by the Baker committee padding</footer>"
        b"</body></html>"
    )
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []
    reloaded = store.load_seen_appearances()
    snap = reloaded.conference_hashes[conference_snapshot_key(BIC_KEY)]
    assert snap.hash == content_hash(extract_normalized_text(new))  # advanced


def test_removed_keyword_line_emits(
    scrape_config: AppConfig, store: StateStore
) -> None:
    old = _page("Gavin Baker keynote", "Other Person Padding Padding")
    new = _page("Other Person Padding Padding")  # Baker cancelled
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert len(events) == 1


def test_moved_keyword_line_no_emit(
    scrape_config: AppConfig, store: StateStore
) -> None:
    old = _page("Gavin Baker keynote", "Other Person Padding", "Third Person Pad")
    new = _page("Other Person Padding", "Third Person Pad", "Gavin Baker keynote")
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []


def test_unique_per_change_identifier(
    scrape_config: AppConfig, store: StateStore
) -> None:
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    old = _page("Base Person Padding Padding")
    _seed_snapshot(store, BIC_KEY, old)

    new1 = _page("Base Person Padding Padding", "Gavin Baker one")
    client1 = FakeFeedClient({BIC_URL: new1, FII_URL: _page("Neutral Person Padding Padding")})
    id1 = check_conference_pages(scrape_config, store, client1, NOW)[0].identifier

    new2 = _page("Base Person Padding Padding", "Gavin Baker two different")
    client2 = FakeFeedClient({BIC_URL: new2, FII_URL: _page("Neutral Person Padding Padding")})
    id2 = check_conference_pages(scrape_config, store, client2, NOW)[0].identifier
    assert id1 != id2


def test_namespaced_keys_no_collision(store: StateStore) -> None:
    # Same raw key under both namespaces must not overwrite each other.
    app = SeenAppearances()
    app.conference_hashes[conference_snapshot_key("shared")] = ConferenceSnapshot(
        "h1", "conf text"
    )
    app.conference_hashes[website_snapshot_key("shared")] = ConferenceSnapshot(
        "h2", "site text"
    )
    store.save_seen_appearances(app)
    reloaded = store.load_seen_appearances()
    assert reloaded.conference_hashes[conference_snapshot_key("shared")].hash == "h1"
    assert reloaded.conference_hashes[website_snapshot_key("shared")].hash == "h2"


def test_volatile_html_no_event(
    scrape_config: AppConfig, store: StateStore
) -> None:
    old = (
        b"<html><head><script>var b='aaa'</script></head><body>"
        b"<p>Gavin Baker keynote padding padding padding</p></body></html>"
    )
    new = (
        b"<html><head><script>var b='zzz999'</script></head><body>"
        b"<p>Gavin Baker keynote padding padding padding</p></body></html>"
    )
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})
    events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []


def test_diff_capped(scrape_config: AppConfig, store: StateStore) -> None:
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    old = _page(*[f"Person number {i} padding" for i in range(300)])
    _seed_snapshot(store, BIC_KEY, old)
    new = _page(*[f"Person number {i} padding" for i in range(300)], "Gavin Baker AI")
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})
    ev = check_conference_pages(scrape_config, store, client, NOW)[0]
    assert len(ev.payload["diff"]) <= DIFF_SNIPPET_MAX


def test_save_failure_suppresses_content_events(
    scrape_config: AppConfig,
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old = _page("Someone Else Padding Padding")
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    new = _page("Someone Else Padding Padding", "Gavin Baker keynote AI")
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})

    real_load = store.load_seen_appearances

    def _boom(_data: SeenAppearances) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_seen_appearances", _boom)
    with caplog.at_level("ERROR"):
        events = check_conference_pages(scrape_config, store, client, NOW)
    assert events == []  # suppressed
    # snapshot NOT advanced (save failed).
    reloaded = real_load()
    snap = reloaded.conference_hashes[conference_snapshot_key(BIC_KEY)]
    assert snap.hash == content_hash(extract_normalized_text(old))


def test_empty_save_skip(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _page("Gavin Baker padding padding")
    _seed_snapshot(store, BIC_KEY, content)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    calls: list[int] = []
    real_save = store.save_seen_appearances

    def _counting(data: SeenAppearances) -> None:
        calls.append(1)
        real_save(data)

    monkeypatch.setattr(store, "save_seen_appearances", _counting)
    client = FakeFeedClient(_both_urls(content))
    check_conference_pages(scrape_config, store, client, NOW)
    assert calls == []  # no save because nothing pending


def test_state_read_failure_fatal(
    scrape_config: AppConfig, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> SeenAppearances:
        raise MonitorError("state read boom")

    monkeypatch.setattr(store, "load_seen_appearances", _boom)
    client = FakeFeedClient({})
    with pytest.raises(MonitorError):
        check_conference_pages(scrape_config, store, client, NOW)


def test_lost_alert_tradeoff_unchanged_after_change(
    scrape_config: AppConfig, store: StateStore
) -> None:
    old = _page("Someone Else Padding Padding")
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    new = _page("Someone Else Padding Padding", "Gavin Baker keynote AI")
    fii = _page("Neutral Person Padding Padding")
    client1 = FakeFeedClient({BIC_URL: new, FII_URL: fii})
    assert len(check_conference_pages(scrape_config, store, client1, NOW)) == 1
    # Immediately-following unchanged run -> zero events.
    client2 = FakeFeedClient({BIC_URL: new, FII_URL: fii})
    assert check_conference_pages(scrape_config, store, client2, NOW) == []


def test_event_fields_and_build_alert(
    scrape_config: AppConfig, store: StateStore
) -> None:
    old = _page("Someone Else Padding Padding")
    _seed_snapshot(store, BIC_KEY, old)
    _seed_snapshot(store, "fii_speakers", _page("Neutral Person Padding Padding"))
    new = _page("Someone Else Padding Padding", "Gavin Baker keynote AI")
    client = FakeFeedClient({BIC_URL: new, FII_URL: _page("Neutral Person Padding Padding")})
    ev = check_conference_pages(scrape_config, store, client, NOW)[0]
    assert ev.source == "Boston Investment Conference"
    assert ev.published == NOW
    assert ev.confidence == Confidence.MEDIUM
    assert ev.priority == Priority.LOW
    alert = build_alert(ev, scrape_config)
    assert set(alert.channels) == {AlertChannel.EMAIL}
    assert ev.payload["diff"] in alert.body or "Change" in alert.body
