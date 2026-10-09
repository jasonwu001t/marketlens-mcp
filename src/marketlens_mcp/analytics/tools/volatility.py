"""analytics_volatility: rolling annualised volatility of each series."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_mcp.results_api import ResultInfo, ResultNotFound, ResultSession
from marketlens_schema.analytics import ALIGNED_SCHEMA_NAME, QUERY_ROW_SCHEMA_NAME, VolatilityPoint
from marketlens_schema.base import TIMEFRAME_RE

from ..common import (
    CAPABILITY,
    PROVIDER,
    RATIO_UNITS,
    RETURNS_MODEL,
    Input,
    adjustment_note,
    bar_timeframe,
    check_unique_times,
    default_periods_per_year,
    model_output,
    names,
    out_time,
    plural,
    points_sql,
    provenance,
    ql,
    refuse,
    resolve,
    result_id_field,
    returns_sql,
    rows,
    scalar,
    series_counts,
    skipped_notes,
    store_field,
    tf_interval,
    tf_parts,
    tf_seconds,
    value_column,
)

TOOL = "analytics_volatility"


class VolatilityArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = result_id_field(
        "A stored result of prices, or of returns (from analytics_returns, or your own, e.g. a portfolio's)."
    )
    window: Annotated[int, Field(ge=2, le=2520)] | Literal["all"] = Field(
        21,
        description="Returns per rolling window (2-2520), or all: one row per series, the volatility of all its "
        "returns (full sample).",
    )
    return_kind: Literal["log", "simple"] = Field(
        "log",
        description="Returns computed from prices (ignored for a returns result, which keeps its kind).",
    )
    periods_per_year: float | None = Field(
        None,
        gt=0,
        allow_inf_nan=False,
        description="Annualisation factor. Default from the bar timeframe and market: US equity 1d 252, 1w 52, "
        "1mo 12, 1h 1638 and Nmin 98280/N (6.5-hour sessions; when the bars include extended hours, 252 x the "
        "median bars a New York day, up to 16 hours: 1h 4032, Nmin 241920/N); crypto 1d 365, Nh 8760/N, Nmin "
        "525600/N. A query answer takes the timeframe of the results it was computed from. Observations much "
        "further apart than the timeframe (a weekly sample of daily bars), or with no timeframe at all, use their "
        "own number per year; required when there are fewer than 6 of them.",
    )
    price_column: str | None = Field(
        None,
        description="Numeric column of prices (or returns). Default: the model's first value column (ret for "
        "returns).",
    )
    input_kind: Literal["prices", "returns"] | None = Field(
        None,
        description="What price_column holds: prices (returns are computed from them) or returns (used as they "
        "are, e.g. a portfolio's daily returns). Default: returns for a returns result, or for a column in "
        "fraction units of a query answer or aligned result; else prices.",
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )
    store: bool = store_field()


_NEW_YORK = "America/New_York"
_YEAR_SECONDS = 365.25 * 86400
#: Fewest gaps between observations for their median spacing to overrule (or stand in for) the timeframe.
_MIN_GAPS = 5
#: Results computed by results_query or analytics_align: their columns come from the results they were computed from.
_DYNAMIC = (QUERY_ROW_SCHEMA_NAME, ALIGNED_SCHEMA_NAME)


def _span(seconds: float) -> str:
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds >= size:
            x = seconds / size
            return f"{x:.3g} {name}" + ("" if x == 1 else "s")
    return f"{seconds:.3g} seconds"


def _spacing(session: ResultSession, rets: str) -> tuple[float | None, int]:
    """The median time between consecutive observations of a series, in
    seconds, and how many such gaps there are."""
    (row,) = rows(
        session,
        "SELECT median(d) AS m, count(d) AS n FROM (SELECT epoch(ts) - epoch(lag(ts) OVER (PARTITION BY s "
        f"ORDER BY ts)) AS d FROM ({rets}))",
    )
    return row["m"], row["n"]


def _lineage(
    ctx: ToolContext, info: ResultInfo, find: Callable[[ResultInfo], set[str] | None], depth: int = 3
) -> set[str] | None:
    """What ``find`` reads from the results ``info`` was computed from, looking
    further back through returns results, query answers and aligned results;
    None when a parent is gone or tells nothing."""
    found: set[str] = set()
    for parent in info.parents:
        try:
            p = ctx.results.info(parent)
        except ResultNotFound:
            return None
        got = find(p)
        if got is None and depth > 1 and (p.model == RETURNS_MODEL or p.model in _DYNAMIC):
            got = _lineage(ctx, p, find, depth - 1)
        if got is None:
            return None
        found |= got
    return found or None


def _column_values(info: ResultInfo, name: str) -> set[str] | None:
    """The values a stored text column spans (its min and max)."""
    c = next((c for c in info.columns if c.name == name), None)
    return {c.min, c.max} if c is not None and isinstance(c.min, str) and isinstance(c.max, str) else None


def _asset_classes(ctx: ToolContext, session: ResultSession, inp: Input) -> set[str]:
    """The input's asset classes: its asset_class column, else the one of the
    results it was computed from (a returns result's prices, a query answer's
    source)."""
    col = inp.column("asset_class")
    if col is not None and col.type == "VARCHAR":
        return {
            r["a"]
            for r in rows(
                session, f"SELECT DISTINCT asset_class AS a FROM {inp.table} WHERE asset_class IS NOT NULL"
            )
        }
    return _lineage(ctx, inp.info, lambda p: _column_values(p, "asset_class")) or set()


def _every_day(ctx: ToolContext, session: ResultSession, inp: Input, rets: str) -> bool:
    """Whether the series trade every day around the clock (crypto): by asset
    class, else by series named BASE/QUOTE. A mix is refused."""
    classes = _asset_classes(ctx, session, inp)
    if not classes:
        return bool(scalar(session, f"SELECT bool_and(contains(s, '/')) FROM ({rets})"))
    if "crypto" in classes and len(classes) > 1:
        raise refuse(
            "periods_per_year_required",
            f"{TOOL} cannot annualise {inp.label}: it mixes crypto, which trades every day, with "
            f"{names(sorted(classes - {'crypto'}))}; pass periods_per_year, or keep one asset class with "
            "results_query (store=true).",
        )
    return classes == {"crypto"}


def _extended_hours(session: ResultSession, rets: str, tf: str) -> bool:
    """Whether a bar lies wholly outside 09:30-16:00 New York (pre-market or after hours)."""
    local = f"timezone('{_NEW_YORK}', timezone('UTC', ts))"
    return bool(
        scalar(
            session,
            f"SELECT count(*) FROM ({rets}) WHERE CAST({local} AS TIME) >= TIME '16:00' "
            f"OR CAST({local} + {tf_interval(tf)} AS TIME) <= TIME '09:30'",
        )
    )


def _per_day(session: ResultSession, rets: str, *, crypto: bool) -> float:
    """The median number of returns a series has in a day: a UTC day for
    crypto, else a New York day (a session with its extended hours)."""
    day = "CAST(ts AS DATE)" if crypto else f"CAST(timezone('{_NEW_YORK}', timezone('UTC', ts)) AS DATE)"
    return scalar(
        session, f"SELECT median(n) FROM (SELECT s, {day} AS d, count(*) AS n FROM ({rets}) GROUP BY s, d)"
    )


def _observed(session: ResultSession, rets: str, *, intraday: bool, crypto: bool) -> tuple[float, str]:
    """periods_per_year counted from the observations themselves, and how."""
    if intraday:
        per_day = _per_day(session, rets, crypto=crypto)
        days = 365 if crypto else 252
        return days * per_day, (
            f"{days} {'days' if crypto else 'sessions'} a year x the median {per_day:g} observations a "
            f"{'UTC' if crypto else 'New York'} day"
        )
    (row,) = rows(
        session,
        "SELECT CAST(sum(n - 1) AS DOUBLE) AS k, sum(span) AS span FROM (SELECT count(*) AS n, "
        f"epoch(max(ts)) - epoch(min(ts)) AS span FROM ({rets}) GROUP BY s HAVING count(*) >= 2)",
    )
    return float(
        max(1, round(row["k"] / row["span"] * _YEAR_SECONDS))
    ), "their number per year over the time they span"


def _timeframe(info: ResultInfo) -> set[str] | None:
    """A stored result's single bar timeframe: its timeframe column, or a returns result's period."""
    found = _column_values(info, "period" if info.model == RETURNS_MODEL else "timeframe")
    return found if found is not None and len(found) == 1 and TIMEFRAME_RE.match(next(iter(found))) else None


def _annualisation(ctx: ToolContext, session: ResultSession, inp: Input, rets: str) -> tuple[float, str]:
    """The default periods_per_year and the note that says where it came from:
    the bar timeframe by market (crypto every day; US equity sessions, counted
    up to 16 hours when the bars include extended hours), or, when the
    observations are much further apart than the timeframe says (a weekly
    sample of daily bars kept as 1d) or there is no timeframe, their own
    number per year."""
    tf = bar_timeframe(ctx, session, TOOL, inp)
    source = inp.rid
    labelled = f"{inp.rid} is labelled {tf}"
    if tf is None and inp.model in _DYNAMIC:
        inherited = _lineage(ctx, inp.info, _timeframe)
        if inherited is not None and len(inherited) == 1:
            (tf,) = inherited
            source = f"the results {inp.rid} was computed from"
            labelled = f"{inp.rid} was computed from {tf} results"
    spacing, gaps = _spacing(session, rets)
    counted = spacing is not None and gaps >= _MIN_GAPS
    if tf is None and not counted:
        apart = f" (its observations are a median {_span(spacing)} apart)" if spacing else ""
        raise refuse(
            "periods_per_year_required",
            f"{TOOL} cannot annualise: {inp.label} has no single bar timeframe{apart}, and its returns are too "
            f"few to count per year ({plural(gaps, 'interval')} between them, at least {_MIN_GAPS}); pass "
            "periods_per_year (252 for daily equity bars, 365 for daily crypto bars, 52 for weekly).",
        )
    if tf is None or (counted and spacing >= 2 * tf_seconds(tf)):
        intraday = spacing < 86400 if tf is None else tf_parts(tf)[1] in ("min", "h")
        crypto = intraday and _every_day(ctx, session, inp, rets)
        ppy, how = _observed(session, rets, intraday=intraday, crypto=crypto)
        why = (
            f"{inp.rid} has no bar timeframe, and its observations are a median {_span(spacing)} apart"
            if tf is None
            else f"{labelled}, but its observations are a median {_span(spacing)} apart (sampled, or with missing bars)"
        )
        return ppy, f"periods_per_year = {ppy:g}: {why}, so it is {how}; pass periods_per_year to set it."
    _, unit = tf_parts(tf)
    intraday = unit in ("min", "h")
    crypto = unit in ("min", "h", "d") and _every_day(ctx, session, inp, rets)
    ppy = default_periods_per_year(tf, crypto=crypto)
    if ppy is None:
        raise refuse(
            "periods_per_year_required",
            f"{TOOL} cannot annualise: the {tf} timeframe of {inp.label} has no default; pass periods_per_year "
            "(252 for daily equity bars, 365 for daily crypto bars).",
        )
    if unit in ("w", "mo"):
        basis = ""
    elif crypto:
        basis = "crypto trades every day: 365 days a year" + (" of 24 hours" if intraday else "") + "; "
    elif intraday and _extended_hours(session, rets, tf):
        # Counted, not assumed: a few after-hours bars a day are not 16 hours of trading.
        regular, most = ppy, default_periods_per_year(tf, extended=True)
        per_day = _per_day(session, rets, crypto=False)
        ppy = min(max(252 * per_day, regular), most)
        basis = (
            f"US equity with extended-hours bars: 252 sessions a year x the median {per_day:g} bars a New York "
            f"day, at least the {regular:g} of 6.5-hour sessions and at most the {most:g} of 16 hours, "
            "04:00-20:00; "
        )
    else:
        basis = (
            "US equity: 252 sessions a year"
            + (" of 6.5 hours, 09:30-16:00 New York" if intraday else "")
            + "; "
        )
    return (
        ppy,
        f"periods_per_year = {ppy:g} from the {tf} timeframe of {source} ({basis}pass periods_per_year to set it).",
    )


def _source_model(ctx: ToolContext, inp: Input, col: str) -> str:
    """The model whose column ``col`` is: the parent's, when a query answer
    over one result kept that column; else the input's own."""
    if inp.model == QUERY_ROW_SCHEMA_NAME and len(inp.info.parents) == 1:
        try:
            parent = ctx.results.info(inp.info.parents[0])
        except ResultNotFound:
            return inp.model
        if any(c.name == col for c in parent.columns):
            return parent.model
    return inp.model


