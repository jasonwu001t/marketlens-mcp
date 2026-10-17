"""analytics_align: an as-of join of two stored results."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_mcp.results_api import (
    ResultInfo,
    RowsOf,
    known_model,
    known_span,
    period_rows,
    source_rows,
    unshown_known,
)
from marketlens_schema import ALIGNED_SCHEMA_NAME, CanonicalModel
from marketlens_schema.base import AbsenceCode, AbsenceReason

from ..common import (
    CAPABILITY,
    PROVIDER,
    Input,
    clocked_ancestor,
    derived_rows_of,
    dynamic_output,
    known_time,
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


def _unshown(info: ResultInfo) -> str:
    """Why a column named as when each row became known can lack its role."""
    if info.tool == "results_query":
        return (
            "a query answer keeps none where it cannot show when its rows became known: its query computes the "
            "clock, or rows from other rows, or reads a result that does not show it; results_query said why"
        )
    if info.parents:
        return f"{info.tool} gives its outputs none"
    return f"{info.tool} did not mark it"


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
        "a right row is usable from the later of its time and when it became known, and each left row gets the "
        "right row with the latest time at or before the left time among those known by then (backward), or "
        "the first update usable at or after it (forward), so no value reaches a time before it was published "
        "or a time before its own (a calendar row known in advance waits for its date); tolerance is measured "
        "to when the row became usable. A note says how much older the "
        "matches are than the latest right time by the time column; a result holding one version of each row "
        "(its latest) has a revised row only as known from its revision, so a match can be older than the value "
        "known then (a note says so when the result shows revisions). time: the right's time "
        "column, which can pair a left row with a value published after it (look-ahead; a note counts such "
        "rows). Without such a column, a knowledge_time or ingested_at column that has lost the role (a query "
        "answer that cannot show when its rows became known) is matched on the same way, as a lower bound, and a "
        "note says the match is not shown point in time. Fetched bars, a query answer passing them through and an "
        "analytics output of them (a return, a resampled bucket, a window) are matched from the end of the period "
        "each row covers, t plus its timeframe or bucket (and the lag its rows_of records when a bucket's last bar "
        "ends later), when its values became known (time: on t, the period's start), and a note says the match is "
        "not shown point in time when they hold prices adjusted for splits and dividends after their time "
        "(adjustment other than raw). A snapshot (market_snapshots, options_snapshots, options_chain) is matched "
        "from its fetch, as its values are as of then. A bucket of other rows (a dataset's resampled) is matched "
        "from its end, with a note that its rows are not shown point in time. A result that shows neither when "
        "its rows became known nor when their period ends (its rows_of not shown: a query's bucket dated at its "
        "start or window over dated rows, a join of bars, returns leaving a knowledge_time out, rows aligned "
        "forward or on time) is refused unless match_on=time. Else the right's time column is used, and a note "
        "says the match is not shown point in time unless the right holds fetched rows known at their time "
        "(quotes, trades; not a computed result, a plugin's rows dated by the period they describe, or orders or "
        "news, whose fields change later).",
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


@dataclass(frozen=True)
class _Rule:
    """How the notes name the rule a right result's rows became known by,
    after their time column: the end of their period, or their fetch."""

    noun: str  # bar, row
    end: str  # its end, t plus its timeframe
    end_paren: str  # its end (t plus its timeframe)
    why: str  # when its close became known
    known: str  # its end
    usable: str  # that ended (at or before its time)
    usable_fwd: str  # that ends (at or after its time)
    later: str  # that ended after (the left time)
    start: str  # , a bar's start,
    each_start: str  # each bar's start
    span: str | None  # the column naming each row's span, if any


def _rule(right: Input, cls: type[CanonicalModel] | None) -> _Rule | None:
    """The rule of ``known_time``: None for rows known at their time. ``cls``
    None: a bucket of rows not known by a rule (``period_rows``)."""
    info, t = right.info, right.time
    src = info.rows_of
    if cls is not None and cls.known_at_fetch:
        at = _at(src.fetched_at if src is not None else info.provenance.fetched_at)
        return _Rule(
            noun="row",
            end=f"its fetch at {at}",
            end_paren=f"its fetch ({at})",
            why="when the values it holds were current (a snapshot's open interest, greeks and session still "
            "trading are as of its fetch)",
            known="its fetch",
            usable="fetched",
            usable_fwd="fetched",
            later="fetched after",
            start="",
            each_start="the time it describes",
            span=None,
        )
    span = known_span(info, cls)
    lag = src.lag_seconds if src is not None else 0
    if span is None and not lag:
        return None
    column = (
        cls.span_column if cls is not None and cls.span_column in {c.name for c in info.columns} else None
    )
    # Bars as fetched, or passed through (their timeframe kept or left out).
    passed = info.tool == "results_query" or not info.parents
    if cls is not None and cls.span_column is not None and not lag and (column is not None or passed):
        return _Rule(
            noun="bar",
            end=f"its end, {t} plus its {cls.span_column}",
            end_paren=f"its end ({t} plus its {cls.span_column})",
            why="when its close became known",
            known="its end",
            usable="that ended",
            usable_fwd="that ends",
            later="that ended after",
            start=", a bar's start,",
            each_start="each bar's start",
            span=column,
        )
    covers = t + (f" plus its {column}" if column else f" plus {src.span}" if src and src.span else "")
    if lag:
        covers += f" and {_span(dt.timedelta(seconds=lag))} more, as late as one of the rows in it ends"
    return _Rule(
        noun="row",
        end=f"the end of the period it covers, {covers}",
        end_paren=f"the end of the period it covers ({covers})",
        why=f"when the {src.model if src else cls.schema_name} rows it was computed from had all become known"
        if cls is not None
        else "the earliest it can hold every row it covers",
        known="its end",
        usable="whose period ended",
        usable_fwd="whose period ends",
        later="whose period ended after",
        start=", the period's start,",
        each_start="each period's start",
        span=column,
    )


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
    # known) joins the clock matched on, unless match_on=time.
    proven = right.available
    # Without one, fetched market data (or a result recording the rows it holds
    # or was computed from, rows_of) is known by its model's rule: at its time
    # (quotes, trades), at the end of the period it covers (a bar, t plus its
    # timeframe; a bucket of them), at its fetch (a snapshot).
    known = known_model(right.info) if proven is None else None
    # A bucket of other rows (a dataset's) records its span: matched from its
    # end, the earliest it can hold every row it covers, though not shown point in time.
    period = period_rows(right.info) if proven is None else None
    ruled = known is not None or period is not None
    rule = _rule(right, known) if ruled else None
    hidden = right.info.rows_of if proven is None and not ruled else None
    if hidden is not None and args.match_on == "available":
        # Computed from dated rows in a way that shows neither: matched on its
        # time column (a bucket's start) it could take values known only later.
        raise refuse(
            "not_point_in_time",
            f"{TOOL}: {right.rid} was computed by {right.info.tool} from {hidden.model} rows (fetched at "
            f"{_at(hidden.fetched_at)}) in a way that shows neither when each of its rows became known nor when the "
            "period it covers ends (a bucket dated at its start, lead() or another window, a join of bars, rows "
            f"aligned forward, a knowledge_time left out), so matching on its time column {right.time} could pair a "
            "left row with values known only after it. Align onto the rows it was computed from (analytics_resample "
            f"records when each of its buckets ends), or pass match_on=time to match on {right.time} anyway, "
            "look-ahead included.",
        )
    # Else a column named as when each row became known that has lost its role
    # (a query answer that cannot show it) is matched on the same way, the safe
    # default, as a lower bound: the match is not called point in time.
    avail = proven if proven is not None else (right.unmarked_clock if not ruled else None)
    unshown = unshown_known(right.info) if avail is None else None
    if avail is not None:
        known_at = right.ts(column=avail)
    elif ruled:
        known_at = known_time(right, known)
    else:
        known_at = None
    by_clock = known_at is not None and args.match_on == "available"
    # Not matched on, but its look-ahead counted (match_on=time).
    seen = known_at
    # A clock that dates each row's own publication (not a bar's end): how
    # much older the matches are than the latest right time, and revisions.
    dated = by_clock and avail is not None
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
        # the later of its time and when it became known: a row is usable once
        # its time has come and it is known, so a row known before its time, a
        # calendar's, waits for it); __ra: when it became known, to count look-ahead.
        where = f"{qi(right.time)} IS NOT NULL" + (f" AND {known_at} IS NOT NULL" if by_clock else "")
        clock = f"greatest({right.ts()}, {known_at})" if by_clock else right.ts()
        clocks = f"{right.ts()} AS __rt, {clock} AS __rk"
        if seen is not None and not by_clock:
            clocks += f", {seen} AS __ra"
        src = base = f"SELECT *, {clocks} FROM {right.table} WHERE {where}"
        if by_clock:
            # Keep only the rows that were the latest usable of their series when
            # they became usable (the latest time usable then, a revision of an
            # older time being no update): an as-of match on __rk then gives the
            # latest time at or before the left time known then, in its latest
            # version then.
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
        if seen is not None and not by_clock:
            picks.append(f"CASE WHEN {within} THEN r.__ra > {lts} END AS __late")
        alias = "l"
        sql = f"SELECT l.*, {', '.join(picks)} FROM {left.table} l ASOF LEFT JOIN ({src}) r ON {on}"
        if dated:
            # __newest: the latest right time at or before the clock the match
            # was made at (the left time; forward: when the matched row became
            # usable) by the time column alone. Later than the match, it is a
            # row this result has only as known after that clock.
            at = lts if args.direction == "backward" else f"CASE WHEN {within} THEN r.__rk END"
            times = f"SELECT {f'{qi(br)}, ' if br else ''}__rt FROM ({base})"
            sql = (
                f"SELECT j.*, {out_time('e.__rt')} AS __newest FROM (SELECT l.*, {', '.join(picks)}, {at} AS __at "
                f"FROM {left.table} l ASOF LEFT JOIN ({src}) r ON {on}) j ASOF LEFT JOIN ({times}) e ON "
                + (f"j.{qi(bl)} = e.{qi(br)} AND " if bl else "")
                + "j.__at >= e.__rt"
            )
            alias = "j"
        order = ([f"{alias}.{qi(bl)}"] if bl else []) + [f"{alias}.{qi(left.time)}"]
        table = session.query(f"{sql} ORDER BY {', '.join(order)}")
        # Each output row holds its left row and a right row known by then
        # (backward, matched from when it became known or by its time when that
        # is when it was): it is known when its left row is; else it shows not
        # (matched forward or on its time, or a right row that shows not), and
        # aligning onto it is refused unless match_on=time.
        own = derived_rows_of(session, [left], keeps_clock=True)
        settled = args.direction == "backward" and (
            (proven is not None and by_clock)
            or (known is not None and unshown is None and (by_clock or known_at is None))
        )
        if not settled:
            of = (
                own
                or source_rows(right.info)
                or RowsOf(model=right.model, fetched_at=right.info.provenance.fetched_at)
            )
            own = of.model_copy(update={"span": None, "lag_seconds": 0, "shown": False})
        unknown = (
            scalar(
                session,
                f"SELECT count(*) FROM {right.table} WHERE {qi(right.time)} IS NOT NULL AND {known_at} IS NULL",
            )
            if by_clock
            else 0
        )
        older: list[tuple[dt.timedelta, Any, Any, Any]] = []
        revised = total = 0
        if dated:
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
                scalar(session, f"SELECT max(n) FROM (SELECT count(*) AS n FROM ({base}) GROUP BY {key})")
                == 1
            )
            series = f"PARTITION BY {qi(br)} " if br else ""
            if one_version:
                (counts,) = rows(
                    session,
                    f"SELECT count(*) FILTER (WHERE __rk > __next) AS revised, count(*) AS total FROM (SELECT __rk, "
                    f"min(__rk) OVER ({series}ORDER BY __rt DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) "
                    f"AS __next FROM ({base}))",
                )
                revised, total = counts["revised"], counts["total"]
    late = None
    if "__late" in table.column_names:
        late = table.column("__late").to_pylist().count(True)
        table = table.drop_columns(["__late"])
    unmatched = table.column(out_names[-1]).null_count
    notes.append(
        f"{unmatched} of {table.num_rows} left rows found no right row "
        f"({'usable ' if by_clock else ''}{'at or before' if args.direction == 'backward' else 'at or after'} "
        f"their time{f', within {args.tolerance}' if args.tolerance else ''}{_same(bl, br)})."
    )
    if by_clock and proven is not None:
        how = (
            f"each left row gets the right row with the latest {right.time} at or before its time among those "
            "known by then, so none was published after it or is dated after it"
            if args.direction == "backward"
            else f"each left row gets the first update usable at or after its time: the right row with the "
            f"latest {right.time} usable then"
        )
        notes.append(
            f"Point in time: each row of {right.rid} is matched from when it became usable, the later of its "
            f"time column {right.time} and when it became known ({avail}): {how}"
            + ("; the tolerance is measured to when it became usable" if args.tolerance else "")
            + f". match_on=time matches on {right.time} alone instead."
        )
    elif dated:
        notes.append(
            f"Not shown point in time: {avail} of {right.rid} is named as when each row became known but has no "
            f"role available ({_unshown(right.info)}), so nothing shows a row was known by then. Each row was matched all the "
            f"same from the later of its time column {right.time} and {avail}, the safe default, which keeps out "
            f"the values {avail} shows were published after the left time"
            + ("; the tolerance is measured to that" if args.tolerance else "")
            + f", but a value computed from rows known later can still hold data published after it. match_on=time "
            f"matches on {right.time} alone instead."
        )
    elif by_clock:
        how = (
            f"each left row gets the {rule.noun} with the latest {right.time} {rule.usable} at or before its time"
            if args.direction == "backward"
            else f"each left row gets the first {rule.noun} {rule.usable_fwd} at or after its time"
        )
        tol = f"; the tolerance is measured to {rule.known}" if args.tolerance else ""
        instead = f". match_on=time matches on {right.time}{rule.start} instead."
        notes.append(
            f"Point in time: each {rule.noun} of {right.rid} is matched from {rule.end}, {rule.why}: {how}{tol}{instead}"
            if unshown is None
            else f"Not shown point in time: {right.rid} {unshown}. Each {rule.noun} was matched all the same from "
            f"{rule.end}, the safe default, which keeps out the {rule.noun}s {rule.later} the left time{tol}{instead}"
        )
    if by_clock:
        if unknown and avail is None:
            notes.append(
                f"{plural(unknown, 'right row')} whose {rule.span} is not a timeframe {'was' if unknown == 1 else 'were'} "
                f"left out: nothing says when {'it' if unknown == 1 else 'they'} ended."
            )
        elif unknown:
            notes.append(
                f"{plural(unknown, 'right row')} with no {avail} {'was' if unknown == 1 else 'were'} left out: "
                f"nothing says when {'it' if unknown == 1 else 'they'} became known."
            )
        if older:
            older.sort(key=lambda o: o[0])
            gap, t, m, n = older[-1]
            clock = "their time" if args.direction == "backward" else "when the matched row became usable"
            notes.append(
                f"{len(older)} of {table.num_rows - unmatched} matched left rows got an older right row than the "
                f"latest {right.time} at or before {clock}, which {right.rid} has only as known after it: older by "
                f"a median of {_span(older[(len(older) - 1) // 2][0])} and at most {_span(gap)} (left {_at(t)}: "
                f"{right.time} {_at(m)} matched, {_at(n)} the latest)."
            )
        if revised:
            notes.append(
                f"{right.rid} holds one version of each {right.time}{f' per {br}' if br else ''}, and at least "
                f"{revised:,} of its {total:,} rows are revisions (usable only after a later {right.time} was): a "
                "revised row's earlier releases are not there, so where a left row got an older right row, a newer one may have been known at its time in "
                "a version this result does not hold, the likelier the larger the gap. A result with every version "
                "of each row matches exactly."
            )
    elif late is not None and avail is None:
        notes.append(
            f"match_on=time: matched on the time column {right.time} of {right.rid}, {rule.each_start}, not "
            f"{rule.end_paren}, {rule.why}: {late} of {table.num_rows - unmatched} matched left rows got a "
            f"{rule.noun} {rule.later} their time (look-ahead). match_on=available (the default) matches each "
            f"{rule.noun} from {rule.known}."
        )
    elif late is not None and proven is None:
        notes.append(
            f"match_on=time: matched on the time column {right.time} of {right.rid} alone: at least {late} of "
            f"{table.num_rows - unmatched} matched left rows got a right row with {avail} after their time "
            f"(look-ahead). {avail} is named as when each row became known but has no role available "
            f"({_unshown(right.info)}), "
            f"so this counts by {avail} alone: a value computed from rows known later can hold data published after "
            "the left time too. match_on=available (the default) matches from the later of the two."
        )
    elif late is not None:
        notes.append(
            f"match_on=time: matched on the time column {right.time} of {right.rid}, not on when its rows "
            f"became known ({avail}): {late} of {table.num_rows - unmatched} matched left rows got a right row "
            "known only after their time (look-ahead). match_on=available (the default) matches only rows "
            "known by then."
        )
    # Without a clock, only fetched rows known by their time column (quotes,
    # trades; bars and buckets at their end, snapshots at their fetch, said
    # above), or rows computed from them recording so, show the match point in time.
    if unshown is not None and not (rule is not None and by_clock):
        origin = clocked_ancestor(ctx, right.info)
        notes.append(
            f"Not shown point in time: {right.rid} {unshown}; its rows were matched on the time column {right.time} "
            "alone, which can pair a left row with a value published after it."
            + (
                f" It was computed from {origin[0]}, whose {origin[1]} has that role: keep {origin[1]} in a query "
                "answer (passed through, or its max()) to match point in time."
                if origin
                else ""
            )
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
        rows_of=own,
    )


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="As-of align",
    description=(
        "As-of join of two stored time series: every left row is kept and gets the right row that is the latest "
        "at or before its time (direction backward) or the first at or after it (forward), optionally within a "
        "tolerance (ISO-8601, e.g. PT5M; PT0S: the same time) and the same `by` value (e.g. ticker; "
        "by_left/by_right if names differ). Output: the left columns, the chosen right columns (colliding names "
        "get suffix, default _right) and matched_t, the matched right row's time; unmatched right values are None. "
        "Point in time: a right row counts from the later of its time and a column with role available (e.g. "
        "knowledge_time) or a knowledge_time without it; a bar or an analytics output of bars from its period's "
        "end; a snapshot from its fetch; a computed result showing neither is refused; notes say when a "
        "match is not shown point in time (match_on=time: the time column alone, look-ahead counted). Lines up "
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
