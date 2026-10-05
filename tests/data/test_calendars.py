"""calendar_economic, calendar_earnings, calendar_earnings_history and
calendar_us_holidays against synthetic Nasdaq, NYSE, SIFMA and OPM payloads.
Nasdaq's economic calendar answers request date D with the events of D - 1;
the tools speak event dates."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from data_harness import NOW, call, check_golden, context, fixture_json, fixture_text, freeze_clocks, refused

NASDAQ = r"api\.nasdaq\.com/api"


def economic_routes(upstream, overrides=None):
    overrides = {} if overrides is None else overrides  # the caller may fill it later

    def respond(request):
        event_day = date.fromisoformat(dict(request.url.params)["date"]) - timedelta(days=1)
        key = event_day.isoformat()
        if key in overrides:
            return overrides[key](request)
        try:
            return fixture_json(f"nasdaq.economic__{key}.json")
        except FileNotFoundError:
            return fixture_json("nasdaq.economic__empty.json")

    return upstream.add(rf"{NASDAQ}/calendar/economicevents", respond)


def earnings_routes(upstream):
    upstream.add(
        rf"{NASDAQ}/calendar/earnings",
        lambda r: fixture_json(f"nasdaq.earnings__{dict(r.url.params)['date']}.json"),
    )


def surprise_routes(upstream):
    def respond(request):
        symbol = request.url.path.split("/")[3]
        name = "TESTCO" if symbol == "TESTCO" else "unknown"
        return fixture_json(f"nasdaq.earnings_surprise__{name}.json")

    upstream.add(rf"{NASDAQ}/company/[^/]+/earnings-surprise", respond)


# --- the economic calendar ----------------------------------------------------------------------


def test_calendar_economic_golden(tmp_path, upstream, frozen_clock):
    economic_routes(upstream)
    out = call("calendar_economic", context(tmp_path), start="2026-10-01", end="2026-10-02")
    doc = check_golden(out, "calendar_economic")
    assert sorted(upstream.params(i)["date"] for i in range(len(upstream.requests))) == [
        "2026-10-02",
        "2026-10-03",
    ]
    assert [(r["event_date"], r["event_name"], r["actual"], r["consensus"]) for r in doc["rows"]] == [
        ("2026-10-01", "Initial Jobless Claims", 225000.0, 230000.0),
        ("2026-10-01", "S&P Global Manufacturing PMI", 51.2, 51.0),
        ("2026-10-01", "ISM Manufacturing PMI", 49.1, 49.5),
        ("2026-10-02", "Nonfarm Payrolls", None, 150000.0),
        ("2026-10-02", "Unemployment Rate", None, 4.3),
        ("2026-10-02", "Synthetic Holiday Note", None, None),
    ]  # US rows only, as published (4.3 is 4.3 %: value_kind pct)
    assert doc["rows"][4]["value_kind"] == "pct"
    assert doc["rows"][0]["release_at"] == "2026-10-01T12:30:00Z"
    assert doc["rows"][5]["all_day"] is True and doc["rows"][5]["absent"]["release_at"] == "not_applicable"
    assert doc["provenance"]["authority"] == ["third-party"]
    pmi = call("calendar_economic", context(tmp_path), start="2026-10-01", end="2026-10-02", q="pmi")
    assert [r.event_name for r in pmi.rows] == ["S&P Global Manufacturing PMI", "ISM Manufacturing PMI"]


def test_calendar_economic_counts_removed_events(tmp_path, upstream, monkeypatch):
    later = {}
    economic_routes(upstream, later)
    freeze_clocks(monkeypatch, NOW)
    first = call("calendar_economic", context(tmp_path), start="2026-10-01", end="2026-10-01")
    assert len(first.rows) == 3 and first.notes == []
    later["2026-10-01"] = lambda _r: fixture_json("nasdaq.economic__2026-10-01__later.json")
    freeze_clocks(monkeypatch, NOW + timedelta(hours=1))
    second = call(
        "calendar_economic",
        context(tmp_path, now=NOW + timedelta(hours=1)),
        start="2026-10-01",
        end="2026-10-01",
    )
    assert [r.event_name for r in second.rows] == ["Initial Jobless Claims", "ISM Manufacturing PMI"]
    assert second.notes == ["1 event listed earlier was removed from Nasdaq's calendar since (not shown)."]
    # point in time: the first capture's view
    before = call(
        "calendar_economic",
        context(tmp_path, settings={"mode": "local"}),
        start="2026-10-01",
        end="2026-10-01",
        as_of="2026-10-02T20:30:00Z",
    )
    assert len(before.rows) == 3


def test_calendar_economic_failed_dates_become_notes(tmp_path, upstream, frozen_clock):
    economic_routes(upstream, {"2026-10-02": lambda _r: httpx.Response(503, text="busy")})
    out = call("calendar_economic", context(tmp_path), start="2026-10-01", end="2026-10-02")
    assert {r.event_date.isoformat() for r in out.rows} == {"2026-10-01"}
    assert len(out.notes) == 1 and out.notes[0].startswith(
        "The capture of 2026-10-02 failed: Nasdaq economic calendar request failed: "
    )
    upstream.add(rf"{NASDAQ}/calendar/economicevents", lambda _r: httpx.Response(503, text="busy"))
    err = refused("calendar_economic", context(tmp_path), start="2026-10-05", end="2026-10-06")
    assert err.code == "data_unavailable" and err.retryable
    assert err.message.startswith("Nasdaq did not answer (")


def test_calendar_windows_are_capped(tmp_path):
    for tool in ("calendar_economic", "calendar_earnings"):
        err = refused(tool, context(tmp_path), start="2026-10-01", end="2026-11-15")
        assert (err.code, err.message) == (
            "data_window",
            f"{tool} accepts at most 31 days between start and end (got 45).",
        )


# --- earnings -----------------------------------------------------------------------------------


def test_calendar_earnings_golden(tmp_path, upstream, frozen_clock):
    earnings_routes(upstream)
    out = call("calendar_earnings", context(tmp_path), start="2026-10-01", end="2026-10-02")
    doc = check_golden(out, "calendar_earnings")
    assert [(r["report_date"], r["ticker"], r["eps_actual"], r["surprise_pct"]) for r in doc["rows"]] == [
        ("2026-10-01", "LOSSCO", -0.12, -0.2),
        ("2026-10-01", "OTHRCO", 0.47, 0.0444),
        ("2026-10-02", "BRK-B", None, None),
        ("2026-10-02", "TESTCO", None, None),
    ]
    testco = doc["rows"][3]
    assert (testco["market_cap"], testco["eps_forecast"], testco["last_year_eps"]) == (
        19296256000.0,
        0.45,
        0.38,
    )
    assert testco["absent"] == {"eps_actual": "not_applicable", "surprise_pct": "not_applicable"}
    assert doc["rows"][0]["absent"] == {
        "market_cap": "no_data",
        "last_year_report_date": "not_applicable",
        "last_year_eps": "not_applicable",
    }
    only = call(
        "calendar_earnings", context(tmp_path), start="2026-10-01", end="2026-10-02", tickers=["brk.b"]
    )
    assert [r.ticker for r in only.rows] == ["BRK-B"]


def test_calendar_earnings_history_golden(tmp_path, upstream, frozen_clock):
    surprise_routes(upstream)
    out = call("calendar_earnings_history", context(tmp_path), tickers=["TESTCO", "ZZZZ"])
    doc = check_golden(out, "calendar_earnings_history")
    assert [(r["fiscal_quarter"], r["eps_actual"], r["surprise_pct"]) for r in doc["rows"]] == [
        ("2026-06", 0.42, 0.05),
        ("2026-03", 0.38, -0.0256),
    ]
    assert doc["notes"] == ["Nasdaq has no earnings history for ZZZZ"]
    err = refused("calendar_earnings_history", context(tmp_path), tickers=["ZZZZ"])
    assert (err.code, err.message) == ("data_not_found", "Nasdaq has no earnings history for ZZZZ.")


# --- holidays -----------------------------------------------------------------------------------


def holiday_routes(upstream):
    upstream.text_file(r"www\.nyse\.com/markets/hours-calendars", "nyse.hours_calendars.html")
    upstream.text_file(
        r"www\.sifma\.org/resources/guides-playbooks/holiday-schedule", "sifma.holiday_schedule.html"
    )
    upstream.text_file(
        r"www\.sifma\.org/resources/guides-playbooks/us-holiday-archive", "sifma.us_holiday_archive.html"
    )
    upstream.text_file(
        r"www\.opm\.gov/policy-data-oversight/pay-leave/federal-holidays/", "opm.federal_holidays.html"
    )


def test_calendar_us_holidays_golden(tmp_path, upstream, frozen_clock):
    holiday_routes(upstream)
    out = call("calendar_us_holidays", context(tmp_path))
    doc = check_golden(out, "calendar_us_holidays")
    assert len(doc["rows"]) == 12 + 17 + 11
    thanksgiving = [r for r in doc["rows"] if r["holiday_date"] == "2026-11-27"]
    assert [(r["market"], r["status"], r["close_time_et"], r["close_at"]) for r in thanksgiving] == [
        ("stocks", "early_close", "13:00", "2026-11-27T18:00:00Z"),
        ("bonds", "early_close", "14:00", "2026-11-27T19:00:00Z"),
    ]
    assert doc["provenance"]["authority"] == ["exchange", "third-party", "official"]
    assert {r["crosscheck"] for r in doc["rows"] if r["publisher"] == "nyse"} == {"not_checked"}
    federal = call("calendar_us_holidays", context(tmp_path), markets=["federal"], start="2026-11-01")
    assert [r.name for r in federal.rows] == ["Veterans Day", "Thanksgiving Day", "Christmas Day"]


def test_calendar_us_holidays_reports_a_failed_publisher(tmp_path, upstream, frozen_clock):
    holiday_routes(upstream)
    upstream.add(r"www\.sifma\.org/.*", httpx.Response(404, text="gone"))
    out = call("calendar_us_holidays", context(tmp_path), markets=["stocks", "bonds"])
    assert {r.market for r in out.rows} == {"stocks"}
    assert out.notes == [
        "SIFMA could not be read (SIFMA refused the request: https://www.sifma.org/resources/guides-playbooks/"
        "holiday-schedule); its stored rows, if any, are shown."
    ]


@pytest.mark.parametrize("tool", ["calendar_economic", "calendar_earnings", "calendar_us_holidays"])
def test_local_mode_captures_nothing(tmp_path, upstream, tool):
    out = call(tool, context(tmp_path, settings={"mode": "local"}), start="2026-10-01", end="2026-10-02")
    assert out.rows == [] and upstream.requests == []
    assert out.notes[-1].startswith("mode is local and nothing is stored")


def test_fixtures_are_marked_synthetic():
    assert "synthetic" in fixture_text("nyse.hours_calendars.html")
