"""Point in time passes through: every data tool hands the as_of it was given,
unchanged, to every marketlens-data read it makes (omni.query and
omni.read_raw), on the golden fixtures of the tool's own test module. The one
exception is SEC's fund ticker list (sec.fund_tickers), which maps a symbol to
its fund series and is read as it stands today."""

from __future__ import annotations

import pytest
from data_harness import SPECS, call, context
from test_calendars import earnings_routes, economic_routes, holiday_routes, surprise_routes
from test_fed_treasury import FISCAL, fomc_routes, treasury_routes
from test_macro import BEA, bls_routes, fred_routes
from test_sec import sec_routes

from marketlens_mcp.providers.data.tools import datasets_by_tool
from marketlens_mcp.providers.data.tools.common import parse_instant

AS_OF = "2026-10-02T20:00:00Z"  # data_harness.NOW: the frozen captures are knowable at it
EXEMPT = {"sec.fund_tickers"}


def schedule_routes(upstream):
    upstream.text_file(r"www\.bls\.gov/schedule/news_release/cpi\.htm", "bls.cpi_schedule.html")
    upstream.text_file(r"www\.bls\.gov/schedule/news_release/empsit\.htm", "bls.empsit_schedule.html")


def file_route(pattern, name):
    return lambda upstream: upstream.json_file(pattern, name)


def no_routes(upstream):
    pass


DAYS = {"start": "2026-10-01", "end": "2026-10-02"}
#: (tool, the routes its golden test installs, its arguments)
CASES = [
    ("macro_series", fred_routes, {"series_ids": ["CPIAUCSL", "ZZTEST01"]}),
    ("macro_series", fred_routes, {"series_ids": ["CPIAUCSL"], "include_vintages": True}),
    ("macro_series_catalog", no_routes, {}),
    ("macro_bls_series", bls_routes, {"series_ids": ["LNS14000000"]}),
    ("macro_bea_table", file_route(BEA, "bea.nipa__T10101.json"), {"table_name": "T10101"}),
    ("macro_release_schedule", schedule_routes, {}),
    ("sec_filings", sec_routes, {"ticker": "TESTCO"}),
    ("sec_xbrl_facts", sec_routes, {"ticker": "TESTCO", "metrics": ["Revenues"]}),
    ("sec_xbrl_facts", sec_routes, {"ticker": "TESTCO", "metrics": ["Revenues"], "include_vintages": True}),
    ("sec_fundamentals", sec_routes, {"ticker": "TESTCO"}),
    ("sec_earnings_releases", sec_routes, {"ticker": "TESTCO"}),
    ("sec_earnings_figures", sec_routes, {"ticker": "TESTCO"}),
    ("sec_insider_trades", sec_routes, {"ticker": "TESTCO"}),
    ("sec_13f_holdings", sec_routes, {"filer_cik": "9999950"}),
    ("sec_fund_nport", sec_routes, {"ticker": "TFUND"}),
    ("sec_fund_holdings", sec_routes, {"ticker": "TFUND"}),
    ("fed_fomc_meetings", fomc_routes, {}),
    ("fed_fomc_statements", fomc_routes, {}),
    (
        "fed_reference_rates",
        file_route(r"markets\.newyorkfed\.org/api/rates/all/search\.json", "nyfed.reference_rates.json"),
        {},
    ),
    ("treasury_yield_curve", treasury_routes, {}),
    (
        "treasury_auctions",
        file_route(rf"{FISCAL}/v1/accounting/od/auctions_query", "fiscaldata.auctions.json"),
        {},
    ),
    (
        "treasury_debt",
        file_route(rf"{FISCAL}/v2/accounting/od/debt_to_penny", "fiscaldata.debt_to_penny.json"),
        {},
    ),
    (
        "treasury_tga",
        file_route(rf"{FISCAL}/v1/accounting/dts/operating_cash_balance", "fiscaldata.tga_balance.json"),
        {},
    ),
    ("calendar_economic", economic_routes, DAYS),
    ("calendar_earnings", earnings_routes, DAYS),
    ("calendar_earnings_history", surprise_routes, {"tickers": ["TESTCO"]}),
    ("calendar_us_holidays", holiday_routes, {}),
]


def _id(case) -> str:
    tool, _, arguments = case
    return tool + ("[vintages]" if arguments.get("include_vintages") else "")


def test_every_tool_has_a_case():
    assert {tool for tool, *_ in CASES} == set(SPECS) and len(SPECS) == 25


@pytest.fixture
def reads(monkeypatch):
    """Every (dataset, as_of) the tools ask marketlens-data for, in order."""
    import omni

    seen: list[tuple[str, object]] = []

    def spy(name):
        real = getattr(omni, name)

        def wrapper(dataset_id, *args, **kwargs):
            seen.append((dataset_id, kwargs.get("as_of", "as_of not passed")))
            return real(dataset_id, *args, **kwargs)

        monkeypatch.setattr(omni, name, wrapper)

    spy("query")
    spy("read_raw")
    return seen


@pytest.mark.parametrize("case", CASES, ids=[_id(c) for c in CASES])
def test_every_read_gets_the_tools_as_of(tmp_path, upstream, keys, frozen_clock, reads, case):
    tool, routes, arguments = case
    routes(upstream)
    if "as_of" not in SPECS[tool].input_model.model_fields:
        call(tool, context(tmp_path), **arguments)  # a local catalogue: nothing point in time
        assert datasets_by_tool()[tool] == () and reads == []
        return
    out = call(tool, context(tmp_path), **arguments, as_of=AS_OF)
    at = parse_instant(AS_OF)
    assert out.provenance.as_of == at
    timed = [(dataset, as_of) for dataset, as_of in reads if dataset not in EXEMPT]
    assert timed, f"{tool} read nothing"
    assert {dataset for dataset, _ in reads} <= set(datasets_by_tool()[tool]) | EXEMPT
    assert timed == [(dataset, at) for dataset, _ in timed]
