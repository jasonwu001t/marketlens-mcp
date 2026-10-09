"""analytics_volatility: rolling annualised volatility of each series."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_mcp.results_api import ResultNotFound, ResultSession
from marketlens_schema.analytics import VolatilityPoint

from ..common import (
    CAPABILITY,
    PROVIDER,
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
    price_column,
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
    tf_interval,
    tf_parts,
    tf_seconds,
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
        description="Annualisation factor. Default from the bar timeframe and market: US equity 1d 252, 1w 52, "
        "1mo 12, 1h 1638 and Nmin 98280/N (6.5-hour sessions; 16 hours, 1h 4032 and Nmin 241920/N, when the bars "
        "include extended hours); crypto 1d 365, Nh 8760/N, Nmin 525600/N. Observations much further apart than "
        "the timeframe (a weekly sample of daily bars) use their own number per year. Required without a timeframe.",
    )
    price_column: str | None = Field(
        None, description="Numeric column of prices (or returns). Default: the model's first value column."
    )
    series_column: str | None = Field(
        None, description="Column naming each series. Default: the result's group column."
    )


_NEW_YORK = "America/New_York"
_YEAR_SECONDS = 365.25 * 86400
#: Fewest gaps between observations for their median spacing to overrule the timeframe.
_MIN_GAPS = 5


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


def _asset_classes(ctx: ToolContext, session: ResultSession, inp: Input) -> set[str]:
    """The input's asset classes: its asset_class column, else the one of the
    result it came from (a returns result's prices)."""
    col = inp.column("asset_class")
    if col is not None and col.type == "VARCHAR":
        return {
            r["a"]
            for r in rows(
                session, f"SELECT DISTINCT asset_class AS a FROM {inp.table} WHERE asset_class IS NOT NULL"
            )
        }
    for parent in inp.info.parents:
        try:
            pinfo = ctx.results.info(parent)
        except ResultNotFound:
            continue
        c = next((c for c in pinfo.columns if c.name == "asset_class"), None)
        if c is not None and isinstance(c.min, str) and isinstance(c.max, str):
            return {c.min, c.max}
    return set()


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


def _annualisation(ctx: ToolContext, session: ResultSession, inp: Input, rets: str) -> tuple[float, str]:
    """The default periods_per_year and the note that says where it came from:
    the bar timeframe by market (crypto every day; US equity sessions, with
    extended hours when the bars have them), or, when the observations are
    much further apart than the timeframe says (a weekly sample of daily
    bars kept as 1d), their own number per year."""
    tf = bar_timeframe(ctx, session, TOOL, inp)
    spacing, gaps = _spacing(session, rets)
    if tf is None:
        apart = f" (its observations are a median {_span(spacing)} apart)" if spacing else ""
        raise refuse(
            "periods_per_year_required",
            f"{TOOL} cannot annualise: {inp.label} has no single bar timeframe{apart}; pass periods_per_year "
            "(252 for daily equity bars, 365 for daily crypto bars, 52 for weekly).",
        )
    _, unit = tf_parts(tf)
    intraday = unit in ("min", "h")
    crypto = unit in ("min", "h", "d") and _every_day(ctx, session, inp, rets)
    if spacing is not None and gaps >= _MIN_GAPS and spacing >= 2 * tf_seconds(tf):
        if intraday:
            day = (
                "CAST(ts AS DATE)"
                if crypto
                else f"CAST(timezone('{_NEW_YORK}', timezone('UTC', ts)) AS DATE)"
            )
            per_day = scalar(
                session,
                f"SELECT median(n) FROM (SELECT s, {day} AS d, count(*) AS n FROM ({rets}) GROUP BY s, d)",
            )
            days = 365 if crypto else 252
            ppy = days * per_day
            how = (
                f"{days} {'days' if crypto else 'sessions'} a year x the median {per_day:g} observations a "
                f"{'UTC' if crypto else 'New York'} day"
            )
        else:
            (row,) = rows(
                session,
                "SELECT CAST(sum(n - 1) AS DOUBLE) AS k, sum(span) AS span FROM (SELECT count(*) AS n, "
                f"epoch(max(ts)) - epoch(min(ts)) AS span FROM ({rets}) GROUP BY s HAVING count(*) >= 2)",
            )
            ppy = float(max(1, round(row["k"] / row["span"] * _YEAR_SECONDS)))
            how = "their number per year over the time they span"
        return ppy, (
            f"periods_per_year = {ppy:g}: {inp.rid} is labelled {tf}, but its observations are a median "
            f"{_span(spacing)} apart (sampled, or with missing bars), so it is {how}; pass periods_per_year to "
            "set it."
        )
    extended = intraday and not crypto and _extended_hours(session, rets, tf)
    ppy = default_periods_per_year(tf, crypto=crypto, extended=extended)
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
    elif extended:
        basis = (
            "US equity: 252 sessions a year of 16 hours, 04:00-20:00 New York, since its bars include extended "
            "hours; "
        )
    else:
        basis = (
            "US equity: 252 sessions a year"
            + (" of 6.5 hours, 09:30-16:00 New York" if intraday else "")
            + "; "
        )
    return (
        ppy,
        f"periods_per_year = {ppy:g} from the {tf} timeframe of {inp.rid} ({basis}pass periods_per_year to set it).",
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
        if args.periods_per_year is None:
            ppy, note = _annualisation(ctx, session, inp, rets)
        else:
            ppy, note = args.periods_per_year, f"periods_per_year = {args.periods_per_year:g} as given."
        notes.append(note)
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
        "bar timeframe and market (US equity 1d 252, 1w 52, 1mo 12, 1h 1638, Nmin 98280/N, more when the bars "
        "include extended hours; crypto 1d 365, Nmin 525600/N), or from the data when its observations are much "
        "further apart than the timeframe; the notes say which. Volatility is a fraction per year (0.2 = 20 %). "
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
