"""analytics_resample: bars to a coarser timeframe (OHLCV rules), or any time
series to buckets with one aggregation per value column."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_mcp.results_api import ResultSession
from marketlens_schema import BUILTIN_MODELS
from marketlens_schema.base import AbsenceCode, AbsenceReason, Timeframe
from marketlens_schema.market import Bar

from ..common import (
    BAR_MODELS,
    CAPABILITY,
    PROVIDER,
    Input,
    bar_timeframe,
    dynamic_output,
    finite,
    model_output,
    out_time,
    plural,
    provenance,
    qi,
    ql,
    refuse,
    resolve,
    result_id_field,
    rows,
    tf_interval,
    tf_nests,
    tf_seconds,
)

TOOL = "analytics_resample"

#: Stated verbatim in the notes of every bar resample.
BAR_RULES = (
    "Resample rules for bars: open = the first open by time; high = the max high; low = the min low; "
    "close = the last close by time; volume = the sum of volume; trade_count = the sum, None if any input "
    "bar has none; vwap = sum(vwap x volume) / sum(volume) over the bars with volume, None "
    "(insufficient_data) when that volume is 0 and None (not_provided_by_source) when a bar with volume has "
    "no vwap; timeframe = the target; t = the bucket start, UTC-aligned (weeks start Monday 00:00 UTC); a "
    "bar belongs to the bucket that contains its start."
)

_ORDERED = "ORDER BY _ts, _rid"
_BAR_SPECIAL = ("timeframe", "t", "open", "high", "low", "close", "volume", "trade_count", "vwap", "absent")
#: The bar columns a row is skipped for when one of them is not finite.
_BAR_VALUES = ("open", "high", "low", "close", "volume", "vwap")


class ResampleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = result_id_field(
        "A stored time series: bars, or quotes, trades, snapshots, portfolio history, ..."
    )
    timeframe: Timeframe = Field(
        description="Target bucket: Nmin, Nh, 1d, 1w (Monday 00:00 UTC), Nmo; coarser than the input's bars."
    )
    agg: Literal["last", "first", "mean", "sum", "min", "max"] = Field(
        "last",
        description="Aggregation of each value column for inputs that are not bars (bars use OHLCV rules).",
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )


def _check_target(inp: Input, source: str, target: str) -> None:
    if tf_seconds(target) <= tf_seconds(source):
        raise refuse(
            "timeframe_not_coarser",
            f"{TOOL} needs a target coarser than the {source} timeframe of {inp.label}; got {target}.",
        )
    if not tf_nests(source, target):
        raise refuse(
            "timeframe_not_nested",
            f"{TOOL}: {target} buckets do not hold whole {source} bars; choose a multiple of {source}, or 1d, 1w or "
            "a month.",
        )


def _all_finite(values: list[str]) -> str:
    """SQL: no value column holds NaN or an infinity (NULLs are allowed)."""
    return " AND ".join(f"coalesce({finite(qi(c))}, true)" for c in values)


def _src(inp: Input, values: list[str]) -> str:
    """The rows with a time and no non-finite value (NaN, +-infinity)."""
    return (
        f"SELECT *, {inp.ts()} AS _ts, rowid AS _rid FROM {inp.table} "
        f"WHERE {qi(inp.time)} IS NOT NULL AND {_all_finite(values)}"
    )


def _non_finite(session: ResultSession, inp: Input, values: list[str]) -> tuple[int, list[str]]:
    """Rows with a time that ``_src`` skips for a non-finite value, and the
    columns holding such values."""
    has_time = f"{qi(inp.time)} IS NOT NULL"
    parts = [f"count(*) FILTER (WHERE {has_time} AND NOT ({_all_finite(values)})) AS c0"] + [
        f"count(*) FILTER (WHERE {has_time} AND NOT coalesce({finite(qi(c))}, true)) AS c{i + 1}"
        for i, c in enumerate(values)
    ]
    (row,) = rows(session, f"SELECT {', '.join(parts)} FROM {inp.table}")
    return row["c0"], [c for i, c in enumerate(values) if row[f"c{i + 1}"]]


def _group_key(inp: Input) -> tuple[str, str]:
    """(select item, group-by list) for the series key and the bucket."""
    if inp.group is None:
        return "", "_b"
    return f"{qi(inp.group)} AS _g, ", "_g, _b"


def _bar_sql(inp: Input, target: str, columns: list[str], values: list[str]) -> str:
    key, group_by = _group_key(inp)
    carried = [c for c in columns if c not in _BAR_SPECIAL and c != inp.group]
    inner = [
        f"first(open {_ORDERED}) AS open",
        "max(high) AS high",
        "min(low) AS low",
        f"last(close {_ORDERED}) AS close",
        "sum(volume) AS volume",
        "CASE WHEN count(trade_count) = count(*) THEN sum(trade_count) END AS trade_count",
        "count(*) FILTER (WHERE volume > 0 AND vwap IS NULL) AS _vwap_missing",
        "sum(volume) FILTER (WHERE volume > 0) AS _vol",
        "sum(vwap * volume) FILTER (WHERE volume > 0) AS _pv",
    ] + [f"last({qi(c)} {_ORDERED}) AS {qi(c)}" for c in carried]
    entries = (
        "list_filter([CASE WHEN trade_count IS NULL THEN {'key': 'trade_count', 'value': 'not_provided_by_source'} END, "
        "CASE WHEN _vwap_missing > 0 THEN {'key': 'vwap', 'value': 'not_provided_by_source'} "
        "WHEN coalesce(_vol, 0) = 0 THEN {'key': 'vwap', 'value': 'insufficient_data'} END], e -> e IS NOT NULL)"
    )
    outer = []
    for c in columns:
        if c == inp.group:
            outer.append(f"_g AS {qi(c)}")
        elif c == "timeframe":
            outer.append(f"{ql(target)} AS timeframe")
        elif c == inp.time:
            outer.append(f"{out_time('_b')} AS {qi(c)}")
        elif c == "vwap":
            outer.append(
                "CASE WHEN _vwap_missing > 0 OR coalesce(_vol, 0) = 0 THEN NULL ELSE _pv / _vol END AS vwap"
            )
        elif c == "absent":
            outer.append("CASE WHEN len(_abs) = 0 THEN NULL ELSE map_from_entries(_abs) END AS absent")
        else:
            outer.append(qi(c))
    order = "_g, _b" if inp.group is not None else "_b"
    return (
        f"SELECT {', '.join(outer)} FROM (SELECT *, {entries} AS _abs FROM ("
        f"SELECT {key}time_bucket({tf_interval(target)}, _ts) AS _b, {', '.join(inner)} "
        f"FROM ({_src(inp, values)}) GROUP BY {group_by})) ORDER BY {order}"
    )


def _value_columns(inp: Input) -> list[str]:
    numeric = set(inp.numeric_columns())
    cls = BUILTIN_MODELS.get(inp.model)
    if cls is not None:
        return [c for c in cls.value_columns if c in numeric]
    return [c.name for c in inp.info.columns if c.name in numeric and c.name not in (inp.time, inp.group)]


def _agg(inp: Input, col: str, agg: str) -> str:
    v = qi(col)
    typ = inp.column(col).type
    expr = {
        "last": f"last({v} {_ORDERED}) FILTER (WHERE {v} IS NOT NULL)",
        "first": f"first({v} {_ORDERED}) FILTER (WHERE {v} IS NOT NULL)",
        "mean": f"avg({v})",
        "sum": f"sum({v})",
        "min": f"min({v})",
        "max": f"max({v})",
    }[agg]
    if agg != "mean" or typ.startswith("DECIMAL"):
        expr = f"CAST({expr} AS {typ})"
    return f"{expr} AS {v}"


def _generic_sql(inp: Input, target: str, agg: str, columns: list[str], values: list[str]) -> str:
    key, group_by = _group_key(inp)
    carried = [
        c for c in columns if c not in values and c not in (inp.time, inp.group, "absent", "timeframe")
    ]
    inner = [_agg(inp, c, agg) for c in values] + [f"last({qi(c)} {_ORDERED}) AS {qi(c)}" for c in carried]
    has_absent = "absent" in columns
    if has_absent:
        keep = "[" + ", ".join(ql(c) for c in carried) + "]"
        inner.append(
            f"list_filter(map_entries(last(absent {_ORDERED})), e -> list_contains({keep}, e.key)) AS _abs"
        )
    outer = []
    for c in columns:
        if c == inp.group:
            outer.append(f"_g AS {qi(c)}")
        elif c == inp.time:
            outer.append(f"{out_time('_b')} AS {qi(c)}")
        elif c == "timeframe" and inp.column(c).type == "VARCHAR":
            outer.append(f"{ql(target)} AS timeframe")
        elif c == "absent":
            outer.append(
                "CASE WHEN _abs IS NULL OR len(_abs) = 0 THEN NULL ELSE map_from_entries(_abs) END AS absent"
            )
        else:
            outer.append(qi(c))
    order = "_g, _b" if inp.group is not None else "_b"
    return (
        f"SELECT {', '.join(outer)} FROM (SELECT {key}time_bucket({tf_interval(target)}, _ts) AS _b, "
        f"{', '.join(inner)} FROM ({_src(inp, values)}) GROUP BY {group_by}) ORDER BY {order}"
    )


async def handler(ctx: ToolContext, args: ResampleArgs) -> ToolOutput:
    inp = resolve(ctx, TOOL, args.result_id, series_column=args.series_column)
    notes = list(inp.notes)
    target = args.timeframe
    columns = [c.name for c in inp.info.columns]
    is_bar = inp.model in BAR_MODELS
    values: list[str] = []
    with ctx.results.open([inp.rid]) as session:
        source = bar_timeframe(ctx, session, TOOL, inp)
        if source is not None:
            _check_target(inp, source, target)
        if is_bar:
            values = list(_BAR_VALUES)
            sql = _bar_sql(inp, target, columns, values)
        else:
            values = _value_columns(inp)
            if not values:
                raise refuse(
                    "no_value_columns",
                    f"{TOOL}: {inp.label} has no numeric value column to aggregate.",
                )
            sql = _generic_sql(inp, target, args.agg, columns, values)
        skipped, bad_columns = _non_finite(session, inp, values)
        table = session.query(sql)
    skipped_text = (
        f"{plural(skipped, 'row')} with a non-finite value (NaN or infinity) in {', '.join(bad_columns)}"
    )
    if table.num_rows == 0:
        raise refuse(
            "too_few_points",
            f"{TOOL}: {inp.label} has no row with a time to resample"
            + (f"; skipped {skipped_text}." if skipped else "."),
        )
    if skipped:
        notes.append(f"Skipped {skipped_text}.")
    absent: dict[str, AbsenceReason] = {}
    if is_bar:
        notes.append(BAR_RULES)
    else:
        notes.append(
            f"Resample: {', '.join(values)} aggregated with {args.agg} per series and {target} bucket (NULLs "
            "skipped; UTC-aligned, weeks start Monday 00:00 UTC; t = the bucket start); every other column is "
            "taken from the bucket's last row by time."
        )
        for c in values:
            if table.column(c).null_count:
                absent[c] = AbsenceReason(
                    code=AbsenceCode.NO_DATA, detail=f"None where a {target} bucket had no non-null {c}."
                )
        for c, reason in inp.info.absent.items():
            if c in columns and c not in values and c not in absent:
                absent[c] = reason
    prov = provenance(ctx, TOOL, [inp.info], args)
    cls = BUILTIN_MODELS.get(inp.model)
    if cls is not None:
        return model_output(table, cls, prov, notes=notes, absent=absent, inputs=[inp.info])
    return dynamic_output(
        ctx,
        TOOL,
        table,
        inp.model,
        prov,
        time_column=inp.time,
        group_column=inp.group,
        notes=notes,
        absent=absent,
        inputs=[inp.info],
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Resample",
    description=(
        "Resample a stored time series to a coarser timeframe, computed locally in DuckDB. Bars: open = first, "
        "high = max, low = min, close = last, volume = sum, trade_count = sum (None if any bar lacks it), vwap "
        "= volume-weighted (None when the volume is 0); the target must be coarser than the bars and hold whole "
        "bars. Other series (quotes, trades, snapshots, portfolio history, query results): each value column "
        "aggregated with agg (last, first, mean, sum, min, max) per series and bucket; other columns come from "
        "the bucket's last row. Rows with a NaN or infinite value are skipped. Buckets are UTC-aligned (weeks "
        "start Monday 00:00 UTC; t = bucket start). The output has the input's model. Large outputs are stored "
        "and you get a result_id."
    ),
    readme="Bars to a coarser timeframe (OHLCV rules stated in the notes); other series by one aggregation.",
    input_model=ResampleArgs,
    output_model=Bar,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_resample.py",
)

SPECS = (SPEC,)
