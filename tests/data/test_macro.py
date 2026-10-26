"""macro_series, macro_series_catalog, macro_bls_series, macro_bea_table,
macro_release_schedule, macro_census_trade, macro_boj_series, macro_boe_series,
macro_regional_fed_surveys and credit_trace_aggregates against synthetic FRED,
BLS, BEA, Census, Bank of Japan, Bank of England, Richmond Fed and FINRA
payloads."""

from __future__ import annotations

import json

import pytest
from data_harness import KEYS, call, check_golden, context, refused

FRED = r"api\.stlouisfed\.org/fred/series/observations"
BLS = r"api\.bls\.gov/publicAPI/v2/timeseries/data/"
BEA = r"apps\.bea\.gov/api/data/"


def fred_routes(upstream):
    def respond(request):
        sid = dict(request.url.params)["series_id"]
        from data_harness import fixture_json

        return fixture_json(f"fred.series__{sid}.json")

    upstream.add(FRED, respond)


def bls_routes(upstream):
    def respond(request):
        from data_harness import fixture_json

        body = json.loads(request.content)
        return fixture_json(f"bls.timeseries__{body['startyear']}.json")

    upstream.add(BLS, respond, method="POST")
    # omni dates each month from BLS's archive of the release (LNS: the Employment Situation),
    # which it reads at most once an hour per process: tests count the API requests only
    upstream.text_file(r"www\.bls\.gov/bls/news-release/empsit\.htm", "bls.release_archive__empsit.html")


def bls_bodies(upstream) -> list[dict]:
    return [json.loads(r.content) for r in upstream.requests if r.url.host == "api.bls.gov"]


# --- macro_series ------------------------------------------------------------------------------


def test_macro_series_golden(tmp_path, upstream, keys):
    fred_routes(upstream)
    out = call("macro_series", context(tmp_path), series_ids=["cpiaucsl", "ZZTEST01"])
    doc = check_golden(out, "macro_series")
    cpi = [r for r in doc["rows"] if r["series_id"] == "CPIAUCSL"]
    assert [(r["date"], r["value"]) for r in cpi] == [
        ("2024-01-01", 308.5),
        ("2024-02-01", 310.1),
        ("2024-03-01", None),
    ]
    assert cpi[2]["absent"] == {"value": "no_data"}
    assert doc["absent"]["period"]["code"] == "not_applicable"
    assert doc["provenance"]["dataset"] == "fred.series"
    assert doc["provenance"]["authority"] == ["official"]
    assert doc["provenance"]["cached"] is False
    # the key went to FRED, and nowhere into the answer
    assert {upstream.params(i)["api_key"] for i in range(len(upstream.requests))} == {KEYS["FRED_API_KEY"]}
    assert KEYS["FRED_API_KEY"] not in json.dumps(doc)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        ("2024-02-01", []),
        # FRED's 2024-02-13 vintage is knowable from 17:00 St. Louis time that day: 23:00 UTC in winter
        ("2024-02-13T22:30:00Z", []),
        ("2024-02-13T23:00:00Z", [("2024-01-01", 308.417)]),
        ("2024-03-01", [("2024-01-01", 308.417)]),
        # FRED's 2024-03-12 vintage is knowable from 22:00 UTC that day (daylight time), not from its midnight
        ("2024-03-12", [("2024-01-01", 308.417)]),
        ("2024-03-12T22:00:00Z", [("2024-01-01", 308.5), ("2024-02-01", 310.326)]),
        ("2024-04-11", [("2024-01-01", 308.5), ("2024-02-01", 310.1), ("2024-03-01", None)]),
    ],
)
def test_macro_series_as_of_two_vintages(tmp_path, upstream, keys, as_of, expected):
    fred_routes(upstream)
    out = call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"], as_of=as_of)
    assert [(r.date.isoformat(), r.value) for r in out.rows] == expected
    assert out.provenance.as_of.isoformat().startswith(as_of[:10])


