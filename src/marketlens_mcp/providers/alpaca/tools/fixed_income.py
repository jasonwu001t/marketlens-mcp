"""Fixed-income quotes (capability ``market``)."""

from __future__ import annotations

from marketlens_mcp.plugin_api import ToolContext, ToolOutput
from marketlens_schema.market import FixedIncomeQuote

from .. import mappers
from ..client import AlpacaClient
from .common import Inputs, Isins, Skips, collect, latest_t, missing_note, output, request_of, spec
from .marketdata import fetch_once

GOLDEN = "tests/alpaca/test_alpaca_fixed_income_news.py"


class LatestIn(Inputs):
    isins: Isins


async def fixed_income_latest_quotes(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    skips = Skips(FixedIncomeQuote.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("data", "/v1beta1/fixed_income/latest/quotes", {"isins": args.isins})
            out = []
            for isin, rec in (data.get("quotes") or {}).items():
                out.extend(
                    collect(
                        skips,
                        [rec],
                        lambda r, i=isin: mappers.fixed_income_quote(r, isin=i),
                        lambda r, i=isin: i,
                    )
                )
            return out

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        FixedIncomeQuote,
        page.rows,
        route="GET /v1beta1/fixed_income/latest/quotes",
        operation="FixedIncomeLatestQuotes",
        request=request_of(args),
        as_of=latest_t(page.rows),
        page=page,
        notes=missing_note(args.isins, page.rows, "isin"),
        skips=skips,
    )


SPECS = (
    spec(
        name="fixed_income_latest_quotes",
        capability="market",
        title="Latest bond quotes",
        description="The latest best bid and ask per bond by ISIN: prices in percent of par, sizes in USD face "
        "value, yield to maturity and yield to worst as fractions (0.0425 = 4.25 %). A side with no active "
        "quote is null (no_data).",
        readme="Latest bond quotes and yields",
        input_model=LatestIn,
        output_model=FixedIncomeQuote,
        route="GET /v1beta1/fixed_income/latest/quotes (market data API)",
        handler=fixed_income_latest_quotes,
        golden_test=GOLDEN,
        operations=("FixedIncomeLatestQuotes",),
        parity=("get_fixed_income_latest_quotes",),
    ),
)
