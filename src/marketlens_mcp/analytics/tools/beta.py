"""analytics_beta: beta, alpha and R squared of each asset series against a
one-series benchmark, static or rolling."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_mcp.results_api import ResultSession
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
    names,
    no_period,
    optional_result_id_field,
    out_time,
    points_sql,
    price_column,
    provenance,
    qi,
    ql,
    refuse,
    resolve,
    rows,
    series_counts,
    skipped_notes,
    store_field,
    tf_interval,
    tf_seconds,
)

TOOL = "analytics_beta"


class BetaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str | None = optional_result_id_field(
        "One stored result of prices holding the assets and the benchmark (e.g. bars of AAPL, MSFT and SPY), "
        "as the other analytics tools take it: name the benchmark with benchmark_series. Instead of "
        "asset_result_id."
    )
    asset_result_id: str | None = optional_result_id_field(
        "A stored result of prices of one or more assets (one beta per series); with benchmark_result_id."
    )
    benchmark_result_id: str | None = optional_result_id_field(
        "A stored result of prices of the benchmark: one series (e.g. SPY), or pick one with benchmark_series."
    )
    benchmark_series: str | None = Field(
        None,
        description="The benchmark's value of the series column (e.g. SPY): picks it out of benchmark_result_id, "
        "or out of result_id, whose other series are then the assets.",
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
    store: bool = store_field()


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


def _benchmark_name(session: ResultSession, a: Input, b: Input) -> str:
    """A benchmark without a series column: its one value of the asset's
    series column (e.g. ticker SPY) when it has that column, else its id."""
    col = b.column(a.group) if a.group is not None else None
    if col is not None and col.type == "VARCHAR":
        g = qi(col.name)
        found = rows(session, f"SELECT DISTINCT {g} AS g FROM {b.table} WHERE {g} IS NOT NULL LIMIT 2")
        if len(found) == 1:
            return found[0]["g"]
    return b.rid


def _inputs(ctx: ToolContext, args: BetaArgs) -> tuple[Input, Input]:
    """The asset and benchmark handles: result_id (with benchmark_series or
    benchmark_result_id), or asset_result_id with benchmark_result_id."""
    if args.result_id is not None and args.asset_result_id is not None:
        raise refuse("invalid_arguments", f"{TOOL}: pass result_id or asset_result_id, not both.")
    asset = args.result_id or args.asset_result_id
    if asset is None:
        raise refuse(
            "invalid_arguments",
            f"{TOOL} needs result_id with benchmark_series (one result holding the assets and the benchmark), or "
            "asset_result_id with benchmark_result_id.",
        )
    bench = args.benchmark_result_id or (asset if args.result_id is not None else None)
    if bench is None:
        raise refuse("invalid_arguments", f"{TOOL}: asset_result_id needs benchmark_result_id.")
    if bench == asset and args.result_id is not None and args.benchmark_series is None:
        raise refuse(
            "invalid_arguments",
            f"{TOOL}: with result_id alone, name the benchmark's series with benchmark_series (e.g. SPY).",
        )
    a = resolve(ctx, TOOL, asset, series_column=args.series_column)
    b = resolve(ctx, TOOL, bench, series_column=args.series_column if bench == asset else None)
    if args.benchmark_series is not None and b.group is None:
        raise refuse(
            "no_series_column",
            f"{TOOL}: benchmark_series needs a series column in {b.label}; it has none.",
        )
    return a, b


def _benchmark_points(session: ResultSession, b: Input, pb: str, name: str | None) -> str:
    """The benchmark's points: the one series named ``name`` when given (refused when absent)."""
    if name is None:
        return pb
    found = series_counts(session, pb)
    if name not in found:
        raise refuse(
            "unknown_series",
            f"{TOOL}: {b.label} has no series {name!r} in column {b.group}. Series: {names(list(found)[:20])}.",
        )
    return f"SELECT * FROM ({pb}) WHERE s = {ql(name)}"


def _numbered(points: str, *, by_series: bool) -> str:
    """The points with their position in their series (to tell the returns
    that skip an observation of one side)."""
    part = "PARTITION BY s " if by_series else ""
    return f"SELECT s, ts, v, row_number() OVER ({part}ORDER BY ts) AS i FROM ({points})"


def _gap_note(session: ResultSession, rets: str, period: str | None) -> list[str]:
    """A note naming the series whose returns between shared timestamps skip
    observations one side has and the other lacks (overnight hours a 24-hour
    market trades, a missing bar)."""
    found = [
        r
        for r in rows(
            session,
            f"SELECT s, count(*) FILTER (WHERE gap) AS g, count(*) AS n FROM ({rets}) GROUP BY s ORDER BY s",
        )
        if r["g"]
    ]
    if not found:
        return []
    listed = ", ".join(f"{r['s']} {r['g']} of {r['n']}" for r in found[:10])
    more = f" and {len(found) - 10} more series" if len(found) > 10 else ""
    return [
        f"Returns that span observations only one side has (hours one market trades and the other does not, or "
        f"missing bars): {listed}{more}. Both returns of such a row cover the same interval, but a longer one than "
        "the rest"
        + (
            "; pass period (e.g. 1d) to sample both to the same buckets."
            if period is None
            else f", even in {period} buckets."
        )
    ]