def _as_returns(ctx: ToolContext, inp: Input, args: VolatilityArgs) -> tuple[str, bool]:
    """The column and whether it holds returns: input_kind, else a returns
    result, or a column in fraction units of a query answer or aligned result
    (a drawdown or beta column of those models is a ratio, not a return). A
    ratio column is not prices."""
    given = args.price_column
    if given is None and (inp.model == RETURNS_MODEL or args.input_kind == "returns") and inp.column("ret"):
        given = "ret"
    col = value_column(TOOL, inp, given, param="price_column")
    unit = inp.column(col).unit
    if args.input_kind is not None:
        as_returns = args.input_kind == "returns"
    else:
        model = _source_model(ctx, inp, col)
        as_returns = model == RETURNS_MODEL or (unit == "fraction" and model in _DYNAMIC)
    if not as_returns and unit in RATIO_UNITS:
        raise refuse(
            "not_prices",
            f"{TOOL}: '{col}' of {inp.label} is a ratio (unit {unit}), not a price. Pass input_kind=returns if it "
            "holds returns, or a price column.",
        )
    return col, as_returns


async def handler(ctx: ToolContext, args: VolatilityArgs) -> ToolOutput:
    inp = resolve(ctx, TOOL, args.result_id, series_column=args.series_column)
    notes = list(inp.notes)
    w = args.window
    with ctx.results.open([inp.rid]) as session:
        col, as_returns = _as_returns(ctx, inp, args)
        if as_returns:
            has_kind = inp.column("kind") is not None and inp.column("kind").type == "VARCHAR"
            kinds = (
                [r["k"] for r in rows(session, f"SELECT DISTINCT kind AS k FROM {inp.table} ORDER BY 1")]
                if has_kind
                else []
            )
            kind = kinds[0] if len(kinds) == 1 else args.return_kind
            if kinds and "return_kind" in args.model_fields_set and args.return_kind != kind:
                notes.append(f"return_kind ignored: {inp.rid} already holds {kind} returns.")
            if inp.model != RETURNS_MODEL:
                why = "input_kind=returns" if args.input_kind else f"{col} is in fraction units"
                notes.append(
                    f"{why}, so the values of {col} were taken as returns as they are"
                    + ("." if kinds else f"; return_kind={kind} only labels them.")
                )
            notes += skipped_notes(session, inp, col, positive=False)
            points = points_sql(inp, col, positive=False)
            check_unique_times(session, TOOL, inp, points)
            rets = f"SELECT s, ts, v AS r FROM ({points})"
        else:
            kind = args.return_kind
            hint = (
                f"If {col} holds returns, pass input_kind=returns."
                if inp.column(col).unit is None and args.input_kind is None
                else None
            )
            notes += skipped_notes(session, inp, col, positive=True, hint=hint)
            points = points_sql(inp, col, positive=True)
            check_unique_times(session, TOOL, inp, points)
            rets = returns_sql(points, kind=kind)
        if args.periods_per_year is None:
            ppy, note = _annualisation(ctx, session, inp, rets)
        else:
            ppy, note = args.periods_per_year, f"periods_per_year = {args.periods_per_year:g} as given."
        notes.append(note)
        counts = series_counts(session, rets)
        need = 2 if w == "all" else w
        if w == "all":
            # One window per series: all of its returns.
            stats = f"SELECT s, max(ts) AS ts, stddev_samp(r) AS sd, count(r) AS n, count(r) AS w FROM ({rets}) GROUP BY s"
        else:
            stats = (
                f"SELECT s, ts, stddev_samp(r) OVER win AS sd, count(r) OVER win AS n, {w} AS w FROM ({rets}) "
                f"WINDOW win AS (PARTITION BY s ORDER BY ts ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW)"
            )
        table = session.query(
            f"SELECT s AS series, {out_time('ts')} AS t, sd * sqrt(CAST(? AS DOUBLE)) AS volatility, "
            f'CAST(w AS BIGINT) AS "window", CAST(? AS DOUBLE) AS periods_per_year, {ql(kind)} AS return_kind, '
            f"NULL::MAP(VARCHAR, VARCHAR) AS absent FROM ({stats}) WHERE n >= {need} AND n = w ORDER BY series, t",
            [ppy, ppy],
        )
    if table.num_rows == 0:
        raise refuse(
            "too_few_points",
            f"{TOOL} needs at least {need} returns in one series (window={w}); the longest series of {inp.label} "
            f"has {plural(max(counts.values(), default=0), 'return')}. Use a smaller window or more data.",
        )
    short = [s for s, n in counts.items() if n < need]
    if short:
        notes.append(f"Series with fewer than {need} returns are omitted: {names(short)}.")
    if w == "all":
        notes.append(
            f"window=all: volatility = stddev_samp of all {kind} returns of each series x sqrt({ppy:g}), at its last "
            "observation; window is the number of returns."
        )
    else:
        notes.append(
            f"volatility_t = stddev_samp of the last {w} {kind} returns x sqrt({ppy:g}), at the window's last "
            "observation; rows before the window fills are omitted."
        )
    note = adjustment_note(inp)
    if note:
        notes.append(note)
    prov = provenance(ctx, TOOL, [inp.info], args)
    return model_output(table, VolatilityPoint, prov, notes=notes, inputs=[inp.info], store=args.store)


SPEC = ToolSpec(
    name=TOOL,
    capability=CAPABILITY,
    title="Rolling volatility",
    description=(
        "Rolling annualised volatility of each series in a stored result of prices (or of returns: from "
        "analytics_returns, a column in fraction units, or any column with input_kind=returns), computed locally in "
        "DuckDB: vol_t = stddev_samp(returns over the last `window` rows) x sqrt(periods_per_year), reported at "
        "the window's last observation; rows before the window fills are omitted; window=all gives one row per "
        "series over all its returns. return_kind log (default) or simple for prices. periods_per_year defaults from the "
        "bar timeframe and market (US equity 1d 252, 1w 52, 1mo 12, 1h 1638, Nmin 98280/N, with extended-hours bars "
        "252 x the bars a day up to 16 hours; crypto 1d 365, Nmin 525600/N), or from the data when its observations "
        "are much further apart than the timeframe or it has none; the notes say which. Volatility is a fraction "
        "per year (0.2 = 20 %). "
        "Large outputs are stored and you get a result_id."
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
