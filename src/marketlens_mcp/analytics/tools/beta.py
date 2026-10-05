"""analytics_beta: beta, alpha and R squared of each asset series against a
one-series benchmark, static or rolling."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema.analytics import BetaPoint, BetaResult
from marketlens_schema.base import Timeframe

from ..common import (
    CAPABILITY,
    PROVIDER,
    Input,
    adjustment_note,
    bar_timeframe,
    check_unique_times,
    model_output,
    no_period,
    out_time,
    points_sql,
    price_column,
    provenance,
    ql,
    refuse,
    resolve,
    result_id_field,
    rows,
    series_counts,
    skipped_notes,
    tf_interval,
    tf_seconds,
)

TOOL = "analytics_beta"


class BetaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_result_id: str = result_id_field(
        "A stored result of prices of one or more assets (one beta per series)."
    )
    benchmark_result_id: str = result_id_field(
        "A stored result of prices of exactly one benchmark series (e.g. SPY)."
    )
    price_column: str | None = Field(
        None,
        description="Numeric price column in both results. Default: each model's first value column (close).",
    )
    return_kind: Literal["simple", "log"] = Field("simple", description="Returns used for both series.")
    period: Timeframe | None = Field(
        None,
        description="Sample both to the last price per UTC bucket first (needed when their bars differ).",
    )
    window: int | None = Field(
        None,
        ge=2,
        le=2520,
        description="Rolling beta over this many shared returns (one row per window end).",
    )
    min_obs: int = Field(
        20, ge=2, le=100000, description="Fewest shared returns for a static beta; below it None."
    )
    series_column: str | None = Field(
        None, description="Asset column naming each series. Default: its group column."
    )


def _sampled(points: str, period: str | None) -> str:
    if period is None:
        return points
    return (
        f"SELECT s, b AS ts, v FROM (SELECT s, time_bucket({tf_interval(period)}, ts) AS b, arg_max(v, ts) AS v "
        f"FROM ({points}) GROUP BY s, b)"
    )


def _check_frequencies(a: Input, tfa: str | None, b: Input, tfb: str | None, period: str | None) -> None:
    if period is None:
        if tfa is not None and tfb is not None and tfa != tfb:
            coarser = tfa if tf_seconds(tfa) > tf_seconds(tfb) else tfb
            raise refuse(
                "mismatched_frequency",
                f"{TOOL}: {a.label} has {tfa} bars and {b.label} has {tfb} bars; pass period (e.g. period={coarser}) "
                "to sample both to the same buckets.",
            )
        return
    for side, tf in ((a, tfa), (b, tfb)):
        if tf is not None and tf_seconds(period) < tf_seconds(tf):
            raise refuse(
                "mismatched_frequency",
                f"{TOOL}: period {period} is finer than the {tf} timeframe of {side.label}; use {tf} or coarser.",
            )


async def handler(ctx: ToolContext, args: BetaArgs) -> ToolOutput:
    a = resolve(ctx, TOOL, args.asset_result_id, series_column=args.series_column)
    b = resolve(ctx, TOOL, args.benchmark_result_id)
    ca = price_column(TOOL, a, args.price_column, param="price_column")
    cb = price_column(TOOL, b, args.price_column, param="price_column")
    notes = list(a.notes) + list(b.notes)
    kind, w, m = args.return_kind, args.window, args.min_obs
    with ctx.results.open([a.rid, b.rid]) as session:
        notes += skipped_notes(session, a, ca, positive=True)
        if b.rid != a.rid:
            notes += skipped_notes(session, b, cb, positive=True)
        pa_ = points_sql(a, ca, positive=True)
        pb = points_sql(b, cb, positive=True)
        check_unique_times(session, TOOL, a, pa_)
        check_unique_times(session, TOOL, b, pb)
        bench_series = series_counts(session, pb)
        if len(bench_series) > 1:
            raise refuse(
                "benchmark_not_single_series",
                f"{TOOL} needs a benchmark with exactly one series; {b.label} has {len(bench_series)} in column "
                f"{b.group}. Keep one with results_query (store=true) first.",
            )
        asset_series = series_counts(session, pa_)
        if max(asset_series.values(), default=0) < 2 or max(bench_series.values(), default=0) < 2:
            raise refuse(
                "too_few_points",
                f"{TOOL} needs at least 2 usable prices in the benchmark and in an asset series; {a.label} and "
                f"{b.label} do not have them.",
            )
        benchmark = next(iter(bench_series)) if b.group is not None else b.rid
        _check_frequencies(
            a, bar_timeframe(ctx, session, TOOL, a), b, bar_timeframe(ctx, session, TOOL, b), args.period
        )
        f = "ln({x} / lag({x}) OVER w)" if kind == "log" else "{x} / lag({x}) OVER w - 1"
        joined = (
            f"SELECT x.s, x.ts, x.v AS va, y.v AS vb FROM ({_sampled(pa_, args.period)}) x "
            f"JOIN ({_sampled(pb, args.period)}) y ON x.ts = y.ts"
        )
        rets = (
            f"SELECT s, ts, ra, rb FROM (SELECT s, ts, {f.format(x='va')} AS ra, {f.format(x='vb')} AS rb FROM ({joined}) "
            "WINDOW w AS (PARTITION BY s ORDER BY ts)) WHERE ra IS NOT NULL AND rb IS NOT NULL"
        )
        period_sql = "CAST(? AS VARCHAR)"
        if w is None:
            model = BetaResult
            fields3 = "'beta': '{c}', 'alpha': '{c}', 'r_squared': '{c}'"
            table = session.query(
                f"WITH st AS (SELECT s, covar_samp(ra, rb) AS cv, var_samp(rb) AS vb, avg(ra) AS ma, avg(rb) AS mb, "
                f"corr(ra, rb) AS c, count(*) AS n, min(ts) AS t0, max(ts) AS t1 FROM ({rets}) GROUP BY s), "
                f"ok AS (SELECT *, n >= {m} AND vb > 0 AS good FROM st) "
                f"SELECT nm.s AS series, {ql(benchmark)} AS benchmark, "
                "CASE WHEN good THEN cv / vb END AS beta, CASE WHEN good THEN ma - (cv / vb) * mb END AS alpha, "
                "CASE WHEN good AND NOT isnan(c) THEN c * c END AS r_squared, coalesce(n, 0) AS n_obs, "
                f'{out_time("t0")} AS start, {out_time("t1")} AS "end", {ql(kind)} AS return_kind, '
                f"{period_sql} AS period, "
                "CASE WHEN coalesce(n, 0) = 0 THEN MAP {"
                + fields3.format(c="insufficient_data")
                + ", 'start': 'no_data', 'end': 'no_data'} "
                f"WHEN n < {m} THEN MAP {{" + fields3.format(c="insufficient_data") + "} "
                "WHEN vb IS NULL OR vb = 0 THEN MAP {" + fields3.format(c="not_applicable") + "} "
                "WHEN c IS NULL OR isnan(c) THEN MAP {'r_squared': 'not_applicable'} END AS absent "
                f"FROM (SELECT DISTINCT s FROM ({pa_})) nm LEFT JOIN ok USING (s) ORDER BY series",
                [args.period],
            )
        else:
            model = BetaPoint
            table = session.query(
                f"SELECT s AS series, {ql(benchmark)} AS benchmark, {out_time('ts')} AS t, "
                f'CASE WHEN vb > 0 THEN cv / vb END AS beta, CAST({w} AS BIGINT) AS "window", '
                f"{ql(kind)} AS return_kind, {period_sql} AS period, "
                "CASE WHEN vb IS NULL OR vb = 0 THEN MAP {'beta': 'not_applicable'} END AS absent "
                f"FROM (SELECT s, ts, covar_samp(ra, rb) OVER win AS cv, var_samp(rb) OVER win AS vb, "
                f"count(*) OVER win AS n FROM ({rets}) "
                f"WINDOW win AS (PARTITION BY s ORDER BY ts ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW)) "
                f"WHERE n = {w} ORDER BY series, t",
                [args.period],
            )
            if table.num_rows == 0:
                most = rows(
                    session, f"SELECT max(n) AS n FROM (SELECT count(*) AS n FROM ({rets}) GROUP BY s)"
                )
                raise refuse(
                    "too_few_points",
                    f"{TOOL} needs at least {w} shared returns for window={w}; the most any asset series shares with "
                    f"the benchmark is {most[0]['n'] or 0}.",
                )
    if args.period is not None:
        notes.append(
            f"Both results were sampled to their last price per {args.period} bucket (UTC-aligned) first."
        )
    notes.append(
        "Returns are computed over the timestamps both results share, so the asset and benchmark returns of a row "
        "cover the same interval."
    )
    if w is None:
        notes.append(
            "beta = covar_samp(r_a, r_b) / var_samp(r_b); alpha = mean(r_a) - beta x mean(r_b), per period; "
            f"r_squared = corr(r_a, r_b)^2; None with insufficient_data below min_obs={m} shared returns."
        )
    else:
        notes.append(
            f"Rolling beta over the last {w} shared returns, at the window's last time; rows before the window "
            "fills are omitted (min_obs is not used)."
        )
    for side in (a, b) if b.rid != a.rid else (a,):
        note = adjustment_note(side)
        if note:
            notes.append(note)
    prov = provenance(ctx, TOOL, [a.info, b.info], args)
    return model_output(
        table, model, prov, notes=notes, absent=no_period(args.period), inputs=[a.info, b.info]
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Beta",
    description=(
        "Beta of each asset series against a benchmark, from two stored price results (the benchmark must hold "
        "exactly one series), computed locally in DuckDB. Returns (simple or log) are computed over the "
        "timestamps both results share: beta = covar_samp(r_a, r_b) / var_samp(r_b), alpha = mean(r_a) - beta x "
        "mean(r_b) per period, r_squared = corr^2; fewer than min_obs (20) shared returns gives None "
        "(insufficient_data). Bars of different timeframes are refused unless period samples both to the same "
        "UTC buckets. With window, a rolling beta per window end. Large outputs are stored and you get a result_id."
    ),
    readme="Beta, alpha and R squared versus a one-series benchmark; rolling with window.",
    input_model=BetaArgs,
    output_model=BetaResult,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_beta.py",
)

SPECS = (SPEC,)
