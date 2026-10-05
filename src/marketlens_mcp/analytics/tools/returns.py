"""analytics_returns: simple or log returns of each series of a stored result."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema.analytics import ReturnPoint
from marketlens_schema.base import Timeframe

from ..common import (
    CAPABILITY,
    PROVIDER,
    adjustment_note,
    bar_timeframe,
    check_unique_times,
    model_output,
    names,
    no_period,
    out_time,
    plural,
    points_sql,
    price_column,
    provenance,
    ql,
    refuse,
    resolve,
    result_id_field,
    returns_sql,
    series_counts,
    skipped_notes,
    tf_seconds,
)

TOOL = "analytics_returns"


class ReturnsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = result_id_field(
        "A stored result of prices (bars, trades, snapshots, portfolio history, ...)."
    )
    price_column: str | None = Field(
        None,
        description="Numeric column of prices. Default: the model's first value column (close for bars).",
    )
    kind: Literal["simple", "log"] = Field(
        "simple", description="simple: p_t / p_t-1 - 1; log: ln(p_t / p_t-1)."
    )
    period: Timeframe | None = Field(
        None,
        description="Sample each series to the last price per UTC bucket first (1d, 1w, 1mo, 1h, 5min, ...); "
        "default: returns between consecutive rows.",
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )


async def handler(ctx: ToolContext, args: ReturnsArgs) -> ToolOutput:
    inp = resolve(ctx, TOOL, args.result_id, series_column=args.series_column)
    price = price_column(TOOL, inp, args.price_column, param="price_column")
    notes = list(inp.notes)
    with ctx.results.open([inp.rid]) as session:
        tf = bar_timeframe(ctx, session, TOOL, inp)
        if args.period is not None and tf is not None and tf_seconds(args.period) < tf_seconds(tf):
            raise refuse(
                "mismatched_frequency",
                f"{TOOL}: period {args.period} is finer than the {tf} timeframe of {inp.label}.",
                hint=f"Use period {tf} or coarser, or omit it.",
            )
        notes += skipped_notes(session, inp, price, positive=True)
        points = points_sql(inp, price, positive=True)
        check_unique_times(session, TOOL, inp, points)
        counts = series_counts(session, points)
        rets = returns_sql(points, kind=args.kind, period=args.period)
        table = session.query(
            f"SELECT s AS series, {out_time('ts')} AS t, r AS ret, {ql(args.kind)} AS kind, "
            f"CAST(? AS VARCHAR) AS period, {ql(price)} AS price_column, NULL::MAP(VARCHAR, VARCHAR) AS absent "
            f"FROM ({rets}) ORDER BY series, t",
            [args.period],
        )
    if table.num_rows == 0:
        raise refuse(
            "too_few_points",
            f"{TOOL} needs at least 2 usable prices in one series"
            f"{f' falling in 2 different {args.period} buckets' if args.period else ''}; the longest series of "
            f"{inp.label} has {plural(max(counts.values(), default=0), 'usable price')}.",
        )
    short = [s for s, n in counts.items() if n < 2]
    if short:
        notes.append(f"Series with fewer than 2 prices have no returns: {names(short)}.")
    if args.period is not None:
        notes.append(
            f"Prices were sampled to the last price in each {args.period} bucket (UTC-aligned; weeks start Monday); "
            "t is the bucket start. Empty buckets are skipped, so a return can span several buckets."
        )
    else:
        notes.append("Returns are between consecutive observations of each series; gaps are not filled.")
    note = adjustment_note(inp)
    if note:
        notes.append(note)
    prov = provenance(ctx, TOOL, [inp.info], args)
    return model_output(
        table, ReturnPoint, prov, notes=notes, absent=no_period(args.period), inputs=[inp.info]
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Returns",
    description=(
        "Simple or log returns of each series in a stored result (pass its result_id), computed locally in "
        "DuckDB. Per series ordered by time: simple r_t = p_t / p_t-1 - 1, log r_t = ln(p_t / p_t-1); the first "
        "row of each series has no return; NULL, NaN, infinite and non-positive prices are skipped and counted "
        "in the notes. With period (e.g. 1d, 1w), each series is first sampled to its last price per UTC bucket "
        "and t is the bucket start. Returns are fractions (0.01 = 1 %). Use split-adjusted bars "
        "(adjustment=all). Large outputs are stored and you get a result_id for results_query or other "
        "analytics_* tools."
    ),
    readme="Simple or log returns per series, optionally per period (last price per UTC bucket).",
    input_model=ReturnsArgs,
    output_model=ReturnPoint,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_returns.py",
)

SPECS = (SPEC,)
