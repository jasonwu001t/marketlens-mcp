"""analytics_volatility: rolling annualised volatility of each series."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema.analytics import VolatilityPoint

from ..common import (
    CAPABILITY,
    PROVIDER,
    RETURNS_MODEL,
    adjustment_note,
    bar_timeframe,
    check_unique_times,
    default_periods_per_year,
    model_output,
    names,
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
    rows,
    series_counts,
    skipped_notes,
    value_column,
)

TOOL = "analytics_volatility"


class VolatilityArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = result_id_field("A stored result of prices, or of returns from analytics_returns.")
    window: int = Field(21, ge=2, le=2520, description="Returns per rolling window (2-2520).")
    return_kind: Literal["log", "simple"] = Field(
        "log",
        description="Returns computed from prices (ignored for a returns result, which keeps its kind).",
    )
    periods_per_year: float | None = Field(
        None,
        gt=0,
        allow_inf_nan=False,
        description="Annualisation factor. Default from the bar timeframe: 1d 252, 1w 52, 1mo 12, 1h 1638, "
        "Nmin 98280/N (US equity sessions); required otherwise (e.g. 365 for daily crypto).",
    )
    price_column: str | None = Field(
        None, description="Numeric column of prices (or returns). Default: the model's first value column."
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )


async def handler(ctx: ToolContext, args: VolatilityArgs) -> ToolOutput:
    inp = resolve(ctx, TOOL, args.result_id, series_column=args.series_column)
    notes = list(inp.notes)
    w = args.window
    with ctx.results.open([inp.rid]) as session:
        if inp.model == RETURNS_MODEL:
            col = value_column(TOOL, inp, args.price_column or "ret", param="price_column")
            kinds = [r["k"] for r in rows(session, f"SELECT DISTINCT kind AS k FROM {inp.table} ORDER BY 1")]
            kind = kinds[0] if len(kinds) == 1 else args.return_kind
            if "return_kind" in args.model_fields_set and args.return_kind != kind:
                notes.append(f"return_kind ignored: {inp.rid} already holds {kind} returns.")
            notes += skipped_notes(session, inp, col, positive=False)
            points = points_sql(inp, col, positive=False)
            check_unique_times(session, TOOL, inp, points)
            rets = f"SELECT s, ts, v AS r FROM ({points})"
        else:
            col = price_column(TOOL, inp, args.price_column, param="price_column")
            kind = args.return_kind
            notes += skipped_notes(session, inp, col, positive=True)
            points = points_sql(inp, col, positive=True)
            check_unique_times(session, TOOL, inp, points)
            rets = returns_sql(points, kind=kind)
        ppy = args.periods_per_year
        if ppy is None:
            tf = bar_timeframe(ctx, session, TOOL, inp)
            ppy = default_periods_per_year(tf) if tf is not None else None
            if ppy is None:
                raise refuse(
                    "periods_per_year_required",
                    f"{TOOL} cannot annualise: "
                    + (
                        f"the {tf} timeframe of {inp.label} has no default"
                        if tf
                        else f"{inp.label} has no single bar timeframe"
                    )
                    + "; pass periods_per_year (252 for daily equity bars, 365 for daily crypto bars).",
                )
            notes.append(
                f"periods_per_year = {ppy:g} from the {tf} timeframe of {inp.rid} (US equity sessions: 252 days of "
                "6.5 hours; pass periods_per_year for other markets, e.g. 365 for daily crypto)."
            )
        else:
            notes.append(f"periods_per_year = {ppy:g} as given.")
        counts = series_counts(session, rets)
        table = session.query(
            f"SELECT s AS series, {out_time('ts')} AS t, sd * sqrt(CAST(? AS DOUBLE)) AS volatility, "
            f'CAST({w} AS BIGINT) AS "window", CAST(? AS DOUBLE) AS periods_per_year, {ql(kind)} AS return_kind, '
            "NULL::MAP(VARCHAR, VARCHAR) AS absent "
            f"FROM (SELECT s, ts, stddev_samp(r) OVER win AS sd, count(r) OVER win AS n FROM ({rets}) "
            f"WINDOW win AS (PARTITION BY s ORDER BY ts ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW)) "
            f"WHERE n = {w} ORDER BY series, t",
            [ppy, ppy],
        )
    if table.num_rows == 0:
        raise refuse(
            "too_few_points",
            f"{TOOL} needs at least {w} returns in one series (window={w}); the longest series of {inp.label} "
            f"has {plural(max(counts.values(), default=0), 'return')}. Use a smaller window or more data.",
        )
    short = [s for s, n in counts.items() if n < w]
    if short:
        notes.append(f"Series with fewer than {w} returns are omitted: {names(short)}.")
    notes.append(
        f"volatility_t = stddev_samp of the last {w} {kind} returns x sqrt({ppy:g}), at the window's last "
        "observation; rows before the window fills are omitted."
    )
    note = adjustment_note(inp)
    if note:
        notes.append(note)
    prov = provenance(ctx, TOOL, [inp.info], args)
    return model_output(table, VolatilityPoint, prov, notes=notes, inputs=[inp.info])


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Rolling volatility",
    description=(
        "Rolling annualised volatility of each series in a stored result of prices (or of returns from "
        "analytics_returns), computed locally in DuckDB: vol_t = stddev_samp(returns over the last `window` "
        "rows) x sqrt(periods_per_year), reported at the window's last observation; rows before the window "
        "fills are omitted. return_kind log (default) or simple for prices. periods_per_year defaults from the "
        "bar timeframe (1d 252, 1w 52, 1mo 12, 1h 1638, Nmin 98280/N: US equity sessions) and is required "
        "otherwise (365 for daily crypto). Volatility is a fraction per year (0.2 = 20 %). Large outputs are "
        "stored and you get a result_id."
    ),
    readme="Rolling annualised volatility; window and periods per year stated on every row.",
    input_model=VolatilityArgs,
    output_model=VolatilityPoint,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_volatility.py",
)

SPECS = (SPEC,)
