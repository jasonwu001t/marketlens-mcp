"""macro_series, macro_series_catalog, macro_bls_series, macro_bea_table and
macro_release_schedule against synthetic FRED, BLS and BEA payloads."""

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
        ("2024-03-01", [("2024-01-01", 308.417)]),
        # FRED's 2024-03-12 vintage is knowable from 22:00 UTC that day, not from its midnight
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
    blocks = sorted(
        (b["startyear"], b["endyear"]) for b in (json.loads(r.content) for r in upstream.requests)
    )
    assert blocks == [("2010", "2019"), ("2020", "2026")]  # default 10 years back; capped at this year
    assert {json.loads(r.content)["registrationkey"] for r in upstream.requests} == {KEYS["BLS_API_KEY"]}
    # 2016-09 is before the default window (10 years back from 2026-10-02); M13 annual rows dropped
    assert [(r["date"], r["period"], r["value"]) for r in doc["rows"]] == [
        ("2016-10-01", "M10", 4.9),
        ("2016-11-01", "M11", 4.7),
        ("2019-12-01", "M12", 3.6),
        ("2020-04-01", "M04", 14.8),
        ("2026-08-01", "M08", None),
        ("2026-09-01", "M09", 4.3),
    ]
    assert doc["rows"][0]["knowledge_time"] == "2016-11-05T00:00:00Z"  # month + 35-day release lag


def test_macro_bls_series_without_a_key_sends_none(tmp_path, upstream):
    bls_routes(upstream)
    call("macro_bls_series", context(tmp_path), series_ids=["LNS14000000"], start="2021-01-01")
    bodies = [json.loads(r.content) for r in upstream.requests]
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


BEA_LEVEL_NOTE = (
    "BEA states each value's multiplier (UNIT_MULT) separately and it is not stored, so a value whose units "
    "is 'Level' is not in ones: NIPA dollar levels are usually in millions of dollars. Check the table's "
    "header on bea.gov before reading a magnitude."
)


def test_macro_bea_table_level_values_say_their_multiplier_is_not_stored(tmp_path, upstream, keys):
    # A dollar-level table: BEA sends 31,012,345 with UNIT_MULT 6 (millions); the
    # stored row keeps only units 'Level', so the answer must say the scale is lost.
    upstream.json_file(BEA, "bea.nipa__T10105.json")
    out = call("macro_bea_table", context(tmp_path), table_name="T10105")
    assert out.notes == [BEA_LEVEL_NOTE]
    doc = check_golden(out, "macro_bea_table__level")
    assert [(r["series_id"], r["units"], r["value"]) for r in doc["rows"]][:2] == [
        ("A191RC", "Level", 31012345.0),
        ("A191RC", "Level", 31456789.0),
    ]
    from marketlens_mcp.providers.data.tools.macro import SPECS

    description = next(s for s in SPECS if s.name == "macro_bea_table").description
    assert "multiplier" in description and "millions" in description


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
    out = call("macro_release_schedule", context(tmp_path))
    doc = check_golden(out, "macro_release_schedule")
    assert [(r["release"], r["start_date"], r["scheduled_at"]) for r in doc["rows"]] == [
        ("cpi", "2026-09-11", "2026-09-11T12:30:00Z"),
        ("employment_situation", "2026-10-02", "2026-10-02T12:30:00Z"),
        ("cpi", "2026-10-15", "2026-10-15T12:30:00Z"),
        ("employment_situation", "2026-11-06", "2026-11-06T13:30:00Z"),
        ("cpi", "2026-11-13", "2026-11-13T13:30:00Z"),
    ]
    only = call("macro_release_schedule", context(tmp_path), releases=["cpi"], start="2026-10-01")
    assert [r.start_date.isoformat() for r in only.rows] == ["2026-10-15", "2026-11-13"]
