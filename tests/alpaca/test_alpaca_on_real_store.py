"""Post-merge checks against the core's real store and offload (marketlens_mcp.testing):
large Alpaca results (market_bars, news_search) come back as stored-result markers
(news marked external_text), small ones (market_snapshots) inline. Skipped until the core lane is merged."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alpaca_harness import TEST_KEYS, load_fixture

testing = pytest.importorskip("marketlens_mcp.testing")


def _bars(n: int) -> list[dict]:
    t0 = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    return [
        {
            "t": (t0 + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            "o": 100.0 + i,
            "h": 101.0 + i,
            "l": 99.0 + i,
            "c": 100.5 + i,
            "v": 1000 + i,
            "n": 10,
            "vw": 100.2 + i,
        }
        for i in range(n)
    ]


def _context(tmp_path, tool: str, capability: str):
    store = testing.temp_store(tmp_path / "store")
    return testing.make_context(tool=tool, store=store, env=TEST_KEYS, capabilities=[capability])


def test_large_bars_are_stored_with_a_marker(tmp_path, specs, alpaca):
    alpaca.add("/v2/stocks/bars", {"bars": {"AAPL": _bars(450)}, "next_page_token": None})
    ctx = _context(tmp_path, "market_bars", "market")
    resp = asyncio.run(testing.call_tool(specs["market_bars"], ctx, tickers=["AAPL"], timeframe="1min"))
    assert resp.kind == "stored"
    assert (resp.model, resp.row_count, resp.tool) == ("marketlens.Bar", 450, "market_bars")
    assert resp.provenance.provider == "alpaca" and resp.provenance.operation == "StockBars"
    assert {c.name for c in resp.columns} >= {"ticker", "t", "close", "volume"}
    assert resp.preview.time_column == "t" and resp.preview.group_column == "ticker"


def _news(n: int) -> list[dict]:
    return [
        {
            "id": 1000 + i,
            "headline": f"Synthetic headline {i}",
            "author": "",
            "created_at": "2026-10-01T09:00:00Z",
            "updated_at": "2026-10-01T09:30:00Z",
            "summary": "Synthetic summary.",
            "content": "",
            "images": [],
            "symbols": ["AAPL"],
            "source": "benzinga",
            "url": None,
        }
        for i in range(n)
    ]


def test_large_news_is_stored_and_marked_external_text(tmp_path, specs, alpaca):
    alpaca.add("/v1beta1/news", {"news": _news(250), "next_page_token": None})
    ctx = _context(tmp_path, "news_search", "news")
    resp = asyncio.run(testing.call_tool(specs["news_search"], ctx))
    assert (resp.kind, resp.model, resp.row_count) == ("stored", "marketlens.NewsItem", 250)
    assert resp.risk == "external_text"


def test_small_snapshots_come_back_inline(tmp_path, specs, alpaca):
    alpaca.add("/v2/stocks/snapshots", load_fixture("StockSnapshots"))
    ctx = _context(tmp_path, "market_snapshots", "market")
    resp = asyncio.run(testing.call_tool(specs["market_snapshots"], ctx, tickers=["AAPL", "BRK-B"]))
    assert resp.kind == "inline" and resp.row_count == 2
    assert [r["ticker"] for r in resp.rows] == ["AAPL", "BRK-B"]
