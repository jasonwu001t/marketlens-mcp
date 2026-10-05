"""fed_fomc_meetings, fed_fomc_statements, fed_reference_rates,
treasury_yield_curve, treasury_auctions, treasury_debt and treasury_tga
against synthetic Federal Reserve, NY Fed, Treasury and FiscalData payloads.
Every source here is keyless."""

from __future__ import annotations

import pytest
from data_harness import call, check_golden, context, fixture_text, refused

FED = r"www\.federalreserve\.gov"
FISCAL = r"api\.fiscaldata\.treasury\.gov/services/api/fiscal_service"


def fomc_routes(upstream):
    upstream.text_file(rf"{FED}/monetarypolicy/fomccalendars\.htm", "fomc.calendar.html")
    for d in ("20260729", "20260916"):
        upstream.text_file(rf"{FED}/newsevents/pressreleases/monetary{d}a\.htm", f"fomc.statement__{d}.html")


def test_fed_fomc_meetings_golden(tmp_path, upstream, frozen_clock):
    fomc_routes(upstream)
    out = call("fed_fomc_meetings", context(tmp_path))
    doc = check_golden(out, "fed_fomc_meetings")
    assert [(r["start_date"], r["end_date"], r["has_projection"]) for r in doc["rows"]] == [
        ("2026-01-27", "2026-01-28", False),
        ("2026-03-17", "2026-03-18", True),
        ("2026-07-28", "2026-07-29", False),
        ("2026-09-15", "2026-09-16", True),
        ("2026-12-08", "2026-12-09", True),
        ("2027-01-26", "2027-01-27", False),
    ]  # the notation vote is not a meeting
    # forward known: an as_of before the capture sees nothing
    assert call("fed_fomc_meetings", context(tmp_path), as_of="2026-01-01").rows == []


def test_fed_fomc_statements_golden(tmp_path, upstream):
    fomc_routes(upstream)
    out = call("fed_fomc_statements", context(tmp_path), start="2026-01-01")
    doc = check_golden(out, "fed_fomc_statements")
    assert [(r["meeting_date"], r["knowledge_time"]) for r in doc["rows"]] == [
        ("2026-09-16", "2026-09-16T19:00:00Z"),
        ("2026-07-29", "2026-07-29T19:00:00Z"),
    ]
    assert "lower the target range" in doc["rows"][0]["text"]
    assert "For media inquiries" not in doc["rows"][0]["text"]
    bare = call("fed_fomc_statements", context(tmp_path), include_text=False)
    assert all(r.text is None and r.absent == {"text": "withheld"} for r in bare.rows)
    assert len(bare.rows) == 2
    # a statement is not knowable before 2 p.m. Washington time on the meeting's last day
    early = call("fed_fomc_statements", context(tmp_path), as_of="2026-09-16T18:00:00Z")
    assert [r.meeting_date.isoformat() for r in early.rows] == ["2026-07-29"]


def test_fed_reference_rates_golden(tmp_path, upstream):
    route = upstream.json_file(
        r"markets\.newyorkfed\.org/api/rates/all/search\.json", "nyfed.reference_rates.json"
    )
    out = call("fed_reference_rates", context(tmp_path))
    doc = check_golden(out, "fed_reference_rates")
    assert upstream.params(0) == {"startDate": "2016-03-01"}  # marketlens-data's first-fetch floor
    effr = next(r for r in doc["rows"] if r["rate_type"] == "EFFR")
    assert (effr["rate"], effr["volume_usd"], effr["target_from"], effr["target_to"]) == (
        0.0433,
        89000000000.0,
        0.0425,
        0.045,
    )
    sofr_old = next(r for r in doc["rows"] if r["date"] == "2026-09-30")
    assert sofr_old["volume_usd"] is None and sofr_old["absent"]["volume_usd"] == "no_data"
    assert {r["rate_type"] for r in doc["rows"]} == {"SOFR", "EFFR", "OBFR", "TGCR", "BGCR"}
    only = call("fed_reference_rates", context(tmp_path), rate_types=["SOFR"])
    assert {r.rate_type for r in only.rows} == {"SOFR"} and route.hits == 1  # second call read the store
    # published the next business day: the 2026-10-01 rates were not knowable on 2026-10-02 at noon
    noon = call("fed_reference_rates", context(tmp_path), as_of="2026-10-02T12:00:00Z")
    assert {r.date.isoformat() for r in noon.rows} == {"2026-09-30"}


def treasury_routes(upstream):
    def respond(request):
        year = request.url.path.rsplit("/", 2)[-2]
        return fixture_text(f"treasury.yield_curve__{year}.csv")

    upstream.add(
        r"home\.treasury\.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates\.csv/\d{4}/all",
        respond,
    )


