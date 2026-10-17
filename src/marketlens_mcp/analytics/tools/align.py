"""analytics_align: an as-of join of two stored results."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

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
    plural,
    provenance,
    qi,
    refuse,
    resolve,
    result_id_field,
    rows,
    scalar,
    store_field,
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
    by_left: str | None = Field(
        None,
        description="Left column to match within when the two names differ (with by_right; not with by).",
    )
    by_right: str | None = Field(
        None,
        description="Right column matched to by_left (e.g. by_left=ticker, by_right=series for returns).",
    )
    direction: Literal["backward", "forward"] = Field(
        "backward",
        description="backward: the latest right row at or before the left time; forward: the first at or after.",
    )
    tolerance: str | None = Field(
        None,
        description="Largest allowed time gap as an ISO-8601 duration (PT30S, PT5M, P1D; PT0S: the same time "
        "only); none by default.",
    )
    right_columns: list[str] | None = Field(
        None,
        description="Right columns to bring over. Default: every right column except its time, by and absent.",
    )
    suffix: str = Field(
        "_right", pattern=r"^_?[A-Za-z0-9_]{1,20}$", description="Added to right column names that collide."
    )
    match_on: Literal["available", "time"] = Field(
        "available",
        description="What right rows are matched on when the right result has a column with role available "
        "(when each row became known, e.g. a dataset's knowledge_time). available (default): point in time, "
        "each left row gets the right row with the latest time among those known at or before the left time "
        "(backward), or the first update known at or after it (forward), so no value reaches a time before "
        "it was published; tolerance is measured to when the row became known. A note says how much older the "
        "matches are than the latest right time by the time column; a result holding one version of each row "
        "(its latest) has a revised row only as known from its revision, so a match can be older than the value "
        "known then (a note says so when the result shows revisions). time: the right's time "
        "column, which can pair a left row with a value published after it (look-ahead; a note counts such "
        "rows). Without such a column the right's time column is always used.",
    )
    store: bool = store_field()


def _span(gap: dt.timedelta) -> str:
    seconds = gap.total_seconds()
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            x = round(seconds / size, 1)
            return f"{x:,g} {name}" + ("" if x == 1 else "s")
    return f"{seconds:g} seconds"


def _at(t: dt.datetime) -> str:
    """An instant as UTC text: the day alone at midnight (a naive time is UTC)."""
    t = t.astimezone(dt.UTC) if t.tzinfo else t
    return t.strftime("%Y-%m-%d" if t.time() == dt.time() else "%Y-%m-%dT%H:%MZ")


def _same(bl: str | None, br: str | None) -> str:
    if bl is None:
        return ""
    return f", same {bl}" if bl == br else f", same {bl} as {br}"


async def handler(ctx: ToolContext, args: AlignArgs) -> ToolOutput:
    left = resolve(ctx, TOOL, args.left_result_id)
    right = resolve(ctx, TOOL, args.right_result_id)
    tolerance = None
    if args.tolerance is not None:
        tolerance = parse_duration(args.tolerance)
        if tolerance is None and args.tolerance.lstrip().startswith("-"):
            raise refuse(
                "invalid_tolerance",
                f"{TOOL}: tolerance must be zero or positive; got {args.tolerance!r}. Use PT0S to match the same "
                "time only.",
            )
        if tolerance is None:
            raise refuse(
                "invalid_tolerance",
                f"{TOOL}: tolerance {args.tolerance!r} is not an ISO-8601 duration of weeks, days, hours, minutes "
                "or seconds (e.g. PT0S, PT30S, PT5M, PT1H30M, P1D); months and years are not fixed lengths.",
            )
    if args.by is not None and (args.by_left is not None or args.by_right is not None):
        raise refuse("invalid_arguments", f"{TOOL}: pass by, or by_left with by_right, not both.")
    if (args.by_left is None) != (args.by_right is None):
        raise refuse("invalid_arguments", f"{TOOL}: by_left and by_right go together; pass both.")
    bl, br = (args.by, args.by) if args.by is not None else (args.by_left, args.by_right)
    left_cols = [c.name for c in left.info.columns]
    right_cols = [c.name for c in right.info.columns]
    if bl is not None:
        for side, col in ((left, bl), (right, br)):
            if side.column(col) is None:
                raise refuse(
                    "unknown_column",
                    f"{TOOL}: by column '{col}' is not in {side.label}. Columns: "
                    f"{names(c.name for c in side.info.columns)}.",
                )
    if args.right_columns is None:
        chosen = [c for c in right_cols if c not in (right.time, br, "absent")]
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
    # Point in time: a right column with role available (when each row became
    # known) is the clock matched on, unless match_on=time.
    avail = right.available
    by_clock = avail is not None and args.match_on == "available"
    with ctx.results.open([left.rid, right.rid]) as session:
        if br is None and right.group is not None:
            n = scalar(session, f"SELECT count(DISTINCT {qi(right.group)}) FROM {right.table}")
            if n > 1:
                raise refuse(
                    "right_not_single_series",
                    f"{TOOL}: {right.label} has {n} series in column {right.group}; pass by (e.g. by={right.group}) "
                    "to match within each series, or keep one series with results_query first.",
                )
        # __rt: the right row's time; __rk: the clock matched on (its time, or
        # when it became known); __ra: when it became known, to count look-ahead.
        where = f"{qi(right.time)} IS NOT NULL" + (f" AND {qi(avail)} IS NOT NULL" if by_clock else "")
        clocks = f"{right.ts()} AS __rt, {right.ts(column=avail) if by_clock else right.ts()} AS __rk"
        if avail is not None and not by_clock:
            clocks += f", {right.ts(column=avail)} AS __ra"
        src = known = f"SELECT *, {clocks} FROM {right.table} WHERE {where}"
        if by_clock:
            # Keep only the rows that were the latest known of their series when
            # they became known (the latest time known then, a revision of an
            # older time being no update): an as-of match on __rk then gives the
            # latest time known at the left time, in its latest version then.
            series = [qi(br)] if br else []
            src = (
                f"SELECT * FROM ({src}) QUALIFY __rt >= max(__rt) OVER ("
                + (f"PARTITION BY {series[0]} " if series else "")
                + "ORDER BY __rk RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AND row_number() OVER "
                f"(PARTITION BY {', '.join([*series, '__rk'])} ORDER BY __rt DESC) = 1"
            )
        lts, rks = left.ts("l"), "r.__rk"
        cond = f"{lts} >= {rks}" if args.direction == "backward" else f"{lts} <= {rks}"
        on = (f"l.{qi(bl)} = r.{qi(br)} AND " if bl else "") + cond
        if tolerance is None:
            within = "r.__rk IS NOT NULL"
        else:
            gap = (
                f"epoch_us({lts}) - epoch_us({rks})"
                if args.direction == "backward"
                else f"epoch_us({rks}) - epoch_us({lts})"
            )
            within = f"r.__rk IS NOT NULL AND {gap} <= {int(tolerance.total_seconds() * 1_000_000)}"
        picks = [
            f"CASE WHEN {within} THEN r.{qi(c)} END AS {qi(n)}"
            for c, n in zip(chosen, out_names, strict=False)
        ]
        picks.append(f"CASE WHEN {within} THEN {out_time('r.__rt')} END AS {qi(out_names[-1])}")
        if avail is not None and not by_clock:
            picks.append(f"CASE WHEN {within} THEN r.__ra > {lts} END AS __late")
        alias = "l"
        sql = f"SELECT l.*, {', '.join(picks)} FROM {left.table} l ASOF LEFT JOIN ({src}) r ON {on}"
        if by_clock:
            # __newest: the latest right time at or before the clock the match
            # was made at (the left time; forward: when the matched row became
            # known) by the time column alone. Later than the match, it is a
            # row this result has only as known after that clock.
            at = lts if args.direction == "backward" else f"CASE WHEN {within} THEN r.__rk END"
            times = f"SELECT {f'{qi(br)}, ' if br else ''}__rt FROM ({known})"
            sql = (
                f"SELECT j.*, {out_time('e.__rt')} AS __newest FROM (SELECT l.*, {', '.join(picks)}, {at} AS __at "
                f"FROM {left.table} l ASOF LEFT JOIN ({src}) r ON {on}) j ASOF LEFT JOIN ({times}) e ON "
                + (f"j.{qi(bl)} = e.{qi(br)} AND " if bl else "")
                + "j.__at >= e.__rt"
            )
            alias = "j"
        order = ([f"{alias}.{qi(bl)}"] if bl else []) + [f"{alias}.{qi(left.time)}"]
        table = session.query(f"{sql} ORDER BY {', '.join(order)}")
        unknown = (
            scalar(
                session,
                f"SELECT count(*) FROM {right.table} WHERE {qi(right.time)} IS NOT NULL AND {qi(avail)} IS NULL",
            )
            if by_clock
            else 0
        )
        older: list[tuple[dt.timedelta, Any, Any, Any]] = []
        revised = total = 0
        if by_clock:
            older = [
                (n - m, t, m, n)
                for t, m, n in zip(
                    table.column(left.time).to_pylist(),
                    table.column(out_names[-1]).to_pylist(),
                    table.column("__newest").to_pylist(),
                    strict=True,
                )
                if m is not None and n is not None and n > m
            ]
            table = table.drop_columns(["__at", "__newest"])
        if older:
            # A result with one version of each time (the latest) has a
            # revised row only as its revision: a row that became known after
            # a later time of its series did shows one.
            key = f"{qi(br)}, __rt" if br else "__rt"
            one_version = (
                scalar(session, f"SELECT max(n) FROM (SELECT count(*) AS n FROM ({known}) GROUP BY {key})")
                == 1
            )
            series = f"PARTITION BY {qi(br)} " if br else ""
            if one_version:
                (counts,) = rows(
                    session,
                    f"SELECT count(*) FILTER (WHERE __rk > __next) AS revised, count(*) AS total FROM (SELECT __rk, "
                    f"min(__rk) OVER ({series}ORDER BY __rt DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) "
                    f"AS __next FROM ({known}))",
                )
                revised, total = counts["revised"], counts["total"]
    late = None
    if "__late" in table.column_names:
        late = table.column("__late").to_pylist().count(True)
        table = table.drop_columns(["__late"])
    unmatched = table.column(out_names[-1]).null_count
    notes.append(
        f"{unmatched} of {table.num_rows} left rows found no right row "
        f"({'known ' if by_clock else ''}{'at or before' if args.direction == 'backward' else 'at or after'} "
        f"their time{f', within {args.tolerance}' if args.tolerance else ''}{_same(bl, br)})."
    )
    if by_clock:
        how = (
            f"each left row gets the right row with the latest {right.time} known at or before its time, so "
            "none was published after it"
            if args.direction == "backward"
            else f"each left row gets the first update known at or after its time: the right row with the "
            f"latest {right.time} known then"
        )
        notes.append(
            f"Point in time: matched on when each row of {right.rid} became known ({avail}), not on its time "
            f"column {right.time}: {how}"
            + (f"; the tolerance is measured to {avail}" if args.tolerance else "")
            + f". match_on=time matches on {right.time} instead."
        )
        if unknown:
            notes.append(
                f"{plural(unknown, 'right row')} with no {avail} {'was' if unknown == 1 else 'were'} left out: "
                f"nothing says when {'it' if unknown == 1 else 'they'} became known."
            )
        if older:
            older.sort(key=lambda o: o[0])
            gap, t, m, n = older[-1]
            clock = "their time" if args.direction == "backward" else "when the matched row became known"
            notes.append(
                f"{len(older)} of {table.num_rows - unmatched} matched left rows got an older right row than the "
                f"latest {right.time} at or before {clock}, which {right.rid} has only as known after it: older by "
                f"a median of {_span(older[(len(older) - 1) // 2][0])} and at most {_span(gap)} (left {_at(t)}: "
                f"{right.time} {_at(m)} matched, {_at(n)} the latest)."
            )
        if revised:
            notes.append(
                f"{right.rid} holds one version of each {right.time}{f' per {br}' if br else ''}, and at least "
                f"{revised:,} of its {total:,} rows are revisions (known only after a later {right.time} was): a "
                "revised row's earlier releases are not there, so where a left row got an older right row, a newer one may have been known at its time in "
                "a version this result does not hold, the likelier the larger the gap. A result with every version "
                "of each row matches exactly."
            )
    elif late is not None:
        notes.append(
            f"match_on=time: matched on the time column {right.time} of {right.rid}, not on when its rows "
            f"became known ({avail}): {late} of {table.num_rows - unmatched} matched left rows got a right row "
            "known only after their time (look-ahead). match_on=available (the default) matches only rows "
            "known by then."
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
    # Every column keeps its unit (a renamed right column its source's), so a
    # price stays a price for the next tool (correlation turns it into returns),
    # and the model that computed it, so a drawdown is not taken for returns
    # once its result is gone.
    right_units, right_sources = right.units(), right.sources()
    units = left.units() | {
        n: right_units[c] for c, n in zip(chosen, out_names, strict=False) if c in right_units
    }
    units[out_names[-1]] = "UTC"
    sources = left.sources() | {
        n: right_sources[c] for c, n in zip(chosen, out_names, strict=False) if c in right_sources
    }
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
        units=units,
        sources=sources,
        store=args.store,
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="As-of align",
    description=(
        "As-of join of two stored time series, computed locally in DuckDB (ASOF LEFT JOIN): every left row is "
        "kept and gets the right row that is the latest at or before its time (direction backward) or the "
        "first at or after it (forward), optionally within a tolerance (ISO-8601 duration, e.g. PT5M; PT0S "
        "for exact time matches) and "
        "within the same `by` value (e.g. ticker; when the names differ, by_left and by_right, e.g. bars' ticker "
        "and returns' series). Output: the left columns, the chosen right columns (names "
        "that collide get the suffix, default _right) and matched_t, the matched right row's time; unmatched "
        "right values are None (no_match). Point in time: a right column with role available (when each row "
        "became known, e.g. a dataset's knowledge_time) is matched on, so a left row only gets a value known "
        "by its time (match_on=time: the right's time column, look-ahead counted in a note). Use it to line up "
        "series of different frequencies. Large outputs are stored and you get a result_id."
    ),
    readme="As-of join of two results (backward or forward, tolerance, by column; point in time on a column "
    "with role available); adds matched_t.",
    input_model=AlignArgs,
    output_model=ALIGNED_SCHEMA_NAME,
    provider=PROVIDER,
    route=f"duckdb:{TOOL}",
    handler=handler,
    golden_test="tests/analytics/test_analytics_align.py",
)

SPECS = (SPEC,)
