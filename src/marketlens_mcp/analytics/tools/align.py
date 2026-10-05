"""analytics_align: an as-of join of two stored results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import ALIGNED_SCHEMA_NAME
from marketlens_schema.base import AbsenceCode, AbsenceReason

from ..common import (
    CAPABILITY,
    PROVIDER,
    dynamic_output,
    names,
    out_time,
    parse_duration,
    provenance,
    qi,
    refuse,
    resolve,
    result_id_field,
    scalar,
)

TOOL = "analytics_align"
MATCHED = "matched_t"


class AlignArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_result_id: str = result_id_field(
        "The stored time series whose rows are kept (one output row per left row)."
    )
    right_result_id: str = result_id_field("The stored time series matched to each left row.")
    by: str | None = Field(None, description="Column present in both results to match within (e.g. ticker).")
    direction: Literal["backward", "forward"] = Field(
        "backward",
        description="backward: the latest right row at or before the left time; forward: the first at or after.",
    )
    tolerance: str | None = Field(
        None,
        description="Largest allowed time gap as an ISO-8601 duration (PT30S, PT5M, P1D); none by default.",
    )
    right_columns: list[str] | None = Field(
        None,
        description="Right columns to bring over. Default: every right column except its time, by and absent.",
    )
    suffix: str = Field(
        "_right", pattern=r"^_?[A-Za-z0-9_]{1,20}$", description="Added to right column names that collide."
    )


async def handler(ctx: ToolContext, args: AlignArgs) -> ToolOutput:
    left = resolve(ctx, TOOL, args.left_result_id)
    right = resolve(ctx, TOOL, args.right_result_id)
    tolerance = None
    if args.tolerance is not None:
        tolerance = parse_duration(args.tolerance)
        if tolerance is None:
            raise refuse(
                "invalid_tolerance",
                f"{TOOL}: tolerance {args.tolerance!r} is not an ISO-8601 duration of weeks, days, hours, minutes "
                "or seconds (e.g. PT30S, PT5M, PT1H30M, P1D); months and years are not fixed lengths.",
            )
    left_cols = [c.name for c in left.info.columns]
    right_cols = [c.name for c in right.info.columns]
    if args.by is not None:
        for side in (left, right):
            if side.column(args.by) is None:
                raise refuse(
                    "unknown_column",
                    f"{TOOL}: by column '{args.by}' is not in {side.label}. Columns: "
                    f"{names(c.name for c in side.info.columns)}.",
                )
    if args.right_columns is None:
        chosen = [c for c in right_cols if c not in (right.time, args.by, "absent")]
    else:
        missing = [c for c in args.right_columns if c not in right_cols]
        if missing:
            raise refuse(
                "unknown_column",
                f"{TOOL}: right column '{missing[0]}' is not in {right.label}. Columns: {names(right_cols)}.",
            )
        chosen = list(dict.fromkeys(args.right_columns))
    taken = set(left_cols)
    renamed: list[tuple[str, str]] = []
    out_names: list[str] = []
    for c in [*chosen, MATCHED]:
        name = c
        if name in taken:
            name = c + args.suffix
            if name in taken:
                raise refuse(
                    "column_collision",
                    f"{TOOL}: right column {c} renamed {name} still collides; pick a suffix.",
                )
            renamed.append((c, name))
        taken.add(name)
        out_names.append(name)
    notes = list(left.notes) + list(right.notes)
    with ctx.results.open([left.rid, right.rid]) as session:
        if args.by is None and right.group is not None:
            n = scalar(session, f"SELECT count(DISTINCT {qi(right.group)}) FROM {right.table}")
            if n > 1:
                raise refuse(
                    "right_not_single_series",
                    f"{TOOL}: {right.label} has {n} series in column {right.group}; pass by (e.g. by={right.group}) "
                    "to match within each series, or keep one series with results_query first.",
                )
        lts, rts = left.ts("l"), "r.__rts"
        cond = f"{lts} >= {rts}" if args.direction == "backward" else f"{lts} <= {rts}"
        on = (f"l.{qi(args.by)} = r.{qi(args.by)} AND " if args.by else "") + cond
        if tolerance is None:
            within = "r.__rts IS NOT NULL"
        else:
            gap = (
                f"epoch_us({lts}) - epoch_us({rts})"
                if args.direction == "backward"
                else f"epoch_us({rts}) - epoch_us({lts})"
            )
            within = f"r.__rts IS NOT NULL AND {gap} <= {int(tolerance.total_seconds() * 1_000_000)}"
        picks = [
            f"CASE WHEN {within} THEN r.{qi(c)} END AS {qi(n)}"
            for c, n in zip(chosen, out_names, strict=False)
        ]
        picks.append(f"CASE WHEN {within} THEN {out_time(rts)} END AS {qi(out_names[-1])}")
        order = ([f"l.{qi(args.by)}"] if args.by else []) + [f"l.{qi(left.time)}"]
        table = session.query(
            f"SELECT l.*, {', '.join(picks)} FROM {left.table} l ASOF LEFT JOIN "
            f"(SELECT *, {right.ts()} AS __rts FROM {right.table} WHERE {qi(right.time)} IS NOT NULL) r ON {on} "
            f"ORDER BY {', '.join(order)}"
        )
    unmatched = table.column(out_names[-1]).null_count
    notes.append(
        f"{unmatched} of {table.num_rows} left rows found no right row "
        f"({'at or before' if args.direction == 'backward' else 'at or after'} their time"
        f"{f', within {args.tolerance}' if args.tolerance else ''}{f', same {args.by}' if args.by else ''})."
    )
    if renamed:
        notes.append("Right columns renamed: " + ", ".join(f"{a} -> {b}" for a, b in renamed) + ".")
    notes.append(f"{out_names[-1]} is the time of the matched right row.")
    absent: dict[str, AbsenceReason] = {}
    for name in out_names:
        if table.column(name).null_count:
            absent[name] = AbsenceReason(
                code=AbsenceCode.NO_MATCH,
                detail=f"None where no right row matched; a matched row's own None keeps its reason in {right.rid}.",
            )
    for c, reason in left.info.absent.items():
        absent.setdefault(c, reason)
    prov = provenance(ctx, TOOL, [left.info, right.info], args)
    group = left.group if left.group in left_cols else None
    return dynamic_output(
        ctx,
        TOOL,
        table,
        ALIGNED_SCHEMA_NAME,
        prov,
        time_column=left.time,
        group_column=group,
        notes=notes,
        absent=absent,
        inputs=[left.info, right.info],
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="As-of align",
    description=(
        "As-of join of two stored time series, computed locally in DuckDB (ASOF LEFT JOIN): every left row is "
        "kept and gets the right row that is the latest at or before its time (direction backward) or the "
        "first at or after it (forward), optionally within a tolerance (ISO-8601 duration, e.g. PT5M) and "
        "within the same `by` value (e.g. ticker). Output: the left columns, the chosen right columns (names "
        "that collide get the suffix, default _right) and matched_t, the matched right row's time; unmatched "
        "right values are None (no_match). Use it to line up series of different frequencies. Large outputs "
        "are stored and you get a result_id."
    ),
    readme="As-of join of two results (backward or forward, tolerance, by column); adds matched_t.",
    input_model=AlignArgs,
    output_model=ALIGNED_SCHEMA_NAME,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_align.py",
)

SPECS = (SPEC,)
