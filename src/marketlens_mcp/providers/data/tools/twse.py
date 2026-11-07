"""The Taiwan Stock Exchange: Taiwan-listed companies' monthly revenue
(capability ``filings``): every company listed on TWSE or TPEx files its
previous month's revenue by the 10th on MOPS, the market's filing system;
marketlens-data's ``twse.monthly_revenue`` reads MOPS's monthly summary pages
and TWSE's OpenAPI. And the TAIEX, TWSE's capitalization-weighted index, per
session (capability ``macro``, beside the other index levels from official
sources: FRED's SP500 and NASDAQ100 are macro_series), from marketlens-data's
``twse.taiex``. Keyless."""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Annotated

from pydantic import AfterValidator, Field

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from marketlens_schema.data import IndexLevel, TaiwanMonthlyRevenue

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
TAIEX = "twse.taiex"
TAIEX_ROUTE = (
    "omni twse.taiex <- openapi.twse.com.tw/v1/indicesReport/MI_5MINS_HIST (the current month) "
    "+ www.twse.com.tw TAIEX/MI_5MINS_HIST (the monthly history)"
)
#: The first month TWSE's TAIEX history page answers.
TAIEX_FIRST = date(1999, 1, 1)
_CODE = re.compile(r"^(?:\d{4,6}(?:\.TWO?)?|[A-Z]{1,6})$")
#: marketlens-data's first month: from 2013-01 every listed company reports consolidated revenue (IFRSs).
FIRST = date(2013, 1, 1)


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


def _months(first: date, last: date) -> set[date]:
    n = (last.year - first.year) * 12 + last.month - first.month
    return {_months_back(last, i) for i in range(n + 1)}


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
        missing: list[date] = []
        history = _months_back(_month(today), 2)  # the history job's newest month
        if start <= history:
            # the history the store lacks from `start`, newest first, at most 24 months a sync, which
            # then counts as fresh for the TTL: sync again, while the call's time lasts, as long as
            # the window has months the store does not hold and the last sync held more
            params = {"start": start.strftime("%Y-%m")}
            wanted = _months(max(start, FIRST), min(end, history))
            until = time.monotonic() + s.call_timeout_seconds
            session.refresh(DATASET, params)
            held = twse._held_months(DATASET)
            while wanted - held and time.monotonic() < until:
                session.refresh(DATASET, params, force=True)
                before, held = held, twse._held_months(DATASET)
                if held == before:  # MOPS served nothing more (or mode is local)
                    break
            missing = sorted(wanted - held)
        adr_of = {code: adr for adr, code in twse.adr_codes().items()}
        df = session.read(DATASET, as_of=as_of, company_code=sorted(codes))
        return F.records(df), adr_of, missing

    (records, adr_of, missing), session = await common.run(ctx, s, work, source="twse")
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
    notes = []
    if missing:
        span = f"{missing[0]:%Y-%m}" + (f" to {missing[-1]:%Y-%m}" if len(missing) > 1 else "")
        notes.append(
            f"The store holds no full MOPS summary of {len(missing)} month(s) of the window ({span}), so "
            "rows of those months may be missing. The history arrives 24 months a sync: call again to "
            "fetch more (mode local fetches nothing, and a month MOPS does not serve stays missing)."
        )
        prov = prov.model_copy(update={"truncated": True, "truncation_note": notes[0]})
    # spec risk: external_text (note)
    return common.output(ctx, s, TaiwanMonthlyRevenue, rows, prov, notes=notes)


class TaiexInputs(Inputs):
    start: date | None = Field(default=None, description="First session (inclusive). Default: 30 days ago.")
    end: date | None = Field(default=None, description="Last session (inclusive). Default: today.")
    as_of: AsOf | None = common.as_of_field()