def test_macro_series_vintages_and_window(tmp_path, upstream, keys):
    fred_routes(upstream)
    out = call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"], include_vintages=True)
    check_golden(out, "macro_series__vintages")
    assert [(r.date.isoformat(), r.value, r.knowledge_time.date().isoformat()) for r in out.rows] == [
        ("2024-01-01", 308.417, "2024-02-13"),
        ("2024-01-01", 308.5, "2024-03-12"),
        ("2024-02-01", 310.326, "2024-03-12"),
        ("2024-02-01", 310.1, "2024-04-10"),
        ("2024-03-01", None, "2024-04-10"),
    ]
    out = call(
        "macro_series", context(tmp_path), series_ids=["CPIAUCSL"], start="2024-02-01", end="2024-02-29"
    )
    assert [r.date.isoformat() for r in out.rows] == ["2024-02-01"]


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        # between the January print (2024-02-13) and its revision (2024-03-12)
        ("2024-03-01", [("2024-01-01", 308.417)]),
        # the revision's release (22:00 UTC that day): both January vintages and February's first print,
        # nothing from 2024-04-10
        ("2024-03-12T22:00:00Z", [("2024-01-01", 308.417), ("2024-01-01", 308.5), ("2024-02-01", 310.326)]),
    ],
)
def test_macro_series_vintages_never_show_one_published_after_as_of(
    tmp_path, upstream, keys, as_of, expected
):
    from marketlens_mcp.providers.data.tools.common import parse_instant

    fred_routes(upstream)
    out = call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"], include_vintages=True, as_of=as_of)
    assert [(r.date.isoformat(), r.value) for r in out.rows] == expected
    assert all(r.knowledge_time <= parse_instant(as_of) for r in out.rows)
    assert 310.1 not in [r.value for r in out.rows]  # the 2024-04-10 revision


def test_macro_series_end_is_inclusive(tmp_path, upstream, keys):
    fred_routes(upstream)
    out = call(
        "macro_series", context(tmp_path), series_ids=["CPIAUCSL"], start="2024-01-01", end="2024-02-01"
    )
    assert [r.date.isoformat() for r in out.rows] == ["2024-01-01", "2024-02-01"]


def test_macro_series_refuses_bad_ids_and_windows(tmp_path):
    from pydantic import ValidationError

    from marketlens_mcp.providers.data.tools.macro import SeriesInputs

    with pytest.raises(ValidationError):
        SeriesInputs(series_ids=["not an id"])
    with pytest.raises(ValidationError):
        SeriesInputs(series_ids=[f"S{i}" for i in range(21)])
    with pytest.raises(ValidationError, match="zone"):
        SeriesInputs(series_ids=["UNRATE"], as_of="2024-03-01T10:00:00")
    err = refused(
        "macro_series", context(tmp_path), series_ids=["UNRATE"], start="2024-02-01", end="2024-01-01"
    )
    assert (err.code, err.message) == (
        "invalid_arguments",
        "start (2024-02-01) must not be after end (2024-01-01).",
    )


# --- macro_series_catalog ------------------------------------------------------------------------

CATALOG = {
    "CPIAUCSL": {
        "name": "CPI (All Items)",
        "category": "Inflation",
        "unit": "Index 1982-84=100",
        "chart_type": "line",
        "polarity": "up_is_risk_off",
        "description": "synthetic",
    },
    "UNRATE": {
        "name": "Unemployment Rate",
        "category": "Employment",
        "unit": "%",
        "chart_type": "line",
        "polarity": "up_is_risk_off",
    },
    "ZZSYN": {"name": "Synthetic series", "category": "Custom"},
}


def test_macro_series_catalog_golden(tmp_path, monkeypatch):
    import omni.catalog

    monkeypatch.setattr(omni.catalog, "series_meta", lambda: CATALOG)
    out = call("macro_series_catalog", context(tmp_path))
    check_golden(out, "macro_series_catalog")
    assert [r.series_id for r in out.rows] == ["CPIAUCSL", "UNRATE", "ZZSYN"]
    assert out.rows[2].units is None and out.rows[2].absent == {
        "units": "not_provided_by_source",
        "chart_type": "not_provided_by_source",
        "polarity": "not_provided_by_source",
    }
    assert [r.series_id for r in call("macro_series_catalog", context(tmp_path), q="unemploy").rows] == [
        "UNRATE"
    ]
    assert [r.series_id for r in call("macro_series_catalog", context(tmp_path), q="cpi").rows] == [
        "CPIAUCSL"
    ]
    rows = call("macro_series_catalog", context(tmp_path), category="inflation").rows
    assert [r.series_id for r in rows] == ["CPIAUCSL"]


