"""stdout carries the MCP protocol over stdio: no data tool (nor
marketlens-data under it) may print. Every tool runs on fixtures here."""

from __future__ import annotations

import logging

from data_harness import SPECS, call, context
from test_calendars import earnings_routes, economic_routes, holiday_routes, surprise_routes
from test_fed_treasury import FISCAL, fomc_routes, treasury_routes
from test_macro import BEA, bls_routes, fred_routes
from test_sec import sec_routes

ARGS = {
    "macro_series": {"series_ids": ["CPIAUCSL"]},
    "macro_bls_series": {"series_ids": ["LNS14000000"]},
    "macro_bea_table": {"table_name": "T10101"},
    "sec_filings": {"ticker": "TESTCO"},
    "sec_xbrl_facts": {"ticker": "TESTCO"},
    "sec_fundamentals": {"ticker": "TESTCO"},
    "sec_earnings_releases": {"ticker": "TESTCO"},
    "sec_earnings_figures": {"ticker": "TESTCO"},
    "sec_insider_trades": {"ticker": "TESTCO"},
    "sec_13f_holdings": {"filer_cik": "9999950"},
    "sec_fund_nport": {"ticker": "TFUND"},
    "sec_fund_holdings": {"ticker": "TFUND"},
    "calendar_economic": {"start": "2026-10-01", "end": "2026-10-02"},
    "calendar_earnings": {"start": "2026-10-01", "end": "2026-10-02"},
    "calendar_earnings_history": {"tickers": ["TESTCO"]},
}


def test_no_tool_writes_to_stdout(tmp_path, upstream, keys, frozen_clock, capsys):
    fred_routes(upstream)
    bls_routes(upstream)
    upstream.json_file(BEA, "bea.nipa__T10101.json")
    upstream.text_file(r"www\.bls\.gov/schedule/news_release/cpi\.htm", "bls.cpi_schedule.html")
    upstream.text_file(r"www\.bls\.gov/schedule/news_release/empsit\.htm", "bls.empsit_schedule.html")
    sec_routes(upstream)
    fomc_routes(upstream)
    upstream.json_file(r"markets\.newyorkfed\.org/api/rates/all/search\.json", "nyfed.reference_rates.json")
    treasury_routes(upstream)
    upstream.json_file(rf"{FISCAL}/v1/accounting/od/auctions_query", "fiscaldata.auctions.json")
    upstream.json_file(rf"{FISCAL}/v2/accounting/od/debt_to_penny", "fiscaldata.debt_to_penny.json")
    upstream.json_file(rf"{FISCAL}/v1/accounting/dts/operating_cash_balance", "fiscaldata.tga_balance.json")
    economic_routes(upstream)
    earnings_routes(upstream)
    surprise_routes(upstream)
    holiday_routes(upstream)
    capsys.readouterr()
    answered = 0
    for name in SPECS:
        out = call(name, context(tmp_path), **ARGS.get(name, {}))
        answered += bool(out.rows)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert answered == 25


def test_the_placeholder_contact_warning_never_reaches_stdout(
    tmp_path, upstream, monkeypatch, capsys, caplog, recwarn
):
    # Without SEC_CONTACT_EMAIL marketlens-data warns (once per process) that it sends a
    # placeholder contact: to logging or stderr, never to stdout.
    from omni.sources import sec as omni_sec

    if hasattr(omni_sec, "_placeholder_warned"):
        monkeypatch.setattr(omni_sec, "_placeholder_warned", False)
    sec_routes(upstream)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        out = call("sec_filings", context(tmp_path), ticker="TESTCO")
    captured = capsys.readouterr()
    assert out.rows and captured.out == ""
    said = caplog.text + captured.err + " ".join(str(w.message) for w in recwarn)
    assert "SEC_CONTACT_EMAIL" in said