async def twse_taiex(ctx: ToolContext, args: TaiexInputs) -> ToolOutput:
    from datetime import timedelta

    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=today - timedelta(days=30), default_end=today
    )

    def work(session: common.Session):
        from omni.sources import _twse_taiex as taiex

        # the daily read: the current month (the OpenAPI) and the month before (its history page)
        session.refresh(TAIEX, {})
        missing: list[date] = []
        history = _months_back(_month(today), 2)  # the newest month only the history job reads
        if start <= history:
            # the history the store lacks from `start`, newest first, at most 36 months a sync: sync
            # again while the call's time lasts and a sync still adds months (as twse_monthly_revenue)
            params = {"start": max(_month(start), TAIEX_FIRST).strftime("%Y-%m")}
            wanted = _months(max(_month(start), TAIEX_FIRST), min(_month(end), history))
            until = time.monotonic() + s.call_timeout_seconds
            session.refresh(TAIEX, params)
            held = taiex.held_months()
            while wanted - held and time.monotonic() < until:
                session.refresh(TAIEX, params, force=True)
                before, held = held, taiex.held_months()
                if held == before:  # TWSE served nothing more (or mode is local)
                    break
            missing = sorted(wanted - held)
        return F.records(session.read(TAIEX, as_of=as_of)), missing

    (records, missing), session = await common.run(ctx, s, work, source="twse")
    rows = []
    for r in records:
        d = F.day(r["date"])
        if not F.within(d, start, end):
            continue
        rows.append(
            common.row(
                IndexLevel,
                {
                    "index": "TAIEX",
                    "date": d,
                    "open": F.number(r["open"]),
                    "high": F.number(r["high"]),
                    "low": F.number(r["low"]),
                    "close": F.number(r["close"]),
                    "unit": "index_points",
                    "publication": F.text(r["publication"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
        )
    rows.sort(key=lambda r: r.date)
    prov = common.provenance(
        ctx,
        provider="twse",
        route=TAIEX_ROUTE,
        datasets=(TAIEX,),
        request={"start": start, "end": end, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["exchange"],
    )
    notes = []
    if missing:
        span = f"{missing[0]:%Y-%m}" + (f" to {missing[-1]:%Y-%m}" if len(missing) > 1 else "")
        notes.append(
            f"The store holds fewer than a month's sessions of {len(missing)} month(s) of the window "
            f"({span}), so sessions of those months may be missing. The history arrives 36 months a sync: "
            "call again to fetch more (mode local fetches nothing)."
        )
        prov = prov.model_copy(update={"truncated": True, "truncation_note": notes[0]})
    return common.output(ctx, s, IndexLevel, rows, prov, notes=notes)


HANDLERS = {"twse_monthly_revenue": twse_monthly_revenue, "twse_taiex": twse_taiex}

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="twse_monthly_revenue",
        capability="filings",
        title="Taiwan monthly revenue (TWSE / TPEx)",
        description=common.describe(
            "Monthly revenue Taiwan-listed companies (TWSE, TPEx) file by the 10th of the next month on "
            "MOPS: per company and month, the month, the month before, a year before and the year to date "
            "in thousands of NT$ as published, and the changes it states (fractions). Takes Taiwan codes "
            "(2330, 2317, 6669) or ADR tickers (TSM, ASX, UMC). History from 2013-01, fetched 24 months a sync "
            "while the call's time lasts: a long first window may answer data_timeout while it keeps "
            "fetching, and an answer still lacking months of the window names them in a note (truncated; "
            "call again for more). Knowable at the statutory deadline "
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
    ToolSpec(
        name="twse_taiex",
        capability="macro",
        title="TAIEX (Taiwan Stock Exchange)",
        description=common.describe(
            "The TAIEX, the Taiwan Stock Exchange's capitalization-weighted stock price index, per session: "
            "open, high, low and close in index points as TWSE states them (nothing recomputed), from 1999-01. "
            "The current month comes from TWSE's OpenAPI, earlier months from its monthly history page; a "
            "window before last month fetches that history, 36 months a sync while the call's time lasts, "
            "and an answer still lacking months of the window names them in a note (truncated; call again). "
            "A session is knowable at 14:00 Taipei of its day (the close is the index at 13:30, after the "
            "closing call): a fixed rule, not a measured posting. Keyless.",
            "lagged",
        ),
        readme="The TAIEX index level per session (Taiwan Stock Exchange)",
        input_model=TaiexInputs,
        output_model=IndexLevel,
        provider="twse",
        route=TAIEX_ROUTE,
        handler=twse_taiex,
        golden_test=GOLDEN,
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {"twse_monthly_revenue": (DATASET,), "twse_taiex": (TAIEX,)}