def test_treasury_yield_curve_golden(tmp_path, upstream):
    treasury_routes(upstream)
    out = call("treasury_yield_curve", context(tmp_path), tenors=["1.5M", "3M", "10Y"])
    doc = check_golden(out, "treasury_yield_curve")
    assert [(r["date"], r["tenor"], r["rate"], r["tenor_label"]) for r in doc["rows"]] == [
        ("2026-09-30", "1.5M", 0.041, None),
        ("2026-09-30", "3M", 0.0406, "3 Mo"),
        ("2026-09-30", "10Y", 0.0397, "10 Yr"),
        ("2026-10-01", "1.5M", 0.0409, None),
        ("2026-10-01", "3M", 0.0405, "3 Mo"),
        ("2026-10-01", "10Y", 0.0395, "10 Yr"),
    ]
    assert doc["rows"][0]["absent"] == {"tenor_label": "not_applicable"}


def test_treasury_yield_curve_spans_years_and_caps_the_window(tmp_path, upstream):
    treasury_routes(upstream)
    out = call(
        "treasury_yield_curve", context(tmp_path), start="2025-12-15", end="2026-01-10", tenors=["1.5M"]
    )
    assert [(r.date.isoformat(), r.rate) for r in out.rows] == [("2026-01-05", None)]
    assert out.rows[0].absent == {"rate": "no_data", "tenor_label": "not_applicable"}
    years = sorted(r.url.path.rsplit("/", 2)[-2] for r in upstream.requests)
    assert years == ["2025", "2026"]
    err = refused("treasury_yield_curve", context(tmp_path), start="2010-01-01", end="2026-01-01")
    assert (err.code, err.message) == (
        "data_window",
        "treasury_yield_curve accepts at most 3660 days between start and end (got 5844).",
    )


def test_treasury_auctions_golden(tmp_path, upstream):
    upstream.json_file(rf"{FISCAL}/v1/accounting/od/auctions_query", "fiscaldata.auctions.json")
    out = call("treasury_auctions", context(tmp_path))
    doc = check_golden(out, "treasury_auctions")
    assert [r["cusip"] for r in doc["rows"]] == ["912797SX6", "912797SY4", "91282CNB3", "91282CNA5"]
    bill = doc["rows"][0]
    assert (bill["offering_amount"], bill["high_discount_rate"], bill["bid_to_cover"]) == (
        75000000000.0,
        0.0405,
        2.85,
    )
    assert bill["absent"] == {"high_yield": "no_data", "interest_rate": "no_data"}
    tips = call("treasury_auctions", context(tmp_path), security_types=["TIPS"])
    assert [r.cusip for r in tips.rows] == ["91282CNB3"]
    notes = call("treasury_auctions", context(tmp_path), security_types=["Note"], start="2026-09-01")
    assert [r.cusip for r in notes.rows] == ["91282CNB3", "91282CNA5"]


def test_treasury_debt_golden(tmp_path, upstream):
    upstream.json_file(rf"{FISCAL}/v2/accounting/od/debt_to_penny", "fiscaldata.debt_to_penny.json")
    out = call("treasury_debt", context(tmp_path))
    doc = check_golden(out, "treasury_debt")
    assert [(r["date"], r["total_debt"]) for r in doc["rows"]] == [
        ("2026-09-29", 37480000000000.0),
        ("2026-09-30", 37500000000000.0),
    ]


def test_treasury_tga_golden(tmp_path, upstream):
    upstream.json_file(rf"{FISCAL}/v1/accounting/dts/operating_cash_balance", "fiscaldata.tga_balance.json")
    out = call("treasury_tga", context(tmp_path))
    doc = check_golden(out, "treasury_tga")
    assert [(r["date"], r["closing_balance"]) for r in doc["rows"]] == [
        ("2026-09-29", 830500000000.0),
        ("2026-09-30", 850123456000.0),
    ]  # $ millions as published, multiplied out
    older = call("treasury_tga", context(tmp_path), start="2022-01-01", end="2022-12-31")
    assert [(r.account_type, r.closing_balance) for r in older.rows] == [
        ("Federal Reserve Account", 350000000000.0)
    ]


@pytest.mark.parametrize(
    "tool", ["treasury_debt", "treasury_tga", "treasury_auctions", "fed_reference_rates"]
)
def test_start_after_end_is_refused(tmp_path, tool):
    err = refused(tool, context(tmp_path), start="2026-02-01", end="2026-01-01")
    assert err.code == "invalid_arguments"