def test_macro_series_catalog_reads_the_shipped_catalogue(tmp_path):
    from omni.catalog import series_meta_for

    rows = call("macro_series_catalog", context(tmp_path), q="UNRATE").rows
    assert rows and rows[0].series_id == "UNRATE"
    assert rows[0].name == series_meta_for("UNRATE")["name"]


# --- macro_bls_series ----------------------------------------------------------------------------


def test_macro_bls_series_golden_in_decade_blocks(tmp_path, upstream, keys):
    bls_routes(upstream)
    out = call("macro_bls_series", context(tmp_path), series_ids=["lns14000000"])
    doc = check_golden(out, "macro_bls_series")
    blocks = sorted((b["startyear"], b["endyear"]) for b in bls_bodies(upstream))
    assert blocks == [("2010", "2019"), ("2020", "2026")]  # default 10 years back; capped at this year
    assert {b["registrationkey"] for b in bls_bodies(upstream)} == {KEYS["BLS_API_KEY"]}
    # 2016-09 is before the default window (10 years back from 2026-10-02); M13 annual rows dropped
    assert [(r["date"], r["period"], r["value"]) for r in doc["rows"]] == [
        ("2016-10-01", "M10", 4.9),
        ("2016-11-01", "M11", 4.7),
        ("2019-12-01", "M12", 3.6),
        ("2020-04-01", "M04", 14.8),
        ("2026-08-01", "M08", None),
        ("2026-09-01", "M09", 4.3),
    ]
    # fetched after the 10-02 jobs report: that report, 08:30 EDT (was its own: 2016-11-04)
    assert doc["rows"][0]["knowledge_time"] == "2026-10-02T12:30:00Z"


def test_macro_bls_series_history_from_the_first_sync_is_at_its_latest_release(tmp_path, upstream, keys):
    # The first sync (20:00Z on 2026-10-02) came after that morning's jobs report, and BLS serves
    # each month's latest revision only, which that report or any before it may have published:
    # every row dates from 08:30 EDT that day, so an earlier as_of returns none (omni A5, B2).
    from marketlens_mcp.providers.data.tools.common import parse_instant
    from marketlens_mcp.providers.data.tools.macro import SPECS

    bls_routes(upstream)
    out = call("macro_bls_series", context(tmp_path), series_ids=["LNS14000000"])
    assert {r.knowledge_time for r in out.rows} == {parse_instant("2026-10-02T12:30:00Z")}
    before = call(
        "macro_bls_series", context(tmp_path), series_ids=["LNS14000000"], as_of="2026-10-02T12:29:00Z"
    )
    assert before.rows == []
    description = next(s for s in SPECS if s.name == "macro_bls_series").description
    assert "history older than the first sync takes that sync's latest release" in description
    assert "real earnings: CPI or jobs report" in description
    assert "SA CPI from December's to January's CPI" in description
    assert "the archive lagging or unread" in description
    assert "A later revision is a new vintage at its release, else at its fetch." in description


def test_macro_bls_series_quarterly_golden(tmp_path, upstream, keys):
    # The Employment Cost Index is quarterly: a quarter is dated at its first day (period Q01-Q04,
    # frequency Q) and is in the window when any of its months is, so a start in February keeps
    # the first quarter and drops the fourth of 2025
    upstream.json_file(BLS, "bls.timeseries__CIS1010000000000I.json", method="POST")
    upstream.text_file(r"www\.bls\.gov/bls/news-release/eci\.htm", "bls.release_archive__eci.html")
    out = call("macro_bls_series", context(tmp_path), series_ids=["CIS1010000000000I"], start="2026-02-15")
    doc = check_golden(out, "macro_bls_series__quarterly")
    assert [(r["date"], r["period"], r["frequency"], r["value"]) for r in doc["rows"]] == [
        ("2026-01-01", "Q01", "Q", 174.1),
        ("2026-04-01", "Q02", "Q", 175.3),
    ]
    # fetched after the 07-31 ECI release (the second quarter's, listed as June): 08:30 EDT
    assert {r["knowledge_time"] for r in doc["rows"]} == {"2026-07-31T12:30:00Z"}


