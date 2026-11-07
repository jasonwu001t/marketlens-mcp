"""twse_monthly_revenue against TWSE's OpenAPI, MOPS's monthly summary pages and
TWSE's daily market summary (trimmed recordings, fixtures/README); twse_taiex
against a synthetic TAIEX OpenAPI file (fixtures/twse.taiex_openapi.json) and
monthly history pages built here in TWSE's layout, with the header row taken
from marketlens-data's parser. Keyless."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import pytest
from data_harness import FIXTURES, call, check_golden, context, refused

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
MOPS = r"mopsov\.twse\.com\.tw/nas/t21/(sii|otc)/t21sc03_\d+_\d+_[01]\.html"
SII_SEP = (FIXTURES / "twse.mops__sii_115_9_0.html").read_bytes()
NO_DATA = (FIXTURES / "twse.mops__no_data.html").read_bytes()  # sii, 115年10月


def _no_data(market: str, roc: int, month: int) -> bytes:
    title = f"{'上市' if market == 'sii' else '上櫃'}公司{roc}年{month}月份".encode("cp950")
    return NO_DATA.replace("上市公司115年10月份".encode("cp950"), title)


def _summary(market: str, roc: int, month: int) -> bytes:
    """Any month's summary page: September's retitled, an otc page under codes of its own."""
    title = f"{'上市' if market == 'sii' else '上櫃'}公司{roc}年{month}月份".encode("cp950")
    body = SII_SEP.replace("上市公司115年9月份".encode("cp950"), title)
    return (
        re.sub(rb"<td align=center>(\d{4})</td>", rb"<td align=center>9\1</td>", body)
        if market == "otc"
        else body
    )


def history_pages(first: tuple[int, int], last: tuple[int, int] = (115, 9)) -> dict:
    """Page 0 of both markets for every month from `first` to `last` (ROC year, month)."""
    pages, (roc, month) = {}, first
    while (roc, month) <= last:
        for market in ("sii", "otc"):
            pages[(market, roc, month, "0")] = _summary(market, roc, month)
        roc, month = (roc + 1, 1) if month == 12 else (roc, month + 1)
    return pages


def _months(first: str, last: str) -> list[str]:
    (y, m), (y2, m2) = (map(int, first.split("-")), map(int, last.split("-")))
    out = []
    while (y, m) <= (y2, m2):
        out.append(f"{y}-{m:02d}-01")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def twse_routes(upstream, *, pages: dict | None = None):
    pages = {("sii", 115, 9, "0"): SII_SEP} if pages is None else pages

    def mops(request: httpx.Request):
        market, name = request.url.path.split("/")[-2:]
        roc, month, page = name[len("t21sc03_") : -len(".html")].split("_")
        key = (market, int(roc), int(month), page)
        if key in pages:
            return pages[key]
        if (int(roc), int(month)) in ((115, 9), (115, 10)):
            return _no_data(market, int(roc), int(month))
        return httpx.Response(404)

    def calendar(request: httpx.Request):
        if request.url.params["date"] == "20261001":
            return (FIXTURES / "twse.fmtqik__202610.json").read_bytes()
        return {"stat": "OK", "data": []}

    upstream.add(
        r"openapi\.twse\.com\.tw/v1/opendata/t187ap05_L",
        lambda _r: (FIXTURES / "twse.t187ap05_L.json").read_bytes(),
    )
    upstream.add(MOPS, mops)
    upstream.add(r"www\.twse\.com\.tw/rwd/zh/afterTrading/FMTQIK", calendar)
    taiex_routes(upstream)


# --- the TAIEX ----------------------------------------------------------------------------------

TAIEX_OPENAPI = r"openapi\.twse\.com\.tw/v1/indicesReport/MI_5MINS_HIST"
TAIEX_HISTORY = r"www\.twse\.com\.tw/rwd/zh/TAIEX/MI_5MINS_HIST"


def taiex_month(year: int, month: int) -> dict:
    """TWSE's history page of one month in its layout: every weekday a session, synthetic levels
    (40,000 + 100 x month + day), ROC dates and thousands separators."""
    from calendar import monthrange
    from datetime import date

    from omni.sources import _twse_taiex

    rows = []
    for day in range(1, monthrange(year, month)[1] + 1):
        if date(year, month, day).weekday() >= 5:
            continue
        base = 40000 + 100 * month + day
        cells = [f"{base + d:,.2f}" for d in (0.25, 60.5, -40.75, 20.0)]
        rows.append([f"{year - 1911}/{month:02d}/{day:02d}", *cells])
    return {"stat": "OK", "title": "synthetic", "fields": list(_twse_taiex._FIELDS), "data": rows}


def taiex_routes(upstream, *, gone: tuple[str, ...] = ()):
    """The OpenAPI's October 2026 (two sessions) and every month's history page up to September
    2026; a month in `gone` (YYYYMM) answers 404."""
    from omni.sources import _twse_taiex

    def history(request: httpx.Request):
        asked = request.url.params["date"]
        if asked[:6] in gone:
            return httpx.Response(404)
        year, month = int(asked[:4]), int(asked[4:6])
        if (year, month) > (2026, 9):
            return {"stat": _twse_taiex._NO_ROWS[0]}
        return taiex_month(year, month)

    upstream.add(TAIEX_OPENAPI, lambda _r: (FIXTURES / "twse.taiex_openapi.json").read_bytes())
    upstream.add(TAIEX_HISTORY, history)


@pytest.fixture
def held_at_one_row(monkeypatch):
    from omni.sources import twse

    monkeypatch.setattr(twse, "_HELD_ROWS", 1)  # a trimmed page lists 9 companies, a real month 800+


@pytest.fixture
def twse_clock(monkeypatch):
    import importlib

    from omni.sources import twse

    router = importlib.import_module("omni.query.router")
    monkeypatch.setattr(twse, "_now", lambda: NOW)
    monkeypatch.setattr(router, "utcnow", lambda: NOW)


def test_twse_monthly_revenue_golden(tmp_path, upstream, twse_clock):
    twse_routes(upstream)
    out = call(
        "twse_monthly_revenue",
        context(tmp_path, now=NOW),
        tickers=["TSM", "2317", "6669.TW"],
        start="2026-09-01",
    )
    doc = check_golden(out, "twse_monthly_revenue")
    assert [(r["company_code"], r["adr"], r["month"]) for r in doc["rows"]] == [
        ("2317", None, "2026-09-01"),
        ("2330", "TSM", "2026-09-01"),
        ("6669", None, "2026-09-01"),
    ]
    tsmc = doc["rows"][1]
    assert (tsmc["revenue"], tsmc["revenue_year_ago"], tsmc["unit"]) == (
        511857399.0,
        330980920.0,
        "TWD_thousands",
    )
    assert tsmc["yoy_change"] == pytest.approx(0.5464) and tsmc["mom_change"] == pytest.approx(-0.0057)
    assert (tsmc["market"], tsmc["industry"], tsmc["company_name"]) == ("sii", "半導體業", "台積電")
    # read after its deadline (2026-10-12, the first trading day from the 10th): stamped there
    assert (tsmc["knowledge_time"], tsmc["stamp_estimated"], tsmc["publication"]) == (
        "2026-10-12T15:59:59Z",
        True,
        "mops_summary",
    )
    assert doc["rows"][0]["absent"] == {"adr": "not_applicable", "note": "no_data"}
    assert doc["provenance"]["authority"] == ["exchange"]
    hosts = upstream.hosts()
    assert hosts[0] == "openapi.twse.com.tw" and hosts.count("mopsov.twse.com.tw") == 8


def test_as_of_before_the_deadline_sees_nothing(tmp_path, upstream, twse_clock):
    twse_routes(upstream)
    ctx = context(tmp_path, now=NOW)
    assert (
        call(
            "twse_monthly_revenue", ctx, tickers=["2330"], start="2026-09-01", as_of="2026-10-12T12:00:00Z"
        ).rows
        == []
    )
    rows = call("twse_monthly_revenue", ctx, tickers=["2330"], start="2026-09-01").rows
    assert [r.month.isoformat() for r in rows] == ["2026-09-01"]


def test_an_older_window_reads_the_history_the_store_lacks(tmp_path, upstream, twse_clock):
    twse_routes(upstream)
    call("twse_monthly_revenue", context(tmp_path, now=NOW), tickers=["2330"], start="2026-06-01")
    asked = {r.url.path.rsplit("/", 1)[-1] for r in upstream.requests if r.url.host == "mopsov.twse.com.tw"}
    assert {"t21sc03_115_6_0.html", "t21sc03_115_7_0.html", "t21sc03_115_8_0.html"} <= asked


def test_a_window_from_two_months_back_reads_that_month(tmp_path, upstream, twse_clock):
    # the daily read takes the OpenAPI's month (2026-09) and the next; 2026-08, two months before
    # October, is the history job's first month, so a window starting there must run it
    twse_routes(upstream)
    call("twse_monthly_revenue", context(tmp_path, now=NOW), tickers=["2330"], start="2026-08-01")
    asked = {r.url.path.rsplit("/", 1)[-1] for r in upstream.requests if r.url.host == "mopsov.twse.com.tw"}
    assert "t21sc03_115_8_0.html" in asked


def test_a_long_window_is_read_whole(tmp_path, upstream, twse_clock, held_at_one_row):
    # a sync reads at most 24 months of history: 2023-01..2026-08 takes two
    twse_routes(upstream, pages=history_pages((112, 1)))
    out = call("twse_monthly_revenue", context(tmp_path, now=NOW), tickers=["2330"], start="2023-01-01")
    assert [r.month.isoformat() for r in out.rows] == _months("2023-01", "2026-09")
    assert out.notes == [] and not out.provenance.truncated


def test_a_call_out_of_time_names_the_months_it_lacks(
    tmp_path, upstream, twse_clock, held_at_one_row, monkeypatch
):
    import itertools
    import types

    from marketlens_mcp.providers.data.tools import twse as tool

    twse_routes(upstream, pages=history_pages((112, 1)))
    ctx = context(tmp_path, now=NOW)
    clock = tool.time
    ticks = itertools.count(0, 1000)  # each reading of the clock 1,000 s later: one sync, then out of time
    monkeypatch.setattr(tool, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    out = call("twse_monthly_revenue", ctx, tickers=["2330"], start="2023-01-01")
    assert [r.month.isoformat() for r in out.rows] == _months("2024-09", "2026-09")
    assert out.provenance.truncated and out.notes == [out.provenance.truncation_note]
    assert out.notes[0].startswith(
        "The store holds no full MOPS summary of 20 month(s) of the window (2023-01 to 2024-08)"
    )
    monkeypatch.setattr(tool, "time", clock)
    again = call("twse_monthly_revenue", ctx, tickers=["2330"], start="2023-01-01")
    assert [r.month.isoformat() for r in again.rows] == _months("2023-01", "2026-09") and again.notes == []


def test_a_past_window_reads_its_own_months(tmp_path, upstream, twse_clock, held_at_one_row):
    # the history is read newest first, 24 months a sync: 2021 lies three syncs back
    twse_routes(upstream, pages=history_pages((110, 1)))
    out = call(
        "twse_monthly_revenue",
        context(tmp_path, now=NOW),
        tickers=["2330"],
        start="2021-01-01",
        end="2021-06-01",
    )
    assert [r.month.isoformat() for r in out.rows] == _months("2021-01", "2021-06")


def test_months_the_store_still_lacks_are_named(tmp_path, upstream, twse_clock, held_at_one_row):
    pages = history_pages((114, 1))
    twse_routes(upstream, pages={k: v for k, v in pages.items() if k[1:3] != (114, 3)})  # MOPS: 404
    ctx = context(tmp_path, settings={"ttl_hours": 100_000}, now=NOW)
    out = call("twse_monthly_revenue", ctx, tickers=["2330"], start="2025-01-01")
    assert [r.month.isoformat() for r in out.rows] == [
        m for m in _months("2025-01", "2026-09") if m != "2025-03-01"
    ]
    assert out.provenance.truncated and out.notes == [out.provenance.truncation_note]
    assert "1 month(s) of the window (2025-03)" in out.notes[0]
    # MOPS serves it later: the next call, though its sync is fresh, reads it
    twse_routes(upstream, pages=pages)
    again = call("twse_monthly_revenue", ctx, tickers=["2330"], start="2025-01-01")
    assert [r.month.isoformat() for r in again.rows] == _months("2025-01", "2026-09")
    assert again.notes == [] and not again.provenance.truncated


def test_unknown_tickers_are_refused(tmp_path, upstream, twse_clock):
    err = refused("twse_monthly_revenue", context(tmp_path, now=NOW), tickers=["NVDA"])
    assert err.code == "invalid_arguments" and "TSM=2330" in err.message
    assert upstream.requests == []


def _history_months(upstream) -> list[str]:
    return [r.url.params["date"] for r in upstream.requests if r.url.path.endswith("TAIEX/MI_5MINS_HIST")]


def test_twse_taiex_golden(tmp_path, upstream, frozen_clock):
    twse_routes(upstream)
    out = call("twse_taiex", context(tmp_path), start="2026-09-28")
    doc = check_golden(out, "twse_taiex")
    assert [(r["date"], r["close"], r["publication"]) for r in doc["rows"]] == [
        ("2026-09-28", 40948.0, "twse_history"),
        ("2026-09-29", 40949.0, "twse_history"),
        ("2026-09-30", 40950.0, "twse_history"),
        ("2026-10-01", 41355.8, "twse_openapi"),
        ("2026-10-02", 41077.43, "twse_openapi"),
    ]
    last = doc["rows"][-1]
    assert (last["index"], last["unit"], last["open"], last["high"], last["low"]) == (
        "TAIEX",
        "index_points",
        41360.12,
        41420.66,
        41015.09,
    )
    # knowable at 14:00 Taipei of the session's day
    assert last["knowledge_time"] == "2026-10-02T06:00:00Z"
    assert doc["provenance"]["authority"] == ["exchange"] and doc["notes"] == []
    # the daily read: the OpenAPI's month and the month before's history page, nothing older
    assert _history_months(upstream) == ["20260901"]


def test_twse_taiex_as_of_before_the_close_hides_the_session(tmp_path, upstream, frozen_clock):
    twse_routes(upstream)
    ctx = context(tmp_path)
    out = call("twse_taiex", ctx, start="2026-10-01", as_of="2026-10-02T05:59:00Z")
    assert [r.date.isoformat() for r in out.rows] == ["2026-10-01"]


def test_twse_taiex_an_older_window_reads_the_history(tmp_path, upstream, frozen_clock):
    twse_routes(upstream)
    out = call("twse_taiex", context(tmp_path), start="2026-06-01", end="2026-08-31")
    assert {r.date.strftime("%Y-%m") for r in out.rows} == {"2026-06", "2026-07", "2026-08"}
    assert out.rows[0].date.isoformat() == "2026-06-01" and out.notes == []
    assert set(_history_months(upstream)) == {"20260601", "20260701", "20260801", "20260901"}


def test_twse_taiex_names_the_months_it_lacks(tmp_path, upstream, frozen_clock):
    twse_routes(upstream)
    taiex_routes(upstream, gone=("202607",))  # the latest route wins
    out = call("twse_taiex", context(tmp_path), start="2026-06-01", end="2026-08-31")
    assert {r.date.strftime("%Y-%m") for r in out.rows} == {"2026-06", "2026-08"}
    assert out.provenance.truncated and out.notes == [out.provenance.truncation_note]
    assert "1 month(s) of the window (2026-07)" in out.notes[0]
