"""twse_monthly_revenue against TWSE's OpenAPI, MOPS's monthly summary pages and
TWSE's daily market summary (trimmed recordings, fixtures/README). Keyless."""

from __future__ import annotations

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


def test_unknown_tickers_are_refused(tmp_path, upstream, twse_clock):
    err = refused("twse_monthly_revenue", context(tmp_path, now=NOW), tickers=["NVDA"])
    assert err.code == "invalid_arguments" and "TSM=2330" in err.message
    assert upstream.requests == []