def test_macro_bls_series_without_a_key_sends_none(tmp_path, upstream):
    bls_routes(upstream)
    call("macro_bls_series", context(tmp_path), series_ids=["LNS14000000"], start="2021-01-01")
    bodies = bls_bodies(upstream)
    assert [(b["startyear"], b["endyear"]) for b in bodies] == [("2020", "2026")]
    assert all("registrationkey" not in b for b in bodies)


# --- macro_bea_table -----------------------------------------------------------------------------


def test_macro_bea_table_golden(tmp_path, upstream, keys):
    upstream.json_file(BEA, "bea.nipa__T10101.json")
    out = call("macro_bea_table", context(tmp_path), table_name="t10101")
    doc = check_golden(out, "macro_bea_table")
    sent = upstream.params(0)
    assert (sent["TableName"], sent["Frequency"], sent["UserID"]) == ("T10101", "Q", KEYS["BEA_API_KEY"])
    assert [(r["series_id"], r["period"], r["line_number"], r["value"]) for r in doc["rows"]] == [
        ("A191RL", "Q1", 1, 1.4),
        ("A191RL", "Q2", 1, 2.1),
        ("DPCERL", "Q1", 2, 0.9),
        ("DPCERL", "Q2", 2, None),
    ]
    only = call("macro_bea_table", context(tmp_path), table_name="T10101", line_numbers=[2])
    assert {r.line_number for r in only.rows} == {2}


def test_macro_bea_table_history_from_the_first_sync_is_at_its_last_revision_not_the_sync(
    tmp_path, upstream, keys
):
    # The first sync's payload says "LastRevised: July 30, 2026": all it holds is knowable from
    # 08:30 ET that day, before the sync, so an as_of between the two returns rows (review).
    from marketlens_mcp.providers.data.tools.common import parse_instant
    from marketlens_mcp.providers.data.tools.macro import SPECS

    upstream.json_file(BEA, "bea.nipa__T10101.json")
    out = call("macro_bea_table", context(tmp_path), table_name="T10101")
    assert {r.knowledge_time for r in out.rows} == {parse_instant("2026-07-30T12:30:00Z")}
    between = call("macro_bea_table", context(tmp_path), table_name="T10101", as_of="2026-08-01")
    assert len(between.rows) == len(out.rows)
    before = call("macro_bea_table", context(tmp_path), table_name="T10101", as_of="2026-07-30T12:29:00Z")
    assert before.rows == []
    description = next(s for s in SPECS if s.name == "macro_bea_table").description
    assert "history older than the first sync takes that sync's last revision date" in description


BEA_LEVEL_NOTE = (
    "A value whose units is just 'Level' was stored without BEA's multiplier (UNIT_MULT; rows stored before "
    "marketlens-data kept it have none), so its scale is not known: NIPA dollar levels are usually in millions of "
    "dollars. Check the table's header on bea.gov before reading a magnitude."
)


def test_macro_bea_table_level_units_state_bea_measure_and_multiplier(tmp_path, upstream, keys):
    # A dollar-level table: BEA sends 31,012,345 with METRIC_NAME Current Dollars and
    # UNIT_MULT 6 (millions). marketlens-data keeps both (unit_mult, metric_name), so the
    # units say the scale and no note is needed (review 2026-10-19: they were dropped).
    upstream.json_file(BEA, "bea.nipa__T10105.json")
    out = call("macro_bea_table", context(tmp_path), table_name="T10105")
    assert out.notes == []
    doc = check_golden(out, "macro_bea_table__level")
    assert [(r["series_id"], r["units"], r["value"]) for r in doc["rows"]][:2] == [
        ("A191RC", "Level, millions of Current Dollars", 31012345.0),
        ("A191RC", "Level, millions of Current Dollars", 31456789.0),
    ]
    from marketlens_mcp.providers.data.tools.macro import SPECS

    description = next(s for s in SPECS if s.name == "macro_bea_table").description
    # A 'Level' row BEA scales by 10^0 (a Ratio, a Fisher index level) is in ones: a row stored
    # without UNIT_MULT has an unknown scale, not a wrong one (review 2026-10-19).
    assert "'Level, millions of Current Dollars'" in description and "not in ones" not in description
    assert "just 'Level' was stored without the scale, which is then not known" in description


