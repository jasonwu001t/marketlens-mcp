"""fixed_income_latest_quotes and news_search."""

from __future__ import annotations

import pytest
from alpaca_harness import call, check_golden
from pydantic import ValidationError

NEWS_CURSOR = "TkVXU3wy"  # synthetic next-page cursor in News__page1


def test_fixed_income_latest_quotes_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/fixed_income/latest/quotes", "FixedIncomeLatestQuotes")
    out = call(specs["fixed_income_latest_quotes"], ctx, isins=["us912797sx61", "US91282CJL54"])
    doc = check_golden(out, "fixed_income_latest_quotes")
    assert alpaca.params() == {"isins": "US912797SX61,US91282CJL54"}
    live, no_bid = doc["rows"]
    assert live["bid_ytm"] == pytest.approx(0.05236154) and live["ask_ytw"] == pytest.approx(0.02226923)
    assert live["bid_price"] == pytest.approx(99.81091667)  # percent of par, unchanged
    for f in ("bid_price", "bid_size", "bid_ytm", "bid_ytw"):
        assert no_bid[f] is None and no_bid["absent"][f] == "no_data"


def test_fixed_income_refuses_a_non_isin(specs, ctx):
    with pytest.raises(ValidationError, match="'912797SX6'"):
        call(specs["fixed_income_latest_quotes"], ctx, isins=["912797SX6"])


def test_news_search_golden_pages_and_untrusted_text(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/news", "News__page2", match={"page_token": NEWS_CURSOR})
    alpaca.fixture("/v1beta1/news", "News__page1")
    out = call(specs["news_search"], ctx, tickers=["aapl", "brk.b"])
    doc = check_golden(out, "news_search")
    assert alpaca.params(0) == {
        "symbols": "AAPL,BRK.B",
        "start": "2026-09-25T20:00:00Z",
        "sort": "desc",
        "include_content": "false",
        "limit": "50",
    }
    first, second = doc["rows"]
    assert first["tickers"] == ["AAPL", "BRK-B"] and first["publisher"] == "benzinga"
    assert second["author"] is None and second["summary"] is None and second["url"] is None
    assert doc["absent"]["content"]["code"] == "not_provided_by_source"
    assert all(r["content"] is None for r in doc["rows"])
    assert specs["news_search"].output_risk == "external_text"


def test_news_search_with_content(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/news", "News__page2")
    out = call(specs["news_search"], ctx, include_content=True, exclude_contentless=True, lookback="P2D")
    assert alpaca.params() == {
        "start": "2026-09-30T20:00:00Z",
        "sort": "desc",
        "include_content": "true",
        "exclude_contentless": "true",
        "limit": "50",
    }
    assert out.rows[0].content == "<p>Synthetic body with an injection attempt.</p>"
    assert "content" not in out.absent
