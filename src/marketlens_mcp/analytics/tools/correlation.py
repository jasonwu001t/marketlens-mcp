"""analytics_correlation: the pairwise correlation matrix of the series of a
stored result, in long form."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import BUILTIN_MODELS
from marketlens_schema.analytics import CorrelationCell

from ..common import (
    CAPABILITY,
    PRICE_UNITS,
    PROVIDER,
    adjustment_note,
    check_unique_times,
    model_output,
    out_time,
    plural,
    points_sql,
    provenance,
    ql,
    refuse,
    resolve,
    result_id_field,
    scalar,
    skipped_notes,
    value_column,
)

TOOL = "analytics_correlation"
MAX_SERIES = 50


class CorrelationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = result_id_field(
        "A stored result holding several series (e.g. bars of several tickers, or returns)."
    )
    value_column: str | None = Field(
        None,
        description="Numeric column to correlate. Default: the model's first value column (ret for returns, close "
        "for bars; ret for a query result that has one). A column in price units is turned into simple returns first.",
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )
    min_overlap: int = Field(
        20, ge=2, le=10000, description="Fewest shared observations for a value; below it the cell is None."
    )
    method: Literal["pearson"] = Field("pearson", description="Pearson correlation.")


async def handler(ctx: ToolContext, args: CorrelationArgs) -> ToolOutput:
    inp = resolve(ctx, TOOL, args.result_id, series_column=args.series_column)
    if inp.group is None:
        raise refuse(
            "no_series_column",
            f"{TOOL} needs a series column; {inp.label} has none. Pass series_column (e.g. ticker).",
        )
    given = args.value_column
    if given is None and inp.model not in BUILTIN_MODELS and inp.column("ret") is not None:
        given = "ret"
    col = value_column(TOOL, inp, given, param="value_column")
    unit = inp.column(col).unit
    as_returns = unit in PRICE_UNITS
    notes = list(inp.notes)
    m = args.min_overlap
    with ctx.results.open([inp.rid]) as session:
        notes += skipped_notes(session, inp, col, positive=as_returns)
        points = points_sql(inp, col, positive=as_returns)
        check_unique_times(session, TOOL, inp, points)
        n_series = scalar(session, f"SELECT count(DISTINCT s) FROM ({points})")
        if n_series > MAX_SERIES:
            raise refuse(
                "too_many_series",
                f"{TOOL} handles at most {MAX_SERIES} series; {inp.label} has {n_series} in column {inp.group}.",
                hint="Keep fewer series with results_query (store=true) first.",
            )
        if n_series < 2:
            raise refuse(
                "too_few_series",
                f"{TOOL} needs at least two series; {inp.label} has {n_series} in column {inp.group}.",
                hint="Fetch several tickers into one result, or pass series_column.",
            )
        joined = (
            "SELECT a.s AS a, b.s AS b, a.ts AS ts, a.v AS xa, b.v AS xb FROM x a JOIN x b ON a.ts = b.ts"
        )
        if as_returns:
            pairs = (
                "SELECT a, b, ts, xa, xb FROM (SELECT a, b, ts, xa / lag(xa) OVER w - 1 AS xa, xb / lag(xb) OVER w - 1 AS xb "
                f"FROM ({joined}) WINDOW w AS (PARTITION BY a, b ORDER BY ts)) WHERE xa IS NOT NULL AND xb IS NOT NULL"
            )
        else:
            pairs = joined
        table = session.query(
            f"WITH x AS ({points}), "
            f"st AS (SELECT a, b, corr(xa, xb) AS c, count(*) AS n, min(ts) AS t0, max(ts) AS t1 FROM ({pairs}) GROUP BY a, b), "
            "nm AS (SELECT DISTINCT s FROM x) "
            "SELECT na.s AS a, nb.s AS b, "
            f"CASE WHEN coalesce(st.n, 0) < {m} OR st.c IS NULL OR isnan(st.c) THEN NULL "
            "WHEN na.s = nb.s THEN 1.0 ELSE greatest(-1.0, least(1.0, st.c)) END AS correlation, "
            f"coalesce(st.n, 0) AS n_obs, {ql(col)} AS value_column, "
            f'{out_time("st.t0")} AS start, {out_time("st.t1")} AS "end", '
            "CASE WHEN coalesce(st.n, 0) = 0 THEN MAP {'correlation': 'insufficient_data', 'start': 'no_data', "
            "'end': 'no_data'} "
            f"WHEN st.n < {m} THEN MAP {{'correlation': 'insufficient_data'}} "
            "WHEN st.c IS NULL OR isnan(st.c) THEN MAP {'correlation': 'not_applicable'} END AS absent "
            "FROM nm na CROSS JOIN nm nb LEFT JOIN st ON st.a = na.s AND st.b = nb.s ORDER BY a, b"
        )
    n_obs = table.column("n_obs").to_pylist()
    if max(n_obs, default=0) < 2:
        raise refuse(
            "too_few_points",
            f"{TOOL} needs at least 2 observations shared by a pair of series"
            f"{' (2 returns, so 3 prices)' if as_returns else ''}; {inp.label} has no such pair.",
        )
    constant = sum(
        1
        for a, b, ab in zip(
            table.column("a").to_pylist(),
            table.column("b").to_pylist(),
            table.column("absent").to_pylist(),
            strict=True,
        )
        if a < b and ab and dict(ab).get("correlation") == "not_applicable"
    )
    if constant:
        notes.append(
            f"{plural(constant, 'pair')} with a constant series over the shared observations: correlation is "
            "undefined there (None, not_applicable)."
        )
    if as_returns:
        notes.append(
            f"{col} is in {unit} units, so each pair was turned into simple returns over the timestamps both series "
            "share (both returns of a pair cover the same interval) before correlating."
        )
        note = adjustment_note(inp)
        if note:
            notes.append(note)
    notes.append(
        f"Pearson correlation of each pair over the timestamps where both series have a value (inner join on "
        f"time); None with insufficient_data when fewer than {m} shared observations. Both orders and the "
        "diagonal are listed."
    )
    prov = provenance(ctx, TOOL, [inp.info], args)
    return model_output(table, CorrelationCell, prov, notes=notes, inputs=[inp.info])


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Correlation matrix",
    description=(
        "Pairwise Pearson correlation of the series in one stored result (e.g. bars of several tickers, or "
        "returns), computed locally in DuckDB, in long form: one row per pair (a, b), both orders and the "
        "diagonal included. Each pair uses the timestamps where both series have a value. value_column "
        "defaults to the model's first value column; a column in price units (close) is turned into simple "
        "returns per pair first. A pair with fewer than min_overlap (20) shared observations gets None "
        "(insufficient_data). At most 50 series. Large outputs are stored and you get a result_id."
    ),
    readme="Pairwise Pearson correlation (long form) over shared timestamps; prices become returns first.",
    input_model=CorrelationArgs,
    output_model=CorrelationCell,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_correlation.py",
)

SPECS = (SPEC,)