async def handler(ctx: ToolContext, args: BetaArgs) -> ToolOutput:
    a, b = _inputs(ctx, args)
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
        pb = _benchmark_points(session, b, pb, args.benchmark_series)
        if args.benchmark_series is not None and b.rid == a.rid:
            pa_ = f"SELECT * FROM ({pa_}) WHERE s <> {ql(args.benchmark_series)}"
        bench_series = series_counts(session, pb)
        if len(bench_series) > 1:
            raise refuse(
                "benchmark_not_single_series",
                f"{TOOL} needs a benchmark with exactly one series; {b.label} has {len(bench_series)} in column "
                f"{b.group}. Name one with benchmark_series (e.g. {next(iter(bench_series))}), or keep one with "
                "results_query (store=true) first.",
            )
        asset_series = series_counts(session, pa_)
        if max(asset_series.values(), default=0) < 2 or max(bench_series.values(), default=0) < 2:
            raise refuse(
                "too_few_points",
                f"{TOOL} needs at least 2 usable prices in the benchmark and in an asset series; {a.label} and "
                f"{b.label} do not have them.",
            )
        if args.benchmark_series is not None:
            benchmark = args.benchmark_series
        elif b.group is not None:
            benchmark = next(iter(bench_series))
        else:
            benchmark = _benchmark_name(session, a, b)
        _check_frequencies(
            a, bar_timeframe(ctx, session, TOOL, a), b, bar_timeframe(ctx, session, TOOL, b), args.period
        )
        f = "ln({x} / lag({x}) OVER w)" if kind == "log" else "{x} / lag({x}) OVER w - 1"
        joined = (
            "SELECT x.s, x.ts, x.v AS va, y.v AS vb, x.i AS ia, y.i AS ib "
            f"FROM ({_numbered(_sampled(pa_, args.period), by_series=True)}) x "
            f"JOIN ({_numbered(_sampled(pb, args.period), by_series=False)}) y ON x.ts = y.ts"
        )
        rets = (
            f"SELECT s, ts, ra, rb, gap FROM (SELECT s, ts, {f.format(x='va')} AS ra, {f.format(x='vb')} AS rb, "
            "ia - lag(ia) OVER w > 1 OR ib - lag(ib) OVER w > 1 AS gap FROM "
            f"({joined}) WINDOW w AS (PARTITION BY s ORDER BY ts)) WHERE ra IS NOT NULL AND rb IS NOT NULL"
        )
        period_sql = "CAST(? AS VARCHAR)"
        fields3 = "'beta': '{c}', 'alpha': '{c}', 'r_squared': '{c}'"
        if w is None:
            model = BetaResult
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
                "CASE WHEN vb > 0 THEN cv / vb END AS beta, CASE WHEN vb > 0 THEN ma - (cv / vb) * mb END AS alpha, "
                "CASE WHEN vb > 0 AND NOT isnan(c) THEN c * c END AS r_squared, n AS n_obs, "
                f'{out_time("t0")} AS start, CAST({w} AS BIGINT) AS "window", '
                f"{ql(kind)} AS return_kind, {period_sql} AS period, "
                "CASE WHEN vb IS NULL OR vb = 0 THEN MAP {" + fields3.format(c="not_applicable") + "} "
                "WHEN c IS NULL OR isnan(c) THEN MAP {'r_squared': 'not_applicable'} END AS absent "
                f"FROM (SELECT s, ts, covar_samp(ra, rb) OVER win AS cv, var_samp(rb) OVER win AS vb, "
                "avg(ra) OVER win AS ma, avg(rb) OVER win AS mb, corr(ra, rb) OVER win AS c, "
                f"count(*) OVER win AS n, min(ts) OVER win AS t0 FROM ({rets}) "
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
        gaps = _gap_note(session, rets, args.period)
    if args.period is not None:
        notes.append(
            f"Both results were sampled to their last price per {args.period} bucket (UTC-aligned) first."
        )
    notes.append(
        "Returns are computed over the timestamps both results share, so the asset and benchmark returns of a row "
        "cover the same interval."
    )
    notes += gaps
    if w is None:
        notes.append(
            "beta = covar_samp(r_a, r_b) / var_samp(r_b); alpha = mean(r_a) - beta x mean(r_b), per period; "
            f"r_squared = corr(r_a, r_b)^2; None with insufficient_data below min_obs={m} shared returns."
        )
    else:
        notes.append(
            f"Rolling beta over the last {w} shared returns, at the window's last time (start is its first); alpha "
            "and r_squared over the same window, as in the static beta; rows before the window fills are omitted "
            "(min_obs is not used)."
        )
    for side in (a, b) if b.rid != a.rid else (a,):
        note = adjustment_note(side)
        if note:
            notes.append(note)
    prov = provenance(ctx, TOOL, [a.info, b.info], args)
    return model_output(
        table,
        model,
        prov,
        notes=notes,
        absent=no_period(args.period),
        inputs=[a.info, b.info],
        store=args.store,
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Beta",
    description=(
        "Beta of each asset series against a benchmark, computed locally in DuckDB, from one stored price result "
        "(result_id, with benchmark_series naming the benchmark's series, e.g. SPY) or two (asset_result_id and "
        "benchmark_result_id, whose one series, or benchmark_series, is the benchmark). Returns (simple or log) "
        "are computed over the timestamps both share: beta = covar_samp(r_a, r_b) / var_samp(r_b), alpha = "
        "mean(r_a) - beta x mean(r_b) per period, r_squared = corr^2; fewer than min_obs (20) shared returns gives "
        "None (insufficient_data); a note names returns that span hours only one side trades. Bars of different "
        "timeframes are refused unless period samples both to the same UTC buckets. With window, a rolling beta "
        "per window end with alpha, r_squared, n_obs and the window's start. Large outputs are stored and you get "
        "a result_id."
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