def test_macro_bea_table_level_without_a_multiplier_keeps_the_note(tmp_path, upstream, keys):
    # A row stored before marketlens-data kept UNIT_MULT reads back without it, as
    # does one BEA sent without it: its units stay 'Level' and the note says why.
    from data_harness import fixture_json

    payload = fixture_json("bea.nipa__T10105.json")
    for item in payload["BEAAPI"]["Results"]["Data"]:
        del item["UNIT_MULT"], item["METRIC_NAME"]
    upstream.add(BEA, payload)
    out = call("macro_bea_table", context(tmp_path), table_name="T10105")
    assert out.notes == [BEA_LEVEL_NOTE]
    assert {r.units for r in out.rows} == {"Level"}


@pytest.mark.parametrize(
    ("unit", "measure", "power", "units"),
    [
        ("Level", "Current Dollars", 6, "Level, millions of Current Dollars"),
        ("Level", "Persons", 3, "Level, thousands of Persons"),
        ("Level", "Ratio", 0, "Level, Ratio"),
        ("Level", "Chained Dollars", 12, "Level, 10^12 Chained Dollars"),
        ("Level", None, 9, "Level, billions of units"),
        ("Level", "Current Dollars", None, "Level"),
        ("Percent change, annual rate", "Fisher Quantity Index", 0, "Percent change, annual rate"),
    ],
)
def test_bea_units(unit, measure, power, units):
    from marketlens_mcp.providers.data.tools.macro import _bea_units

    assert _bea_units(unit, measure, power) == units


def test_macro_bea_table_needs_its_key(tmp_path, upstream):
    upstream.json_file(BEA, "bea.nipa__T10101.json")
    err = refused("macro_bea_table", context(tmp_path), table_name="T10101")
    assert err.code == "data_key_missing"
    assert err.message == "BEA needs BEA_API_KEY, which is not set in this server's environment."
    assert err.hint == (
        "Get a free key at https://apps.bea.gov/API/signup/ and add BEA_API_KEY to the env block of this "
        "server in your MCP client's configuration."
    )
    assert upstream.requests == []


# --- macro_release_schedule ----------------------------------------------------------------------


def test_macro_release_schedule_golden(tmp_path, upstream, frozen_clock):
    upstream.text_file(r"www\.bls\.gov/schedule/news_release/cpi\.htm", "bls.cpi_schedule.html")
    upstream.text_file(r"www\.bls\.gov/schedule/news_release/empsit\.htm", "bls.empsit_schedule.html")
    upstream.text_file(r"www\.bls\.gov/schedule/\d{4}/\d{2}_sched_list\.htm", "bls.schedule_month_list.html")
    out = call("macro_release_schedule", context(tmp_path))
    doc = check_golden(out, "macro_release_schedule")
    assert [(r["release"], r["start_date"], r["scheduled_at"]) for r in doc["rows"]] == [
        ("cpi", "2026-09-11", "2026-09-11T12:30:00Z"),
        ("employment_situation", "2026-10-02", "2026-10-02T12:30:00Z"),
        ("cpi", "2026-10-15", "2026-10-15T12:30:00Z"),
        ("employment_situation", "2026-11-06", "2026-11-06T13:30:00Z"),
        ("cpi", "2026-11-13", "2026-11-13T13:30:00Z"),
    ]
    # The route names every page the sync reads: each release's page and this and next month's
    # lists of releases (review 2026-10-19: the lists were missing from it and from the README).
    assert {r.url.path for r in upstream.requests} == {
        "/schedule/news_release/cpi.htm",
        "/schedule/news_release/empsit.htm",
        "/schedule/2026/10_sched_list.htm",
        "/schedule/2026/11_sched_list.htm",
    }
    assert out.provenance.route == (
        "omni bls.cpi_schedule, bls.empsit_schedule <- www.bls.gov/schedule/news_release, "
        "schedule/{year}/{MM}_sched_list.htm (this and next month's releases)"
    )
    only = call("macro_release_schedule", context(tmp_path), releases=["cpi"], start="2026-10-01")
    assert [r.start_date.isoformat() for r in only.rows] == ["2026-10-15", "2026-11-13"]


