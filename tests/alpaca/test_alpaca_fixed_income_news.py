"""fixed_income_latest_quotes and news_search."""

from __future__ import annotations

import httpx
import pytest
from alpaca_harness import FakeContext, call, check_golden
from pydantic import ValidationError

from marketlens_mcp.plugin_api import FetchLimits, ToolError

NEWS_CURSOR = "TkVXU3wy"  # synthetic next-page cursor in News__page1


def test_fixed_income_latest_quotes_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/fixed_income/latest/quotes", "FixedIncomeLatestQuotes")
    out = call(specs["fixed_income_latest_quotes"], ctx, isins=["us912797sx61", "US91282CJL55"])
    doc = check_golden(out, "fixed_income_latest_quotes")
    assert alpaca.params() == {"isins": "US912797SX61,US91282CJL55"}
    live, no_bid = doc["rows"]
    assert live["bid_ytm"] == pytest.approx(0.05236154) and live["ask_ytw"] == pytest.approx(0.02226923)
    assert live["bid_price"] == pytest.approx(99.81091667)  # percent of par, unchanged
    for f in ("bid_price", "bid_size", "bid_ytm", "bid_ytw"):
        assert no_bid[f] is None and no_bid["absent"][f] == "no_data"


def test_fixed_income_says_at_registration_which_plans_have_it(specs, alpaca):
    # Registration does no I/O (FakePluginContext.env refuses), so the description carries the entitlement.
    spec = specs["fixed_income_latest_quotes"]
    assert "Needs an Alpaca market data plan that includes fixed income" in spec.description
    assert "alpaca_not_entitled" in spec.description and "needs a plan with fixed income" in spec.readme
    assert alpaca.requests == []


def test_fixed_income_refuses_a_non_isin(specs, ctx):
    with pytest.raises(ValidationError, match="'912797SX6'"):
        call(specs["fixed_income_latest_quotes"], ctx, isins=["912797SX6"])


def test_fixed_income_refuses_a_wrong_check_digit_before_asking_alpaca(specs, ctx, alpaca):
    with pytest.raises(
        ValidationError, match="'US91282CMM01' is not a valid ISIN: its check digit is 1, the code needs 0"
    ):
        call(specs["fixed_income_latest_quotes"], ctx, isins=["US91282CMM01"])
    assert alpaca.requests == []


@pytest.mark.parametrize(
    "isin", ["US0378331005", "AU0000XVGZA3", "GB0002634946", "US38259P5089", "US912797SX61"]
)
def test_isin_check_digit_of_known_isins(isin):
    from marketlens_mcp.providers.alpaca.tools.common import isin_check_digit

    assert isin_check_digit(isin[:11]) == int(isin[11])


