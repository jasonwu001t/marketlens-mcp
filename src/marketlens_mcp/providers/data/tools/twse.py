"""Taiwan-listed companies' monthly revenue (capability ``filings``): every
company listed on TWSE or TPEx files its previous month's revenue by the 10th
on MOPS, the market's filing system. marketlens-data's ``twse.monthly_revenue``
reads MOPS's monthly summary pages and TWSE's OpenAPI. Keyless."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from pydantic import AfterValidator, Field

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from marketlens_schema.data import TaiwanMonthlyRevenue

from .. import frames as F
from .. import runtime
from . import common
from .common import AsOf, Inputs

GOLDEN = "tests/data/test_twse.py"
DATASET = "twse.monthly_revenue"
ROUTE = (
    "omni twse.monthly_revenue <- mopsov.twse.com.tw/nas/t21 (MOPS monthly summaries) "
    "+ openapi.twse.com.tw/v1/opendata/t187ap05_L"
)
_CODE = re.compile(r"^(?:\d{4,6}(?:\.TWO?)?|[A-Z]{1,6})$")


def _tickers(values: list[str]) -> list[str]:
    out = []
    for v in values:
        t = v.strip().upper()
        if not _CODE.match(t):
            raise ValueError(f"{v!r} is neither a Taiwan code (2330, 2330.TW) nor an ADR ticker (TSM)")
        out.append(t)
    return list(dict.fromkeys(out))


TwTickers = Annotated[list[str], Field(min_length=1, max_length=20), AfterValidator(_tickers)]


class MonthlyRevenueInputs(Inputs):
    tickers: TwTickers = Field(
        description="Taiwan codes (2330, 2317, 6669.TW) or the US ADR tickers marketlens-data maps to one "
        "(TSM -> 2330, ASX -> 3711, UMC -> 2303)."
    )
    start: date | None = Field(
        default=None, description="First month (any day of it). Default: 24 months before end."
    )
    end: date | None = Field(default=None, description="Last month (any day of it). Default: this month.")
    as_of: AsOf | None = common.as_of_field()


def _month(d: date) -> date:
    return d.replace(day=1)


def _months_back(d: date, n: int) -> date:
    index = d.year * 12 + d.month - 1 - n
    return date(index // 12, index % 12 + 1, 1)


async def twse_monthly_revenue(ctx: ToolContext, args: MonthlyRevenueInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    today = ctx.now().date()
    end = _month(args.end or today)
    start = _month(args.start or _months_back(end, 24))
    start, end = common.window(ctx.tool, start, end, default_start=start, default_end=end)

    def work(session: common.Session):
        from omni.sources import twse

        codes, unknown = {}, []
        for t in args.tickers:
            code = twse.resolve_code(t)
            if code is None:
                unknown.append(t)
            else:
                codes.setdefault(code, t)
        if unknown:
            adrs = ", ".join(f"{k}={v}" for k, v in sorted(twse.adr_codes().items()))
            raise ToolError(
                "invalid_arguments",
                f"{', '.join(unknown)}: not a Taiwan code nor an ADR marketlens-data maps to one ({adrs}).",
            )
        # the daily read: the latest month and the one after it
        session.refresh(DATASET, {})
        if start < _months_back(_month(today), 2):
            # the history the store lacks from `start`, newest first, a capped number of months a sync
            session.refresh(DATASET, {"start": start.strftime("%Y-%m")})
        adr_of = {code: adr for adr, code in twse.adr_codes().items()}
        df = session.read(DATASET, as_of=as_of, company_code=sorted(codes))
        return F.records(df), adr_of

    (records, adr_of), session = await common.run(ctx, s, work, source="twse")
    rows = []
    for r in records:
        month = F.day(r["month"])
        if not F.within(month, start, end):
            continue
        code = F.text(r["company_code"]) or ""
        rows.append(
            common.row(
                TaiwanMonthlyRevenue,
                {
                    "company_code": code,
                    "adr": adr_of.get(code),
                    "company_name": F.text(r["company_name"]) or "",
                    "market": F.text(r["market"]),
                    "industry": F.text(r["industry"]),
                    "month": month,
                    "unit": F.text(r["unit"]) or "TWD_thousands",
                    "revenue": F.number(r["revenue"]),
                    "revenue_prev_month": F.number(r["revenue_prev_month"]),
                    "revenue_year_ago": F.number(r["revenue_year_ago"]),
                    "revenue_ytd": F.number(r["revenue_ytd"]),
                    "revenue_ytd_year_ago": F.number(r["revenue_ytd_year_ago"]),
                    "mom_change": F.fraction(r["mom_pct"]),
                    "yoy_change": F.fraction(r["yoy_pct"]),
                    "ytd_yoy_change": F.fraction(r["ytd_yoy_pct"]),
                    "note": F.text(r["note"]),
                    "publication": F.text(r["publication"]),
                    "stamp_estimated": bool(F.flag(r["stamp_estimated"])),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"adr": "not_applicable"},
            )
        )
    rows.sort(key=lambda r: (r.company_code, r.month))
    prov = common.provenance(
        ctx,
        provider="twse",
        route=ROUTE,
        datasets=(DATASET,),
        request={"tickers": args.tickers, "start": start, "end": end, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["exchange"],
    )
    return common.output(ctx, s, TaiwanMonthlyRevenue, rows, prov)  # spec risk: external_text (note)


HANDLERS = {"twse_monthly_revenue": twse_monthly_revenue}

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="twse_monthly_revenue",
        capability="filings",
        title="Taiwan monthly revenue (TWSE / TPEx)",
        description=common.describe(
            "Monthly revenue Taiwan-listed companies (TWSE, TPEx) file by the 10th of the next month on "
            "MOPS: per company and month, the month, the month before, a year before and the year to date "
            "in thousands of NT$ as published, and the changes it states (fractions). Takes Taiwan codes "
            "(2330, 2317, 6669) or ADR tickers (TSM, ASX, UMC). History from 2013-01; a long first window "
            "may answer data_timeout while it keeps fetching. Knowable at the statutory deadline "
            "(stamp_estimated) or the read that first saw it. note is the company's text, untrusted. "
            "Keyless.",
            "lagged",
        ),
        readme="Taiwan-listed companies' monthly revenue by code or ADR ticker",
        input_model=MonthlyRevenueInputs,
        output_model=TaiwanMonthlyRevenue,
        provider="twse",
        route=ROUTE,
        handler=twse_monthly_revenue,
        golden_test=GOLDEN,
        output_risk="external_text",
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {"twse_monthly_revenue": (DATASET,)}