# --- macro_census_trade --------------------------------------------------------------------------

CENSUS_TRADE = r"api\.census\.gov/data/timeseries/intltrade/(exports|imports)/hs"


def census_routes(upstream):
    upstream.json_file(CENSUS_TRADE, "census.trade__exports_8542.json")


def boj_routes(upstream):
    upstream.json_file(r"www\.stat-search\.boj\.or\.jp/api/v1/getDataCode", "boj.series__STRDCLUCON.json")


def boe_routes(upstream):
    upstream.text_file(
        r"www\.bankofengland\.co\.uk/boeapps/database/_iadb-fromshowcolumns\.asp", "boe.series__IUDBEDR.csv"
    )


def test_macro_census_trade_golden(tmp_path, upstream, keys):
    census_routes(upstream)
    out = call("macro_census_trade", context(tmp_path), hs_codes=["8542"], flow="exports")
    doc = check_golden(out, "macro_census_trade")
    china = [r for r in doc["rows"] if r["country_code"] == "5700"]
    assert [(r["month"], r["value_usd"]) for r in china] == [
        ("2025-01-01", 1000000000.0),
        ("2025-02-01", 1100000000.0),
    ]
    # the total for all countries ("-" upstream) is TOTAL; 2025 is revised, so knowable at the revision
    assert {r["country_code"] for r in doc["rows"]} == {"TOTAL", "5700", "0003"}
    assert china[0]["knowledge_time"] == "2026-07-01T03:59:00Z" and china[0]["stamp_estimated"] is True
    assert china[0]["quantity"] is None and china[0]["absent"]["quantity"] == "not_applicable"
    assert upstream.params(0)["E_COMMODITY"] == "8542"
    assert doc["provenance"]["authority"] == ["official"]
    only = call(
        "macro_census_trade",
        context(tmp_path),
        hs_codes=["8542"],
        flow="exports",
        country_codes=["0003"],
        start="2025-02-01",
    )
    assert [(r.country_code, r.month.isoformat()) for r in only.rows] == [
        ("0003", "2025-02-01"),
        ("0003", "2025-03-01"),
    ]


def test_macro_census_trade_needs_the_census_key(tmp_path, upstream, monkeypatch):
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    err = refused("macro_census_trade", context(tmp_path), hs_codes=["8542"], flow="exports")
    assert err.code == "data_key_missing" and "CENSUS_API_KEY" in err.message
    with pytest.raises(ValueError, match="not a valid code"):
        call("macro_census_trade", context(tmp_path), hs_codes=["chips"])


# --- macro_boj_series and macro_boe_series ---------------------------------------------------------


def test_macro_boj_series_golden(tmp_path, upstream):
    boj_routes(upstream)
    out = call("macro_boj_series", context(tmp_path), database="fm01", series_codes=["STRDCLUCON"])
    doc = check_golden(out, "macro_boj_series")
    assert [(r["period"], r["value"]) for r in doc["rows"]] == [
        ("2025-09-25", 0.477),
        ("2025-09-26", 0.478),
        ("2025-09-29", 0.477),
    ]  # a weekend's null is no observation
    assert doc["rows"][0]["units"] == "percent per annum" and doc["rows"][0]["bank"] == "boj"
    # a day: knowable a week on, or at the end of the series' last update (2025-09-30, JST)
    assert [r["knowledge_time"] for r in doc["rows"]] == [
        "2025-09-30T14:59:59Z",
        "2025-09-30T14:59:59Z",
        "2025-09-30T14:59:59Z",
    ]
    assert upstream.params(0) == {"format": "json", "lang": "en", "db": "FM01", "code": "STRDCLUCON"}