def test_news_search_golden_pages_and_untrusted_text(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/news", "News__page2", match={"page_token": NEWS_CURSOR})
    alpaca.fixture("/v1beta1/news", "News__page1")
    out = call(specs["news_search"], ctx, tickers=["aapl", "brk.b"])
    doc = check_golden(out, "news_search")
    assert alpaca.params(0) == {
        "symbols": "AAPL,BRK.B,BRK",  # a share class is found through its root
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


def test_news_search_date_end_includes_that_utc_day(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/news", "News__page2")
    out = call(specs["news_search"], ctx, start="2026-09-28", end="2026-10-01")
    assert alpaca.params()["end"] == "2026-10-01T23:59:59.999999Z"
    assert out.notes[0].startswith(
        "end 2026-10-01 is a date: the window includes that whole day, to midnight UTC"
    )


def test_news_lookback_doc_says_nothing_of_bars(specs):
    for name in (
        "news_search",
        "crypto_quotes",
        "crypto_trades",
        "market_quotes",
        "market_trades",
        "options_trades",
    ):
        doc = specs[name].input_model.model_fields["lookback"].description
        assert "Bars" not in doc, name
    for name in ("market_bars", "crypto_bars", "options_bars"):
        assert "Bars of 1d or longer" in specs[name].input_model.model_fields["lookback"].description, name


def _article(i: int, symbols: list[str], headline: str = "Synthetic headline", summary: str = "") -> dict:
    return {
        "id": i,
        "headline": headline,
        "summary": summary,
        "author": "A. Writer",
        "created_at": "2026-10-02T11:08:42Z",
        "updated_at": "2026-10-02T11:08:43Z",
        "symbols": symbols,
        "source": "benzinga",
        "url": f"https://www.example.com/news/{i}",
    }


def _news_index(*articles: dict):
    """Alpaca's news symbols filter as it behaves live (checked 2026-10-09): an
    article is found by a plain tag it carries (BRK, AAPL), never by a share
    class's form: symbols=BRK.B, BRK/B or BRK-B find nothing, although articles
    are tagged BRK/B (and also carry the root BRK)."""

    def answer(request: httpx.Request) -> httpx.Response:
        asked = set(request.url.params.get("symbols", "").split(","))
        found = [a for a in articles if any(s in asked and s.isalnum() for s in a["symbols"])]
        return httpx.Response(200, json={"news": found, "next_page_token": None})

    return answer


def test_news_finds_a_share_class_through_its_root(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/news",
        _news_index(
            _article(1, ["BRK", "BRK/A", "BRK/B", "KO"]),
            _article(2, ["BRK", "SPY", "XLF"]),  # a sector round-up: the company, not the class
            _article(3, ["BRK", "BRK/A"]),
            _article(4, ["AAPL"]),
        ),
    )
    out = call(specs["news_search"], ctx, tickers=["BRK-B"], lookback="P30D")
    assert alpaca.params()["symbols"] == "BRK.B,BRK"
    assert [r.news_id for r in out.rows] == ["1"]
    assert out.rows[0].tickers == ["BRK", "BRK-A", "BRK-B", "KO"]  # BRK/A and BRK/B are tickers
    assert out.notes == [
        "Alpaca's news finds a share class only through its root, so BRK was asked for too: 2 article(s) "
        "tagged BRK but not BRK-B were left out (ask for BRK to see them)."
    ]


def test_news_keeps_a_root_article_tagged_with_another_requested_ticker(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/news",
        _news_index(_article(1, ["BRK", "AAPL"]), _article(2, ["AAPL"]), _article(3, ["BRK"])),
    )
    out = call(specs["news_search"], ctx, tickers=["BRK-B", "AAPL"], lookback="P30D")
    assert alpaca.params()["symbols"] == "BRK.B,AAPL,BRK"
    assert [r.news_id for r in out.rows] == ["1", "2"]
    assert out.notes == [
        "No article in the window is tagged BRK-B.",
        "Alpaca's news finds a share class only through its root, so BRK was asked for too: 1 article(s) "
        "tagged BRK but not BRK-B were left out (ask for BRK to see them).",
    ]


def test_news_description_says_a_root_article_with_another_requested_ticker_is_kept(specs):
    assert "only those tagged BRK-B or another requested ticker are kept" in specs["news_search"].description


def test_news_with_the_root_asked_for_keeps_all_its_articles(specs, ctx, alpaca):
    alpaca.add("/v1beta1/news", _news_index(_article(1, ["BRK", "BRK/B"]), _article(2, ["BRK"])))
    out = call(specs["news_search"], ctx, tickers=["BRK", "BRK-B"], lookback="P30D")
    assert alpaca.params()["symbols"] == "BRK,BRK.B"
    assert [r.news_id for r in out.rows] == ["1", "2"] and out.notes == []


def test_news_says_nothing_of_a_share_class_when_the_window_was_not_read_to_the_end(specs, alpaca):
    alpaca.add(
        "/v1beta1/news",
        {"news": [_article(1, ["BRK"]), _article(2, ["BRK"])], "next_page_token": "P2"},
    )
    out = call(
        specs["news_search"],
        FakeContext(limits=FetchLimits(max_rows=50_000, max_pages=1)),
        tickers=["BRK-B"],
        lookback="P30D",
    )
    assert out.rows == [] and not any("No article" in n for n in out.notes)


def test_news_query_keeps_articles_with_every_word_in_headline_or_summary(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/news",
        {
            "news": [
                _article(1, ["SPY"], "Fed signals a rate cut in December"),
                _article(2, ["SPY"], "Markets rally", "Traders price in another Rate Cut."),
                _article(
                    3, ["SPY"], "Corporate earnings beat", "Rates steady; no cut."
                ),  # "rate" is not "Rates"
                _article(4, ["SPY"], "Rate decision ahead"),  # no "cut"
            ],
            "next_page_token": None,
        },
    )
    out = call(specs["news_search"], ctx, query="  rate   CUT ", lookback="P1D")
    assert [r.news_id for r in out.rows] == ["1", "2"]
    assert "query" not in alpaca.params()  # Alpaca has no text search: filtered here
    assert out.provenance.request["query"] == "rate CUT"
    assert out.notes == [
        "query 'rate CUT': 2 of the 4 article(s) read had every word in their headline or summary "
        "(filtered by this server; Alpaca has no text search)."
    ]


def test_news_query_pages_on_past_articles_it_filters_out(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/news",
        {"news": [_article(3, ["SPY"], "Other news")], "next_page_token": None},
        match={"page_token": "P2"},
    )
    alpaca.add(
        "/v1beta1/news",
        {
            "news": [_article(1, ["SPY"], "Tariff ruling"), _article(2, ["SPY"], "Other")],
            "next_page_token": "P2",
        },
    )
    out = call(specs["news_search"], ctx, query="tariff", lookback="P1D")
    assert [r.news_id for r in out.rows] == ["1"] and len(alpaca.requests) == 2
    assert out.notes[-1].startswith("query 'tariff': 1 of the 3 article(s)")


def test_news_query_says_a_cut_fetch_did_not_reach_the_end_of_the_window(specs, alpaca):
    # Without tickers a day holds several hundred articles: fetch.max_pages pages of 50 can end early.
    alpaca.add(
        "/v1beta1/news",
        {"news": [_article(1, ["SPY"], "Other"), _article(2, ["SPY"], "Rate cut")], "next_page_token": "P2"},
    )
    ctx = FakeContext(limits=FetchLimits(max_rows=50_000, max_pages=1))
    out = call(specs["news_search"], ctx, query="tariff", lookback="PT1H")
    assert out.rows == [] and out.pagination.next_page_token == "P2"
    assert out.notes[-1] == (
        "query 'tariff': 0 of the 2 article(s) read had every word in their headline or summary (filtered by this "
        "server; Alpaca has no text search). The fetch stopped before the end of the window: narrow it with "
        "tickers or a shorter window."
    )


def test_news_query_without_tickers_refuses_a_window_one_call_cannot_read(specs, ctx, alpaca):
    """2026-10-20: "Bridgewater" over the default P7D (about 5,950 articles)
    read the newest 1,000 and reported no match, three times."""
    with pytest.raises(ToolError) as e:
        call(specs["news_search"], ctx, query="Bridgewater")
    assert e.value.code == "invalid_arguments"
    assert e.value.message == (
        "query without tickers filters every article Alpaca has in the window, about 850 a day, and one call "
        "reads at most 1,000 (20 page(s) of 50, fetch.max_pages): the window from 2026-09-25T20:00:00Z to "
        "2026-10-02T20:00:00Z holds about 5,950, so the fetch would stop before its end. Nothing was fetched."
    )
    assert e.value.hint == 'Give tickers, or a window of at most 28 hours (lookback="PT28H").'
    assert alpaca.requests == []


def test_the_refusal_names_filings_search_when_filings_is_on(specs, alpaca):
    ctx = FakeContext(capabilities=frozenset({"news", "filings"}))
    with pytest.raises(ToolError) as e:
        call(specs["news_search"], ctx, query="Bridgewater")
    assert e.value.hint == (
        'Give tickers, or a window of at most 28 hours (lookback="PT28H"); to find a company, fund or person in '
        "SEC filings, use filings_search (SEC EDGAR full-text search)."
    )
    assert alpaca.requests == []


def test_a_query_without_tickers_reads_a_window_one_call_can_read(specs, ctx, alpaca):
    alpaca.add("/v1beta1/news", {"news": [], "next_page_token": None})
    call(specs["news_search"], ctx, query="Bridgewater", lookback="PT28H")  # about 992 of 1,000
    assert len(alpaca.requests) == 1
    with pytest.raises(ToolError) as e:
        call(specs["news_search"], ctx, query="Bridgewater", lookback="PT29H")  # about 1,027
    assert "holds about 1,027" in e.value.message and len(alpaca.requests) == 1


def test_tickers_or_a_page_token_lift_the_refusal(specs, ctx, alpaca):
    alpaca.add("/v1beta1/news", {"news": [], "next_page_token": None})
    call(specs["news_search"], ctx, query="Bridgewater", tickers=["SPY"])
    call(specs["news_search"], ctx, query="Bridgewater", page_token="P2")
    assert len(alpaca.requests) == 2


def test_news_query_says_to_give_tickers_or_a_short_window(specs):
    doc = specs["news_search"].input_model.model_fields["query"].description
    assert "give tickers or a window of a day or less" in doc


@pytest.mark.parametrize(
    ("query", "found"), [("Wednesday's", True), ("S&P 500", True), ("39", False), ("amp", False)]
)
def test_news_query_reads_the_text_with_its_html_entities_decoded(specs, ctx, alpaca, query, found):
    # As Alpaca sends summaries live: "Wednesday&#39;s regular session", "S&amp;P 500".
    summary = "Two of 11 sectors are higher in Wednesday&#39;s regular session; the S&amp;P 500 slips."
    alpaca.add("/v1beta1/news", {"news": [_article(1, ["SPY"], "Sectors", summary)], "next_page_token": None})
    out = call(specs["news_search"], ctx, query=query, lookback="P1D")
    assert (len(out.rows) == 1) is found


@pytest.mark.parametrize("query", ["", "  ", "!!"])
def test_news_query_needs_a_word(specs, ctx, query):
    with pytest.raises(ValidationError):
        call(specs["news_search"], ctx, query=query)


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
