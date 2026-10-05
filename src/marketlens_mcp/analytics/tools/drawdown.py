"""analytics_drawdown: drawdown from the running peak, as a series or as the
maximum drawdown of each series."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema.analytics import DrawdownPoint, DrawdownSummary

from ..common import (
    CAPABILITY,
    PROVIDER,
    adjustment_note,
    check_unique_times,
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
    series_counts,
    skipped_notes,
)

TOOL = "analytics_drawdown"


class DrawdownArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = result_id_field(
        "A stored result of prices or account equity (bars, portfolio history, ...)."
    )
    value_column: str | None = Field(
        None,
        description="Numeric column of positive values. Default: the model's first value column (close, equity).",
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )
    mode: Literal["max", "series"] = Field(
        "max", description="max: one summary row per series; series: the drawdown at every observation."
    )


def _series_sql(dd: str) -> str:
    return (
        f"SELECT s AS series, {out_time('ts')} AS t, v AS value, pk AS running_peak, v / pk - 1 AS drawdown, "
        f"NULL::MAP(VARCHAR, VARCHAR) AS absent FROM ({dd}) ORDER BY series, t"
    )


def _max_sql(dd: str, col: str) -> str:
    return (
        f"WITH d AS (SELECT s, ts, v, pk, v / pk - 1 AS dd FROM ({dd})), "
        "st AS (SELECT s, min(dd) AS mdd, count(*) AS n FROM d GROUP BY s), "
        "tr AS (SELECT d.s, min(d.ts) AS trough_ts, any_value(st.mdd) AS mdd, any_value(st.n) AS n "
        "FROM d JOIN st ON d.s = st.s AND d.dd = st.mdd GROUP BY d.s), "
        "pv AS (SELECT d.s, d.pk AS peak_value FROM d JOIN tr ON d.s = tr.s AND d.ts = tr.trough_ts), "
        "pk AS (SELECT d.s, max(d.ts) AS peak_ts FROM d JOIN tr ON d.s = tr.s AND d.ts <= tr.trough_ts "
        "JOIN pv ON pv.s = d.s WHERE d.v = pv.peak_value GROUP BY d.s), "
        "rc AS (SELECT d.s, min(d.ts) AS rec_ts FROM d JOIN tr ON d.s = tr.s AND d.ts > tr.trough_ts "
        "JOIN pv ON pv.s = d.s WHERE d.v >= pv.peak_value GROUP BY d.s) "
        "SELECT tr.s AS series, tr.mdd AS max_drawdown, "
        f"{out_time('pk.peak_ts')} AS peak_t, {out_time('tr.trough_ts')} AS trough_t, "
        f"CASE WHEN tr.mdd < 0 THEN {out_time('rc.rec_ts')} END AS recovery_t, "
        "(epoch(tr.trough_ts) - epoch(pk.peak_ts)) / 86400.0 AS peak_to_trough_days, tr.n AS n_obs, "
        f"{ql(col)} AS value_column, "
        "CASE WHEN tr.mdd = 0 THEN MAP {'recovery_t': 'not_applicable'} "
        "WHEN rc.rec_ts IS NULL THEN MAP {'recovery_t': 'not_recovered'} END AS absent "
        "FROM tr JOIN pk ON pk.s = tr.s LEFT JOIN rc ON rc.s = tr.s ORDER BY series"
    )


async def handler(ctx: ToolContext, args: DrawdownArgs) -> ToolOutput:
    inp = resolve(ctx, TOOL, args.result_id, series_column=args.series_column)
    col = price_column(TOOL, inp, args.value_column, param="value_column")
    notes = list(inp.notes)
    with ctx.results.open([inp.rid]) as session:
        notes += skipped_notes(session, inp, col, positive=True)
        points = points_sql(inp, col, positive=True)
        check_unique_times(session, TOOL, inp, points)
        counts = series_counts(session, points)
        enough = [s for s, n in counts.items() if n >= 2]
        if not enough:
            raise refuse(
                "too_few_points",
                f"{TOOL} needs at least 2 usable values in one series; the longest series of {inp.label} has "
                f"{plural(max(counts.values(), default=0), 'usable value')}.",
            )
        dd = (
            "SELECT s, ts, v, max(v) OVER (PARTITION BY s ORDER BY ts ROWS UNBOUNDED PRECEDING) AS pk "
            f"FROM ({points}) WHERE s IN (SELECT s FROM ({points}) GROUP BY s HAVING count(*) >= 2)"
        )
        model = DrawdownPoint if args.mode == "series" else DrawdownSummary
        table = session.query(_series_sql(dd) if args.mode == "series" else _max_sql(dd, col))
    short = [s for s, n in counts.items() if n < 2]
    if short:
        notes.append(f"Series with fewer than 2 values are omitted: {names(short)}.")
    if args.mode == "series":
        notes.append("drawdown = value / running_peak - 1, where running_peak is the highest value so far.")
    else:
        notes.append(
            "max_drawdown is the lowest value / running_peak - 1 (the earliest such time is the trough); peak_t is "
            "the last time at that peak before the trough; recovery_t is the first time after the trough at or "
            "above the peak (None: not_recovered by the last observation, or not_applicable when the series never "
            "fell below a peak)."
        )
    note = adjustment_note(inp)
    if note:
        notes.append(note)
    prov = provenance(ctx, TOOL, [inp.info], args)
    return model_output(table, model, prov, notes=notes, inputs=[inp.info])


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Drawdown",
    description=(
        "Drawdown of each series in a stored result of prices or equity, computed locally in DuckDB: running "
        "peak = the highest value so far, drawdown = value / peak - 1 (0 at a new peak, negative below it). "
        "mode max (default): one row per series with max_drawdown, peak_t, trough_t, recovery_t (the first "
        "time back at the peak; None if not recovered), peak_to_trough_days and n_obs. mode series: the "
        "drawdown at every observation. NULL, NaN, infinite and non-positive values are skipped. Drawdowns are "
        "fractions (-0.25 = 25 % below the peak). Use split-adjusted bars. Large outputs are stored and you get a "
        "result_id."
    ),
    readme="Maximum drawdown with peak, trough and recovery per series, or the drawdown series (mode=series).",
    input_model=DrawdownArgs,
    output_model=DrawdownSummary,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_drawdown.py",
)

SPECS = (SPEC,)