def test_macro_boe_series_golden(tmp_path, upstream):
    boe_routes(upstream)
    out = call("macro_boe_series", context(tmp_path), series_codes=["iudbedr"])
    doc = check_golden(out, "macro_boe_series")
    assert [(r["date"], r["value"], r["knowledge_time"]) for r in doc["rows"]] == [
        ("2025-09-22", 4.0, "2025-09-29T00:00:00Z"),
        ("2025-09-23", 4.0, "2025-09-30T00:00:00Z"),
        ("2025-09-24", 4.0, "2025-10-01T00:00:00Z"),
    ]
    assert doc["rows"][0]["absent"] == {"units": "not_provided_by_source"}
    assert upstream.params(0)["SeriesCodes"] == "IUDBEDR" and doc["notes"]


# --- macro_jgb_yields --------------------------------------------------------------------------------


def jgb_routes(upstream):
    import httpx
    from data_harness import fixture_text

    mof = r"www\.mof\.go\.jp/english/policy/jgbs/reference/interest_rate"
    upstream.text_file(rf"{mof}/index\.htm", "mof.interest_rate_page.html")
    upstream.add(
        rf"{mof}/jgbcme\.csv",
        lambda _r: httpx.Response(
            200,
            text=fixture_text("mof.jgbcme.csv"),
            headers={"Last-Modified": "Fri, 02 Oct 2026 23:30:00 GMT"},
        ),
    )
    upstream.add(
        rf"{mof}/historical/jgbcme_all\.csv",
        lambda _r: httpx.Response(
            200,
            text=fixture_text("mof.jgbcme_all.csv"),
            headers={"Last-Modified": "Thu, 01 Oct 2026 23:30:00 GMT"},
        ),
    )


JGB = {"tenors": ["10Y", "40Y"], "start": "2026-09-28", "end": "2026-10-02"}


def test_macro_jgb_yields_golden(tmp_path, upstream):
    jgb_routes(upstream)
    out = call("macro_jgb_yields", context(tmp_path), **JGB)
    doc = check_golden(out, "macro_jgb_yields")
    assert [(r["date"], r["tenor"], r["rate"]) for r in doc["rows"]] == [
        ("2026-09-29", "10Y", 0.0308),
        ("2026-09-30", "10Y", 0.0306),
        ("2026-09-30", "40Y", 0.041),
        ("2026-10-01", "10Y", 0.031),
        ("2026-10-01", "40Y", 0.042),
        ("2026-10-02", "10Y", 0.031),
        ("2026-10-02", "40Y", 0.0417),
    ]  # no 40Y on 09-29 ("-")
    # knowable when MOF wrote the file holding the day (before 12:00 JST a week on)
    assert doc["rows"][0]["knowledge_time"] == "2026-10-01T23:30:00Z"
    assert doc["rows"][-1]["knowledge_time"] == "2026-10-02T23:30:00Z"
    assert doc["rows"][0]["tenor_label"] == "10 Yr" and doc["provenance"]["authority"] == ["official"]
    # a window within this month reads the current file only
    upstream.requests.clear()
    call("macro_jgb_yields", context(tmp_path), start="2026-10-01", end="2026-10-02")
    assert not [r for r in upstream.requests if "historical" in r.url.path]


# --- macro_regional_fed_surveys --------------------------------------------------------------------


