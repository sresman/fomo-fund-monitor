from __future__ import annotations

"""Tests for the EDGAR monitor (``monitors/edgar.py``).

Fully type-annotated (mypy strict scope). A typed fake ``EdgarClient`` is the
mock seam -- never real HTTP. The concrete-client tests inject a typed fake
``requests.Session``/response plus a no-op ``sleep``; still no network.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from alerting.formatting import build_alert
from config import AppConfig, load_config
from constants import (
    EDGAR_MIN_REQUEST_INTERVAL_SECONDS,
    EDGAR_RETRY_BACKOFF_SECONDS,
    HTTP_MAX_RETRIES,
)
from errors import MonitorError
from models import Confidence, EventType, Priority
from monitors.edgar import (
    EdgarHttpClient,
    FilingRecord,
    SubmissionsResponse,
    _parse_submissions,
    check_edgar,
)
from state_manager import StateStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"
EDGAR_CONFIG = FIXTURES_DIR / "edgar_config.yaml"

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)

ATREIDES_CIK = "0001777813"
SA_CIK = "0002045724"
EMPTY_CIK = "0000000042"


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


@pytest.fixture
def edgar_config() -> AppConfig:
    return load_config(EDGAR_CONFIG)


class FakeEdgarClient:
    """Typed fake client. RAISES ``MonitorError`` for an unknown CIK (matches a
    real 404)."""

    def __init__(self, by_cik: dict[str, SubmissionsResponse]) -> None:
        self._by_cik = by_cik

    def fetch_submissions(self, cik: str) -> SubmissionsResponse:
        try:
            return self._by_cik[cik]
        except KeyError as exc:
            raise MonitorError(f"unknown CIK {cik}") from exc


class RaisingFakeEdgarClient:
    """Fake that raises an arbitrary (non-MonitorError) exception for a target
    CIK, and serves normal responses otherwise."""

    def __init__(
        self, by_cik: dict[str, SubmissionsResponse], raise_for: str, exc: Exception
    ) -> None:
        self._by_cik = by_cik
        self._raise_for = raise_for
        self._exc = exc

    def fetch_submissions(self, cik: str) -> SubmissionsResponse:
        if cik == self._raise_for:
            raise self._exc
        return self._by_cik[cik]


def make_filing(
    accession: str,
    form: str,
    filing_date: str = "2026-07-22",
    report_date: str = "2026-06-30",
    primary_document: str = "",
    description: str = "",
) -> FilingRecord:
    return FilingRecord(
        accession=accession,
        form=form,
        filing_date=filing_date,
        report_date=report_date,
        primary_document=primary_document,
        description=description,
    )


def make_submissions(
    cik: str, filings: list[FilingRecord]
) -> SubmissionsResponse:
    return SubmissionsResponse(cik=cik, filings=tuple(filings))


# --------------------------------------------------------------------------- #
# Parser tests (canned raw dicts)
# --------------------------------------------------------------------------- #


def test_parse_parallel_arrays() -> None:
    obj: dict[str, object] = {
        "cik": "1777813",
        "filings": {
            "recent": {
                "accessionNumber": ["0001777813-26-000123", "0001777813-26-000122"],
                "form": ["13F-HR", "4"],
                "filingDate": ["2026-07-22", "2026-07-20"],
                "reportDate": ["2026-06-30", ""],
                "primaryDocument": ["form13f.xml", "form4.xml"],
                "primaryDocDescription": ["13F-HR", "FORM 4"],
            }
        },
    }
    sub = _parse_submissions(obj, requested_cik="0001777813")
    assert len(sub.filings) == 2
    first = sub.filings[0]
    assert first.accession == "0001777813-26-000123"
    assert first.form == "13F-HR"
    assert first.filing_date == "2026-07-22"
    assert first.report_date == "2026-06-30"
    assert first.primary_document == "form13f.xml"
    assert first.description == "13F-HR"
    assert sub.filings[1].report_date == ""


def test_parse_skips_malformed_row(caplog: pytest.LogCaptureFixture) -> None:
    obj: dict[str, object] = {
        "cik": "1777813",
        "filings": {
            "recent": {
                "accessionNumber": ["0001777813-26-000123", "", "0001777813-26-000121"],
                "form": ["13F-HR", "4", "SC 13G"],
                "filingDate": ["2026-07-22", "2026-07-21", "2026-07-20"],
            }
        },
    }
    with caplog.at_level(logging.WARNING, logger="monitors.edgar"):
        sub = _parse_submissions(obj, requested_cik="0001777813")
    assert len(sub.filings) == 2
    assert [f.accession for f in sub.filings] == [
        "0001777813-26-000123",
        "0001777813-26-000121",
    ]
    assert any("skipped 1 malformed filing rows" in r.message for r in caplog.records)


def test_parse_coerces_nonstr_nonaccession_fields() -> None:
    obj: dict[str, object] = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001777813-26-000123"],
                "form": [None],
                "filingDate": [12345],
                "reportDate": [None],
                "primaryDocument": [{"x": 1}],
                "primaryDocDescription": [None],
            }
        },
    }
    sub = _parse_submissions(obj, requested_cik="0001777813")
    assert len(sub.filings) == 1
    rec = sub.filings[0]
    assert rec.accession == "0001777813-26-000123"
    assert rec.form == ""
    assert rec.filing_date == ""
    assert rec.report_date == ""
    assert rec.primary_document == ""
    assert rec.description == ""


def test_parse_ragged_short_array_reads_blank() -> None:
    obj: dict[str, object] = {
        "filings": {
            "recent": {
                "accessionNumber": ["a-1", "a-2", "a-3"],
                "form": ["13F-HR"],  # shorter than spine
                "filingDate": ["2026-07-22", "2026-07-21"],
            }
        },
    }
    sub = _parse_submissions(obj, requested_cik="0001777813")
    assert len(sub.filings) == 3
    assert sub.filings[0].form == "13F-HR"
    assert sub.filings[1].form == ""
    assert sub.filings[2].form == ""
    assert sub.filings[2].filing_date == ""


def test_parse_dedupes_accession_within_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    obj: dict[str, object] = {
        "filings": {
            "recent": {
                "accessionNumber": ["dup-1", "dup-1", "other-2"],
                "form": ["13F-HR", "13F-HR/A", "4"],
                "filingDate": ["2026-07-22", "2026-07-22", "2026-07-20"],
            }
        },
    }
    with caplog.at_level(logging.WARNING, logger="monitors.edgar"):
        sub = _parse_submissions(obj, requested_cik="0001777813")
    assert [f.accession for f in sub.filings] == ["dup-1", "other-2"]
    # First occurrence kept (form 13F-HR, not the /A that came second).
    assert sub.filings[0].form == "13F-HR"
    assert any("duplicate accession dup-1" in r.message for r in caplog.records)


def test_parse_missing_filings_raises() -> None:
    with pytest.raises(MonitorError):
        _parse_submissions({"cik": "1"}, requested_cik="0000000001")


def test_parse_missing_recent_raises() -> None:
    with pytest.raises(MonitorError):
        _parse_submissions({"filings": {}}, requested_cik="0000000001")


def test_parse_missing_accessionNumber_raises() -> None:
    obj: dict[str, object] = {
        "filings": {"recent": {"form": ["13F-HR"], "filingDate": ["2026-07-22"]}}
    }
    with pytest.raises(MonitorError):
        _parse_submissions(obj, requested_cik="0000000001")


def test_parse_optional_arrays_default_empty() -> None:
    obj: dict[str, object] = {
        "filings": {
            "recent": {
                "accessionNumber": ["acc-1"],
                "form": ["13F-HR"],
                # filingDate/reportDate/primaryDocument/primaryDocDescription absent
            }
        },
    }
    sub = _parse_submissions(obj, requested_cik="0000000001")
    assert len(sub.filings) == 1
    rec = sub.filings[0]
    assert rec.form == "13F-HR"
    assert rec.filing_date == ""
    assert rec.report_date == ""
    assert rec.description == ""


def test_parse_empty_recent_arrays_zero_filings() -> None:
    obj: dict[str, object] = {
        "filings": {
            "recent": {
                "accessionNumber": [],
                "form": [],
                "filingDate": [],
            }
        },
    }
    sub = _parse_submissions(obj, requested_cik="0000000001")
    assert sub.filings == ()


def test_parse_rejects_wrong_type_container() -> None:
    # recent is a list -> structural.
    with pytest.raises(MonitorError):
        _parse_submissions({"filings": {"recent": []}}, requested_cik="0000000001")
    # accessionNumber is a non-list -> structural.
    obj: dict[str, object] = {
        "filings": {"recent": {"accessionNumber": "not-a-list"}}
    }
    with pytest.raises(MonitorError):
        _parse_submissions(obj, requested_cik="0000000001")


def test_parse_cik_fallback_to_requested() -> None:
    # Missing cik -> requested.
    obj_missing: dict[str, object] = {
        "filings": {"recent": {"accessionNumber": ["a-1"]}}
    }
    assert (
        _parse_submissions(obj_missing, requested_cik="0001777813").cik
        == "0001777813"
    )
    # Non-digit cik -> requested.
    obj_bad: dict[str, object] = {
        "cik": "abc",
        "filings": {"recent": {"accessionNumber": ["a-1"]}},
    }
    assert _parse_submissions(obj_bad, requested_cik="0001777813").cik == "0001777813"
    # Clean-digit echoed cik IS used (zfilled).
    obj_good: dict[str, object] = {
        "cik": "1777813",
        "filings": {"recent": {"accessionNumber": ["a-1"]}},
    }
    assert _parse_submissions(obj_good, requested_cik="0009999999").cik == "0001777813"


# --------------------------------------------------------------------------- #
# check_edgar: classification / filtering / dedupe (pre-seeded)
# --------------------------------------------------------------------------- #


def _preseed(store: StateStore, mapping: dict[str, list[str]]) -> None:
    store.save_seen_filings(mapping)


def test_filter_by_entity_filing_types(
    edgar_config: AppConfig, store: StateStore
) -> None:
    # Pre-seed ALL entities so nothing first-run-seeds.
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, []),
            SA_CIK: make_submissions(
                SA_CIK,
                [
                    make_filing("sa-nport", "NPORT-P"),  # NOT tracked by SA
                    make_filing("sa-13f", "13F-HR"),  # tracked
                ],
            ),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    idents = {e.identifier for e in events}
    assert "sa-13f" in idents
    assert "sa-nport" not in idents


def test_classify_base_and_amendment_forms(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    filings = [
        make_filing("a-13f", "13F-HR"),
        make_filing("a-13fa", "13F-HR/A"),
        make_filing("a-13ga", "SC 13G/A"),
        make_filing("a-4", "4"),
        make_filing("a-nport", "NPORT-P"),
    ]
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, filings),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    by_id = {e.identifier: e for e in events}
    assert by_id["a-13f"].event_type is EventType.FILING_13F
    assert by_id["a-13f"].priority is Priority.HIGH
    assert by_id["a-13fa"].event_type is EventType.FILING_13F
    assert by_id["a-13fa"].priority is Priority.HIGH
    assert by_id["a-13ga"].event_type is EventType.FILING_SC13
    assert by_id["a-13ga"].priority is Priority.HIGH
    assert by_id["a-4"].event_type is EventType.FILING_FORM4
    assert by_id["a-4"].priority is Priority.MEDIUM
    assert by_id["a-nport"].event_type is EventType.FILING_OTHER
    assert by_id["a-nport"].priority is Priority.MEDIUM


def test_classify_fallback_unmapped_form(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK, [make_filing("a-zzz", "ZZZ-TEST")]
            ),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert len(events) == 1
    assert events[0].event_type is EventType.FILING_OTHER
    assert events[0].priority is Priority.LOW


def test_form_normalization(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    filings = [
        make_filing("a-lc", " 13f-hr "),  # lower/whitespace -> 13F-HR
        make_filing("a-sc", "SC 13G"),  # single space preserved
        make_filing("a-blank", "   "),  # blank -> fails filter
    ]
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, filings),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    by_id = {e.identifier: e for e in events}
    assert by_id["a-lc"].event_type is EventType.FILING_13F
    assert by_id["a-lc"].payload["filing_type"] == "13F-HR"
    assert by_id["a-sc"].event_type is EventType.FILING_SC13
    assert by_id["a-sc"].payload["filing_type"] == "SC 13G"
    assert "a-blank" not in by_id


def test_dedupe_seen_accession_no_event(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(
        store,
        {
            "atreides": ["a-old"],
            "situational_awareness": ["prior"],
            "empty_tracker": ["prior"],
        },
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK,
                [make_filing("a-new", "13F-HR"), make_filing("a-old", "13F-HR")],
            ),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    idents = {e.identifier for e in events}
    assert "a-new" in idents
    assert "a-old" not in idents


def test_normal_event_does_not_change_state_for_its_accession(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(
        store,
        {
            "atreides": ["a-old"],
            "situational_awareness": ["prior"],
            "empty_tracker": ["prior"],
        },
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [make_filing("a-new", "13F-HR")]),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert any(e.identifier == "a-new" for e in events)
    # Option B: monitor never marks a normal filing seen.
    assert "a-new" not in store.load_seen_filings()["atreides"]


# --------------------------------------------------------------------------- #
# First-run seeding
# --------------------------------------------------------------------------- #


def test_first_run_seeds_ALL_accessions_zero_events(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    # Pre-seed the OTHER entities so only atreides is first-run.
    _preseed(
        store,
        {"situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    filings = [
        make_filing("seed-13f", "13F-HR"),  # tracked
        make_filing("seed-8k", "8-K"),  # untracked
    ]
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, filings),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.INFO, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, client, NOW)
    assert events == []
    seeded = store.load_seen_filings()["atreides"]
    assert "seed-13f" in seeded and "seed-8k" in seeded  # tracked AND untracked
    seed_logs = [
        r for r in caplog.records if "first-run seed for atreides" in r.message
    ]
    assert seed_logs and seed_logs[0].levelno == logging.INFO


def test_first_run_empty_payload_does_not_seed(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(
        store,
        {"situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, []),  # structurally valid, empty
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert events == []
    assert "atreides" not in store.load_seen_filings()  # stays first-run


def test_multiple_first_run_entities_batched_seed(
    tmp_path: Path, edgar_config: AppConfig
) -> None:
    save_calls: list[dict[str, list[str]]] = []

    class SpyStore(StateStore):
        def save_seen_filings(self, data: dict[str, list[str]]) -> None:
            save_calls.append({k: list(v) for k, v in data.items()})
            super().save_seen_filings(data)

    store = SpyStore(tmp_path / "state")
    # Only atreides + situational_awareness first-run; pre-seed empty_tracker.
    _preseed(store, {"empty_tracker": ["prior"]})
    save_calls.clear()
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [make_filing("a-1", "13F-HR")]),
            SA_CIK: make_submissions(SA_CIK, [make_filing("s-1", "13F-HR")]),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert events == []
    assert len(save_calls) == 1  # a SINGLE batched save
    final = store.load_seen_filings()
    assert final["atreides"] == ["a-1"]
    assert final["situational_awareness"] == ["s-1"]


def test_interleaved_first_run_and_preseeded_entity(
    edgar_config: AppConfig, store: StateStore
) -> None:
    # atreides first-run (absent); situational_awareness + empty pre-seeded.
    _preseed(
        store,
        {"situational_awareness": ["s-old"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [make_filing("a-seed", "13F-HR")]),
            SA_CIK: make_submissions(
                SA_CIK,
                [make_filing("s-new", "13F-HR"), make_filing("s-old", "13F-HR")],
            ),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    idents = {e.identifier for e in events}
    assert idents == {"s-new"}  # first-run atreides emits nothing
    final = store.load_seen_filings()
    assert "a-seed" in final["atreides"]  # seeded
    assert final["situational_awareness"] == ["s-old"]  # unchanged (Option B)


def test_second_run_after_seed_emits_only_new(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(store, {"situational_awareness": ["prior"], "empty_tracker": ["prior"]})
    first_client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [make_filing("a-1", "13F-HR")]),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    assert check_edgar(edgar_config, store, first_client, NOW) == []  # seed run
    second_client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK,
                [make_filing("a-2", "13F-HR"), make_filing("a-1", "13F-HR")],
            ),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, second_client, NOW)
    assert {e.identifier for e in events} == {"a-2"}


def test_bad_date_row_seeded_but_skipped_for_emission(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _preseed(store, {"situational_awareness": ["prior"], "empty_tracker": ["prior"]})
    # Phase (a): first run seeds the bad-date row by accession.
    bad_seed = make_filing("bad-seed", "13F-HR", filing_date="not-a-date")
    first_client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [bad_seed]),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    assert check_edgar(edgar_config, store, first_client, NOW) == []
    assert "bad-seed" in store.load_seen_filings()["atreides"]

    # Phase (b): a DIFFERENT bad-date accession as a new tracked row -> skipped
    # for emission with a WARNING.
    bad_new = make_filing("bad-new", "13F-HR", filing_date="also-bad")
    good_new = make_filing("good-new", "13F-HR", filing_date="2026-07-22")
    second_client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [good_new, bad_new, bad_seed]),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.WARNING, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, second_client, NOW)
    idents = {e.identifier for e in events}
    assert idents == {"good-new"}  # bad-new skipped, bad-seed already seen
    assert any("bad-new" in r.message and "unparseable" in r.message for r in caplog.records)


def test_present_empty_list_is_not_first_run(
    edgar_config: AppConfig, store: StateStore
) -> None:
    _preseed(
        store,
        {"atreides": [], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [make_filing("a-1", "13F-HR")]),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert {e.identifier for e in events} == {"a-1"}  # emits (not first run)


def test_empty_filing_types_entity(
    edgar_config: AppConfig, store: StateStore
) -> None:
    # empty_tracker.filing_types == ["13F-HR"] in the fixture; override via a
    # dedicated single-entity config to truly test empty filing_types.
    # Build an inline config with an empty-filing_types entity is out of scope;
    # instead, assert filtering + seeding independence using empty_tracker,
    # which tracks only 13F-HR. First-run: seed ALL by accession regardless.
    _preseed(store, {"atreides": ["prior"], "situational_awareness": ["prior"]})
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, []),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(
                EMPTY_CIK, [make_filing("e-8k", "8-K"), make_filing("e-13f", "13F-HR")]
            ),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert events == []  # first-run: zero events
    seeded = store.load_seen_filings()["empty_tracker"]
    assert "e-8k" in seeded and "e-13f" in seeded  # seed is filter-independent


def test_truly_empty_filing_types_entity(tmp_path: Path) -> None:
    """A config entity with an EMPTY filing_types list: nothing passes the
    filter even on a non-first run; first-run still seeds by accession."""
    cfg_text = (EDGAR_CONFIG).read_text(encoding="utf-8").replace(
        'filing_types: ["13F-HR"]\n', "filing_types: []\n"
    )
    cfg_path = tmp_path / "empty_ft.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")
    config = load_config(cfg_path)
    store = StateStore(tmp_path / "state")
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["e-old"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, []),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, [make_filing("e-new", "13F-HR")]),
        }
    )
    events = check_edgar(config, store, client, NOW)
    assert events == []  # empty filing_types -> nothing passes the filter


# --------------------------------------------------------------------------- #
# DetectedEvent field tests
# --------------------------------------------------------------------------- #


def _single_atreides_client(filing: FilingRecord) -> FakeEdgarClient:
    return FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [filing]),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )


def test_payload_keys_populated(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK,
                [
                    make_filing("p-13f", "13F-HR", report_date="2026-06-30"),
                    make_filing("p-4", "4", report_date=""),
                ],
            ),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    by_id = {e.identifier: e for e in events}
    p13f = by_id["p-13f"]
    assert set(p13f.payload.keys()) == {"filing_type", "period", "note"}
    assert p13f.payload["filing_type"] == "13F-HR"
    assert p13f.payload["period"] == "2026-06-30"
    assert p13f.payload["note"] == "Quarterly holdings report; position data at the link."
    p4 = by_id["p-4"]
    assert p4.payload["period"] == ""
    assert p4.payload["note"] == "Insider transaction report; details at the link."


def test_url_and_identifier(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = _single_atreides_client(make_filing("0001777813-26-000123", "13F-HR"))
    events = check_edgar(edgar_config, store, client, NOW)
    ev = events[0]
    assert ev.identifier == "0001777813-26-000123"
    assert ev.url == (
        "https://www.sec.gov/Archives/edgar/data/1777813/"
        "000177781326000123/0001777813-26-000123-index.htm"
    )


def test_url_malformed_cik_no_crash(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    # Response echoes an all-zeros / odd cik -> parser falls back to requested,
    # but force the edge directly by constructing a SubmissionsResponse with an
    # all-zeros cik.
    sub = SubmissionsResponse(
        cik="0000000000", filings=(make_filing("acc-x", "13F-HR"),)
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: sub,
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    events = check_edgar(edgar_config, store, client, NOW)
    assert events[0].url.startswith("https://www.sec.gov/Archives/edgar/data/0/")


def test_published_is_tz_aware_utc(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = _single_atreides_client(
        make_filing("pub-1", "13F-HR", filing_date="2026-07-22")
    )
    ev = check_edgar(edgar_config, store, client, NOW)[0]
    assert ev.published is not None
    assert ev.published.tzinfo == timezone.utc
    assert ev.published == datetime(2026, 7, 22, tzinfo=timezone.utc)


def test_source_is_entity_name(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = _single_atreides_client(make_filing("src-1", "13F-HR"))
    ev = check_edgar(edgar_config, store, client, NOW)[0]
    assert ev.source == "Atreides Management LLC"
    assert ev.confidence is Confidence.HIGH


def test_build_alert_integration(edgar_config: AppConfig, store: StateStore) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = _single_atreides_client(
        make_filing("0001777813-26-000123", "13F-HR", report_date="2026-06-30")
    )
    ev = check_edgar(edgar_config, store, client, NOW)[0]
    alert = build_alert(ev, edgar_config)
    assert alert.subject == "[SEC FILING] Atreides Management LLC — 13F-HR filed"
    assert "Entity: Atreides Management LLC" in alert.body
    assert "Filing type: 13F-HR" in alert.body
    assert "0001777813-26-000123-index.htm" in alert.body


def test_date_drift_circuit_breaker(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    # All new tracked candidates have bad dates -> ERROR log, zero events.
    bad_client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK,
                [
                    make_filing("d-1", "13F-HR", filing_date="nope"),
                    make_filing("d-2", "4", filing_date="also-nope"),
                ],
            ),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.ERROR, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, bad_client, NOW)
    assert events == []
    assert any("date-format change" in r.message for r in caplog.records)

    # Control: at least one good date -> NO error.
    caplog.clear()
    good_client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK,
                [
                    make_filing("d-3", "13F-HR", filing_date="2026-07-22"),
                    make_filing("d-4", "4", filing_date="bad"),
                ],
            ),
            SA_CIK: make_submissions(SA_CIK, []),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.ERROR, logger="monitors.edgar"):
        check_edgar(edgar_config, store, good_client, NOW)
    assert not any(r.levelno == logging.ERROR for r in caplog.records)


# --------------------------------------------------------------------------- #
# Per-entity isolation
# --------------------------------------------------------------------------- #


def test_fetch_error_isolated_per_entity(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    # atreides CIK missing -> fake raises MonitorError; SA still healthy.
    client = FakeEdgarClient(
        {
            SA_CIK: make_submissions(SA_CIK, [make_filing("s-new", "13F-HR")]),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.ERROR, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, client, NOW)
    assert {e.identifier for e in events} == {"s-new"}
    assert any("failed to process entity atreides" in r.message for r in caplog.records)


def test_non_monitorerror_isolated_per_entity(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = RaisingFakeEdgarClient(
        {
            SA_CIK: make_submissions(SA_CIK, [make_filing("s-new", "13F-HR")]),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        },
        raise_for=ATREIDES_CIK,
        exc=TypeError("simulated JSON drift"),
    )
    with caplog.at_level(logging.ERROR, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, client, NOW)
    assert {e.identifier for e in events} == {"s-new"}
    assert any("failed to process entity atreides" in r.message for r in caplog.records)


def test_multi_entity_events_and_order(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(
                ATREIDES_CIK,
                [make_filing("a-1", "13F-HR"), make_filing("a-2", "4")],  # newest-first
            ),
            SA_CIK: make_submissions(SA_CIK, [make_filing("s-1", "13F-HR")]),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.WARNING, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, client, NOW)
    # Config order: atreides before situational_awareness; newest-first within.
    assert [e.identifier for e in events] == ["a-1", "a-2", "s-1"]
    assert events[0].entity_key == "atreides"
    assert events[2].entity_key == "situational_awareness"
    assert events[2].source == "Situational Awareness LP"


def test_success_no_error_logs(
    edgar_config: AppConfig, store: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    _preseed(
        store,
        {"atreides": ["prior"], "situational_awareness": ["prior"], "empty_tracker": ["prior"]},
    )
    client = _single_atreides_client(make_filing("ok-1", "13F-HR"))
    with caplog.at_level(logging.WARNING, logger="monitors.edgar"):
        check_edgar(edgar_config, store, client, NOW)
    # No WARNING/ERROR/isolation logs on a clean happy path.
    assert not any(
        r.levelno >= logging.WARNING for r in caplog.records
    )


# --------------------------------------------------------------------------- #
# State-write path (spy store)
# --------------------------------------------------------------------------- #


def test_seed_write_reloads_and_merges(
    tmp_path: Path, edgar_config: AppConfig
) -> None:
    class SpyStore(StateStore):
        def __init__(self, state_dir: Path) -> None:
            super().__init__(state_dir)
            self.load_count = 0
            self.saved: dict[str, list[str]] | None = None

        def load_seen_filings(self) -> dict[str, list[str]]:
            self.load_count += 1
            if self.load_count == 1:
                # Initial load: atreides + empty pre-seeded; SA absent (first-run).
                return {"atreides": ["prior"], "empty_tracker": ["prior"]}
            # 2nd (fresh) load: an interleaved concurrent write added a key.
            return {
                "atreides": ["prior"],
                "empty_tracker": ["prior"],
                "situational_awareness": ["interleaved"],
                "unrelated": ["keepme"],
            }

        def save_seen_filings(self, data: dict[str, list[str]]) -> None:
            self.saved = {k: list(v) for k, v in data.items()}

    store = SpyStore(tmp_path / "state")
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, []),
            SA_CIK: make_submissions(SA_CIK, [make_filing("s-seed", "13F-HR")]),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    check_edgar(edgar_config, store, client, NOW)
    assert store.saved is not None
    # Merged into the 2nd (fresh) load, order-preserving set-like dedup.
    assert store.saved["situational_awareness"] == list(
        dict.fromkeys(["interleaved", "s-seed"])
    )
    assert store.saved["unrelated"] == ["keepme"]  # interleaved key preserved


def test_initial_load_failure_propagates(
    tmp_path: Path, edgar_config: AppConfig
) -> None:
    class FailingLoadStore(StateStore):
        def load_seen_filings(self) -> dict[str, list[str]]:
            raise RuntimeError("disk gone")

    store = FailingLoadStore(tmp_path / "state")
    client = FakeEdgarClient({})
    with pytest.raises(RuntimeError):
        check_edgar(edgar_config, store, client, NOW)


def test_final_save_failure_nonfatal_returns_events(
    tmp_path: Path, edgar_config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingSaveStore(StateStore):
        def __init__(self, state_dir: Path) -> None:
            super().__init__(state_dir)
            self._data: dict[str, list[str]] = {
                "situational_awareness": ["s-old"],
                "empty_tracker": ["prior"],
            }

        def load_seen_filings(self) -> dict[str, list[str]]:
            return {k: list(v) for k, v in self._data.items()}

        def save_seen_filings(self, data: dict[str, list[str]]) -> None:
            raise RuntimeError("save failed")

    store = FailingSaveStore(tmp_path / "state")
    # atreides first-run (triggers a seed save that fails); SA pre-seeded emits.
    client = FakeEdgarClient(
        {
            ATREIDES_CIK: make_submissions(ATREIDES_CIK, [make_filing("a-seed", "13F-HR")]),
            SA_CIK: make_submissions(
                SA_CIK, [make_filing("s-new", "13F-HR"), make_filing("s-old", "13F-HR")]
            ),
            EMPTY_CIK: make_submissions(EMPTY_CIK, []),
        }
    )
    with caplog.at_level(logging.ERROR, logger="monitors.edgar"):
        events = check_edgar(edgar_config, store, client, NOW)
    assert {e.identifier for e in events} == {"s-new"}  # still returns events
    assert any("failed to persist first-run seeds" in r.message for r in caplog.records)


def test_now_must_be_tz_aware(tmp_path: Path, edgar_config: AppConfig) -> None:
    class SpyStore(StateStore):
        def __init__(self, state_dir: Path) -> None:
            super().__init__(state_dir)
            self.load_called = False

        def load_seen_filings(self) -> dict[str, list[str]]:
            self.load_called = True
            return {}

    store = SpyStore(tmp_path / "state")
    client = FakeEdgarClient({})
    naive = datetime(2026, 7, 22)  # naive
    with pytest.raises(ValueError):
        check_edgar(edgar_config, store, client, naive)
    assert store.load_called is False  # validated BEFORE any state load


# --------------------------------------------------------------------------- #
# Concrete EdgarHttpClient (injected fake session + no-op sleep; no network)
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status_code: int, json_data: object = None, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._bad_json = bad_json

    def json(self) -> object:
        if self._bad_json:
            raise ValueError("no json")
        return self._json_data


class FakeSession:
    """Serves a scripted sequence of responses / exceptions to raise."""

    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.get_calls = 0

    def get(self, url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        self.get_calls += 1
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, FakeResponse)
        return item


def _good_body(cik: str) -> dict[str, object]:
    return {
        "cik": cik.lstrip("0"),
        "filings": {
            "recent": {
                "accessionNumber": ["0001777813-26-000123"],
                "form": ["13F-HR"],
                "filingDate": ["2026-07-22"],
                "reportDate": ["2026-06-30"],
                "primaryDocument": ["f.xml"],
                "primaryDocDescription": ["13F-HR"],
            }
        },
    }


def _make_client(session: FakeSession, sleep: Callable[[float], None]) -> EdgarHttpClient:
    # FakeSession is not a requests.Session subclass; the client only calls
    # .get(), so it is duck-typed here and the type is silenced at this one seam.
    return EdgarHttpClient(session=session, sleep=sleep)  # type: ignore[arg-type]  # test fake session (duck-typed .get)


def test_client_wraps_transport_error() -> None:
    import requests

    calls: list[float] = []
    session = FakeSession([requests.ConnectionError("boom")] * (1 + HTTP_MAX_RETRIES))
    client = _make_client(session, calls.append)
    with pytest.raises(MonitorError):
        client.fetch_submissions(ATREIDES_CIK)


def test_client_wraps_bad_json() -> None:
    calls: list[float] = []
    session = FakeSession([FakeResponse(200, bad_json=True)])
    client = _make_client(session, calls.append)
    with pytest.raises(MonitorError):
        client.fetch_submissions(ATREIDES_CIK)


def test_client_happy_path_parses() -> None:
    calls: list[float] = []
    session = FakeSession([FakeResponse(200, json_data=_good_body(ATREIDES_CIK))])
    client = _make_client(session, calls.append)
    sub = client.fetch_submissions(ATREIDES_CIK)
    assert sub.cik == ATREIDES_CIK
    assert len(sub.filings) == 1
    assert sub.filings[0].accession == "0001777813-26-000123"
    assert sub.filings[0].form == "13F-HR"


def test_client_retries_then_succeeds() -> None:
    calls: list[float] = []
    session = FakeSession(
        [FakeResponse(503), FakeResponse(200, json_data=_good_body(ATREIDES_CIK))]
    )
    client = _make_client(session, calls.append)
    sub = client.fetch_submissions(ATREIDES_CIK)
    assert len(sub.filings) == 1
    assert session.get_calls == 2
    # Backoff before retry #1 == base * 1.
    assert EDGAR_RETRY_BACKOFF_SECONDS in calls


def test_client_retry_exhaustion() -> None:
    calls: list[float] = []
    session = FakeSession([FakeResponse(503)] * (1 + HTTP_MAX_RETRIES))
    client = _make_client(session, calls.append)
    with pytest.raises(MonitorError):
        client.fetch_submissions(ATREIDES_CIK)
    assert session.get_calls == 1 + HTTP_MAX_RETRIES


def test_client_404_not_retried() -> None:
    calls: list[float] = []
    session = FakeSession([FakeResponse(404)])
    client = _make_client(session, calls.append)
    with pytest.raises(MonitorError):
        client.fetch_submissions(ATREIDES_CIK)
    assert session.get_calls == 1
    # No retry backoff sleep (a rate-limit sleep is skipped on the first call).
    assert calls == []


def test_client_403_not_retried() -> None:
    calls: list[float] = []
    session = FakeSession([FakeResponse(403)])
    client = _make_client(session, calls.append)
    with pytest.raises(MonitorError):
        client.fetch_submissions(ATREIDES_CIK)
    assert session.get_calls == 1


def test_rate_limit_spacing_uses_injected_sleep() -> None:
    calls: list[float] = []
    session = FakeSession(
        [
            FakeResponse(200, json_data=_good_body(ATREIDES_CIK)),
            FakeResponse(200, json_data=_good_body(ATREIDES_CIK)),
        ]
    )
    client = _make_client(session, calls.append)
    client.fetch_submissions(ATREIDES_CIK)
    client.fetch_submissions(ATREIDES_CIK)
    # The second call must space via the injected sleep (>0, <= min interval).
    assert calls, "expected a rate-limit sleep on the back-to-back call"
    assert all(0 < c <= EDGAR_MIN_REQUEST_INTERVAL_SECONDS + 1e-6 for c in calls)