def _richmond_workbook() -> bytes:
    """A synthetic mfg_historicaldata.xlsx with every column marketlens-data collects."""
    import io

    import openpyxl
    from omni import config

    columns = list(config.source_config("regionalfed")["series"]["richmond"]["manufacturing"])
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["date", *columns])
    from datetime import datetime

    for month, value in ((7, 5), (8, 4), (9, -2)):
        ws.append([datetime(2025, month, 1), *[value] * len(columns)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def regional_routes(upstream):
    rich = r"www\.richmondfed\.org"
    upstream.text_file(
        rf"{rich}/region_communities/regional_data_analysis/business_surveys/manufacturing",
        "regionalfed.richmond_manufacturing.html",
    )
    upstream.text_file(
        rf"{rich}/region_communities/regional_data_analysis/business_surveys/about",
        "regionalfed.richmond_about.html",
    )
    book = _richmond_workbook()
    upstream.add(rf"{rich}/-/media/.+/mfg_historicaldata\.xlsx", lambda _r: book)


REGIONAL = {"bank": "richmond", "survey": "manufacturing"}


def test_macro_regional_fed_surveys_golden(tmp_path, upstream):
    regional_routes(upstream)
    out = call(
        "macro_regional_fed_surveys",
        context(tmp_path),
        bank="richmond",
        survey="manufacturing",
        series=["sa_mfg_composite", "nsa_mfg_pct_chg_prcs_pd_c"],
    )
    doc = check_golden(out, "macro_regional_fed_surveys")
    composite = [r for r in doc["rows"] if r["series"] == "sa_mfg_composite"]
    assert [(r["month"], r["value"]) for r in composite] == [
        ("2025-07-01", 5.0),
        ("2025-08-01", 4.0),
        ("2025-09-01", -2.0),
    ]
    # September is on the schedule (10:10 ET); July and August are estimated a week after the month
    assert (composite[2]["knowledge_time"], composite[2]["release_estimated"]) == (
        "2025-09-23T14:10:00Z",
        False,
    )
    assert (composite[0]["knowledge_time"], composite[0]["release_estimated"]) == (
        "2025-08-08T03:59:00Z",
        True,
    )
    assert {r["unit"] for r in doc["rows"] if r["series"] == "nsa_mfg_pct_chg_prcs_pd_c"} == {"percent"}


# --- credit_trace_aggregates -----------------------------------------------------------------------


def trace_routes(upstream):
    upstream.add(
        r"ews\.fintech\.finra\.org/fip/rest/ews/oauth2/access_token",
        {"access_token": "synthetictoken", "token_type": "Bearer", "expires_in": 1800},
        method="POST",
    )

    def respond(request):
        from data_harness import fixture_json

        return (
            fixture_json("finra.trace__corporateMarketBreadth.json")["rows"]
            if (json.loads(request.content)["offset"] == 0)
            else []
        )

    upstream.add(
        r"api\.finra\.org/data/group/fixedIncomeMarket/name/corporateMarketBreadth", respond, method="POST"
    )


TRACE = {"report": "corporateMarketBreadth", "start": "2025-09-24", "end": "2025-09-25"}


def test_credit_trace_aggregates_golden(tmp_path, upstream, keys):
    trace_routes(upstream)
    out = call(
        "credit_trace_aggregates",
        context(tmp_path),
        report="corporateMarketBreadth",
        start="2025-09-24",
        end="2025-09-25",
        metrics=["advances", "declines"],
    )
    doc = check_golden(out, "credit_trace_aggregates")
    assert [(r["date"], r["metric"], r["value"]) for r in doc["rows"]] == [
        ("2025-09-24", "advances", 3000.0),
        ("2025-09-24", "declines", 3500.0),
        ("2025-09-25", "advances", 3600.0),
        ("2025-09-25", "declines", 3100.0),
    ]
    # Wednesday's statistics: 23:59 ET of Thursday, flagged as the rule's estimate
    assert doc["rows"][0]["knowledge_time"] == "2025-09-26T03:59:00Z" and doc["rows"][0]["stamp_estimated"]
    body = json.loads(upstream.requests[1].content)
    assert body["dateRangeFilters"][0] == {
        "fieldName": "tradeReportDate",
        "startDate": "2025-09-24",
        "endDate": body["dateRangeFilters"][0]["endDate"],
    }
    assert upstream.requests[1].headers["Authorization"] == "Bearer synthetictoken"
    for secret in ("finrasyntheticid", "finrasyntheticsecret", "synthetictoken"):
        assert secret not in json.dumps(doc)


def test_credit_trace_aggregates_needs_finras_api_client(tmp_path, upstream, monkeypatch):
    for name in ("FINRA_API_CLIENT_ID", "FINRA_API_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    err = refused("credit_trace_aggregates", context(tmp_path), report="treasuryDailyAggregates")
    assert err.code == "data_key_missing"
    assert "FINRA_API_CLIENT_ID and FINRA_API_CLIENT_SECRET" in err.message
    assert upstream.requests == []
