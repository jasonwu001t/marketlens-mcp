"""analytics_align: as-of join of two results (latest right row at or before,
or first at or after, each left row), with tolerance and per-series matching.
"""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest
from analytics_harness import MINUTE, bars, run, source_provenance, unexplained_dynamic, utc

from marketlens_mcp.analytics.tools.align import SPEC
from marketlens_mcp.analytics.tools.correlation import SPEC as CORRELATION
from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.market import Quote

M0 = utc(2026, 1, 5, 13, 30)
SEC = dt.timedelta(seconds=1)
TS = pa.timestamp("us", tz="UTC")


def left_bars(store, tickers=("AAPL",)):
    rows = []
    for tk in tickers:
        rows += bars(tk, [1.0, 2.0, 3.0, 4.0], start=M0, step=MINUTE, timeframe="1min")
    return store.put_rows(rows)


def right_quotes(store, by_ticker=None, risk="api_structured"):
    times = [M0 - 30 * SEC, M0 + MINUTE, M0 + 2 * MINUTE + 59 * SEC]
    syms, ts, bids = [], [], []
    for tk, offset in (by_ticker or {"AAPL": 0.0}).items():
        for t, b in zip(times, [0.9, 1.9, 2.9], strict=True):
            syms.append(tk)
            ts.append(t)
            bids.append(b + offset)
    table = pa.table({"sym": syms, "t": pa.array(ts, TS), "bid": bids})
    return store.put_dynamic(table, group_column="sym", risk=risk)


def picks(out, col="bid"):
    return [(r["t"], r[col], r["matched_t"]) for r in out.table.to_pylist()]


def test_backward_takes_the_latest_right_row_at_or_before(store, ctx):
    left, right = left_bars(store), right_quotes(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"])
    assert out.model == "marketlens.Aligned"
    assert picks(out) == [
        (M0, 0.9, M0 - 30 * SEC),
        (M0 + MINUTE, 1.9, M0 + MINUTE),
        (M0 + 2 * MINUTE, 1.9, M0 + MINUTE),
        (M0 + 3 * MINUTE, 2.9, M0 + 2 * MINUTE + 59 * SEC),
    ]
    cols = out.table.column_names
    assert cols[: len(cols) - 2][-1] == "absent" and cols[-2:] == ["bid", "matched_t"]
    assert out.provenance.derived_from == [left, right]
    assert out.provenance.route == "duckdb:analytics_align"


def test_forward_takes_the_first_right_row_at_or_after(store, ctx):
    left, right = left_bars(store), right_quotes(store)
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"], direction="forward"
    )
    assert picks(out) == [
        (M0, 1.9, M0 + MINUTE),
        (M0 + MINUTE, 1.9, M0 + MINUTE),
        (M0 + 2 * MINUTE, 2.9, M0 + 2 * MINUTE + 59 * SEC),
        (M0 + 3 * MINUTE, None, None),
    ]
    rows = out.table.to_pylist()
    assert out.absent["bid"].code == "no_match" and out.absent["matched_t"].code == "no_match"
    assert unexplained_dynamic(rows, out.absent) == []
    assert any("1 of 4 left rows" in n for n in out.notes)


def test_tolerance_drops_matches_that_are_too_far(store, ctx):
    left, right = left_bars(store), right_quotes(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"], tolerance="PT30S")
    assert picks(out) == [
        (M0, 0.9, M0 - 30 * SEC),
        (M0 + MINUTE, 1.9, M0 + MINUTE),
        (M0 + 2 * MINUTE, None, None),
        (M0 + 3 * MINUTE, 2.9, M0 + 2 * MINUTE + 59 * SEC),
    ]
    assert unexplained_dynamic(out.table.to_pylist(), out.absent) == []
    wide = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"], tolerance="PT1H30M"
    )
    assert [b for _, b, _ in picks(wide)] == [0.9, 1.9, 1.9, 2.9]


def test_by_matches_within_each_series(store, ctx):
    left = left_bars(store, ("AAPL", "MSFT"))
    right = right_quotes(store, {"AAPL": 0.0, "MSFT": 100.0})
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"], by="ticker")
    assert e.value.code == "unknown_column" and "ticker" in e.value.message  # right has 'sym', not 'ticker'
    renamed = right_quotes_ticker(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=renamed, by="ticker")
    rows = out.table.to_pylist()
    assert [(r["ticker"], r["t"], r["bid"]) for r in rows] == [
        ("AAPL", M0, 0.9),
        ("AAPL", M0 + MINUTE, 1.9),
        ("AAPL", M0 + 2 * MINUTE, 1.9),
        ("AAPL", M0 + 3 * MINUTE, 2.9),
        ("MSFT", M0, 100.9),
        ("MSFT", M0 + MINUTE, 101.9),
        ("MSFT", M0 + 2 * MINUTE, 101.9),
        ("MSFT", M0 + 3 * MINUTE, 102.9),
    ]
    assert "ticker_right" not in out.table.column_names  # the by column is a key, not a right column


def right_quotes_ticker(store):
    times = [M0 - 30 * SEC, M0 + MINUTE, M0 + 2 * MINUTE + 59 * SEC]
    syms, ts, bids = [], [], []
    for tk, offset in (("AAPL", 0.0), ("MSFT", 100.0)):
        for t, b in zip(times, [0.9, 1.9, 2.9], strict=True):
            syms.append(tk)
            ts.append(t)
            bids.append(b + offset)
    return store.put_dynamic(
        pa.table({"ticker": syms, "t": pa.array(ts, TS), "bid": bids}), group_column="ticker"
    )


def test_by_left_and_by_right_match_bars_with_returns(store, ctx):
    left = left_bars(store, ("AAPL", "MSFT"))
    rets = run(RETURNS, ctx, result_id=left)
    right = store.put(
        rets.table, tool="analytics_returns", model=rets.model.schema_name, provenance=rets.provenance
    ).result_id
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right, by="ticker")
    assert e.value.code == "unknown_column" and "ticker" in e.value.message  # returns name it 'series'
    out = run(
        SPEC,
        ctx,
        left_result_id=left,
        right_result_id=right,
        by_left="ticker",
        by_right="series",
        right_columns=["ret"],
    )
    rows = out.table.to_pylist()
    assert [(r["ticker"], r["t"], r["ret"]) for r in rows] == [
        ("AAPL", M0, None),
        ("AAPL", M0 + MINUTE, pytest.approx(1.0)),
        ("AAPL", M0 + 2 * MINUTE, pytest.approx(0.5)),
        ("AAPL", M0 + 3 * MINUTE, pytest.approx(1 / 3)),
        ("MSFT", M0, None),
        ("MSFT", M0 + MINUTE, pytest.approx(1.0)),
        ("MSFT", M0 + 2 * MINUTE, pytest.approx(0.5)),
        ("MSFT", M0 + 3 * MINUTE, pytest.approx(1 / 3)),
    ]
    assert any("same ticker as series" in n for n in out.notes)
    default = run(SPEC, ctx, left_result_id=left, right_result_id=right, by_left="ticker", by_right="series")
    assert "series" not in default.table.column_names  # the right key is not brought over


@pytest.mark.parametrize(
    "keys",
    [
        {"by": "ticker", "by_left": "ticker", "by_right": "series"},
        {"by_left": "ticker"},
        {"by_right": "series"},
    ],
)
def test_by_with_by_left_or_a_lone_by_side_is_refused(store, ctx, keys):
    left = left_bars(store)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right_quotes_ticker(store), **keys)
    assert e.value.code == "invalid_arguments"


def test_right_result_with_several_series_needs_by(store, ctx):
    left = left_bars(store)
    right = right_quotes(store, {"AAPL": 0.0, "MSFT": 100.0})
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right)
    assert e.value.code == "right_not_single_series" and "by" in e.value.message and "2" in e.value.message


def test_default_right_columns_and_collision_suffix(store, ctx):
    left = left_bars(store)
    quotes = [
        Quote(
            ticker="AAPL",
            asset_class="us_equity",
            t=M0,
            bid_price=1.0,
            bid_size=100.0,
            bid_exchange="V",
            ask_price=1.1,
            ask_size=200.0,
            ask_exchange="Q",
            conditions=["R"],
            tape="C",
        )
    ]
    right = store.put_rows(quotes, tool="market_quotes")
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, suffix="_q")
    right_cols = out.table.column_names[len(store.tables[left].column_names) :]
    assert right_cols == [
        "ticker_q",
        "asset_class_q",
        "bid_price",
        "bid_size",
        "bid_exchange",
        "ask_price",
        "ask_size",
        "ask_exchange",
        "conditions",
        "tape",
        "currency_q",
        "matched_t",
    ]
    assert any("ticker -> ticker_q" in n for n in out.notes)
    first = out.table.to_pylist()[0]
    assert (first["ticker_q"], first["bid_price"], first["conditions"]) == ("AAPL", 1.0, ["R"])


def test_unknown_right_column_and_bad_tolerance_are_refused(store, ctx):
    left, right = left_bars(store), right_quotes(store)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["ask"])
    assert e.value.code == "unknown_column" and "'ask'" in e.value.message
    for bad in ("P1M", "1 hour", "PT", "P1Y"):
        with pytest.raises(ToolError) as e:
            run(SPEC, ctx, left_result_id=left, right_result_id=right, tolerance=bad)
        assert e.value.code == "invalid_tolerance"


def test_non_time_series_side_is_refused(store, ctx):
    left = left_bars(store)
    right = store.put_dynamic(pa.table({"x": [1.0]}))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right)
    assert e.value.code == "not_time_series" and right in e.value.message


def test_large_output_is_stored_with_the_left_time_and_group_columns(store, ctx):
    left = store.put_rows(
        bars("AAPL", [float(i + 1) for i in range(300)], start=M0, step=MINUTE, timeframe="1min")
    )
    right = right_quotes(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"])
    assert out.stored is not None and out.table is None
    info = out.stored
    assert (info.row_count, info.model, info.time_column, info.group_column) == (
        300,
        "marketlens.Aligned",
        "t",
        "ticker",
    )
    assert info.parents == [left, right]


def test_untrusted_text_on_either_side_marks_the_output(store, ctx):
    left, right = left_bars(store), right_quotes(store, risk="external_text")
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"])
    assert out.risk == "external_text"


def test_output_times_are_utc_whatever_the_session_zone(store, ctx):
    left, right = left_bars(store), right_quotes(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["bid"])
    assert out.table.schema.field("t").type == TS
    assert out.table.schema.field("matched_t").type == TS


def test_single_left_row(store, ctx):
    left = store.put_rows(bars("AAPL", [1.0], start=M0 + 2 * MINUTE, timeframe="1min"))
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right_quotes(store), right_columns=["bid"])
    assert picks(out) == [(M0 + 2 * MINUTE, 1.9, M0 + MINUTE)]


@pytest.mark.parametrize("huge", ["P999999999W", "PT9999999999999999999S", "P99999999999D"])
def test_a_tolerance_too_large_for_a_duration_is_refused_readably(store, ctx, huge):
    """A tolerance beyond Python's timedelta range used to surface as an internal
    error; it is an invalid tolerance like any other."""
    left, right = left_bars(store), right_quotes(store)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right, tolerance=huge)
    assert e.value.code == "invalid_tolerance"


def _units(table: pa.Table) -> dict[str, str | None]:
    return {f.name: ((f.metadata or {}).get(b"unit") or b"").decode() or None for f in table.schema}


def test_columns_keep_their_units_so_correlation_reads_prices_as_prices(store, ctx):
    """Every aligned column had no unit, so analytics_correlation on the aligned
    closes correlated price levels (AAPL-TLT -0.77 on returns of 0.11) with no
    word: each column keeps its source's unit, a renamed right column too."""
    t0 = utc(2026, 1, 5, 21)
    aapl = [100, 102, 101, 105, 104, 108, 107, 111]
    tlt = [90, 89, 90.5, 89.2, 90.1, 88.7, 89.9, 88.1]
    left = store.put_rows(bars("AAPL", aapl, start=t0) + bars("TLT", tlt, start=t0))
    right = store.put_rows(bars("SPY", [600 + i for i in range(8)], start=t0))
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["close"], suffix="_spy")
    units = _units(out.table)
    assert (units["close"], units["close_spy"], units["t"], units["matched_t"]) == (
        "price",
        "price",
        "UTC",
        "UTC",
    )
    assert units["ticker"] is None and units["volume"] == "by_asset_class"
    aligned = store.keep(out, "analytics_align")
    assert {c.name: c.unit for c in store.info(aligned).columns}["close_spy"] == "price"
    on_aligned = run(
        CORRELATION, ctx, result_id=aligned, value_column="close", series_column="ticker", min_overlap=2
    )
    on_bars = run(CORRELATION, ctx, result_id=left, min_overlap=2)
    assert any("simple returns" in n for n in on_aligned.notes)
    assert on_aligned.table.column("correlation").to_pylist() == pytest.approx(
        on_bars.table.column("correlation").to_pylist(), rel=1e-12
    )


def test_a_stored_aligned_result_keeps_the_units(store, ctx):
    left = store.put_rows(
        bars("AAPL", [float(i + 1) for i in range(300)], start=M0, step=MINUTE, timeframe="1min")
    )
    right = store.put_rows(bars("SPY", [1.0, 2.0], start=M0, step=60 * MINUTE, timeframe="1h"))
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["close", "vwap"])
    assert out.stored is not None
    units = {c.name: c.unit for c in out.stored.columns}
    assert (units["close"], units["close_right"], units["vwap_right"], units["matched_t"]) == (
        "price",
        "price",
        "price",
        "UTC",
    )


@pytest.mark.parametrize("zero", ["PT0S", "P0D", "PT0M"])
@pytest.mark.parametrize("direction", ["backward", "forward"])
def test_a_zero_tolerance_matches_the_same_time_only(store, ctx, zero, direction):
    """PT0S was refused as 'not an ISO-8601 duration'; it is an exact match."""
    left, right = left_bars(store), right_quotes(store)
    out = run(
        SPEC,
        ctx,
        left_result_id=left,
        right_result_id=right,
        right_columns=["bid"],
        tolerance=zero,
        direction=direction,
    )
    assert picks(out) == [
        (M0, None, None),
        (M0 + MINUTE, 1.9, M0 + MINUTE),
        (M0 + 2 * MINUTE, None, None),
        (M0 + 3 * MINUTE, None, None),
    ]
    assert any("3 of 4 left rows found no right row" in n and f"within {zero}" in n for n in out.notes)


def test_a_negative_tolerance_is_refused_by_name(store, ctx):
    left, right = left_bars(store), right_quotes(store)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, left_result_id=left, right_result_id=right, tolerance="-PT5M")
    assert e.value.code == "invalid_tolerance"
    assert "tolerance must be zero or positive" in e.value.message and "'-PT5M'" in e.value.message


# --- point in time: a right column with role available ----------------------------------------

D = dt.timedelta(days=1)
H = dt.timedelta(hours=1)
#: A datastore-shaped daily series: (event_time, knowledge_time, value). Each day
#: is known at 22:00 UTC the next day; Jan 2 was revised (its only version known
#: on Jan 8 12:00) and Dec 31 revised with Jan 7's release (the same knowledge time).
DAY = [
    (utc(2026, 1, 2), utc(2026, 1, 8, 12), 0.5),
    (utc(2026, 1, 5), utc(2026, 1, 6, 22), 1.0),
    (utc(2026, 1, 6), utc(2026, 1, 7, 22), 2.0),
    (utc(2026, 1, 7), utc(2026, 1, 8, 22), 3.0),
    (utc(2025, 12, 31), utc(2026, 1, 8, 22), -1.0),
]
LEFT_T = [utc(2026, 1, 6, 4), utc(2026, 1, 7, 4), utc(2026, 1, 8, 4), utc(2026, 1, 8, 15), utc(2026, 1, 9, 4)]


def known_series(store, rows=DAY, role=True):
    """A stored point-in-time result as a plugin stores one: time column
    event_time, and knowledge_time marked role available in its field metadata.
    ``rows``: (event_time, knowledge_time, value) of DGS10, or with the series first."""
    rows = [r if len(r) == 4 else ("DGS10", *r) for r in rows]
    meta = {b"unit": b"UTC", **({b"role": b"available"} if role else {})}
    schema = pa.schema(
        [
            pa.field("series_id", pa.string()),
            pa.field("event_time", TS, metadata={b"unit": b"UTC"}),
            pa.field("knowledge_time", TS, metadata=meta),
            pa.field("value", pa.float64(), metadata={b"unit": b"percent"}),
        ]
    )
    table = pa.Table.from_pylist([dict(zip(schema.names, r, strict=True)) for r in rows], schema=schema)
    return store.put_dynamic(
        table, tool="datastore_query", time_column="event_time", group_column="series_id"
    )


def daily_left(store, times=LEFT_T):
    table = pa.table({"series_id": ["DGS10"] * len(times), "t": pa.array(times, TS), "x": [1.0] * len(times)})
    return store.put_dynamic(table, time_column="t")


def test_a_right_column_with_role_available_is_matched_point_in_time(store, ctx):
    """N2: a value is matched to a left time only once it was known there,
    never by its event time: the time-column match gave four of five rows a
    value published after them."""
    left, right = daily_left(store), known_series(store)
    assert store.info(right).columns[2].role == "available"
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value", "knowledge_time"]
    )
    rows = out.table.to_pylist()
    assert [(r["t"], r["value"], r["matched_t"]) for r in rows] == [
        (LEFT_T[0], None, None),  # nothing known yet (Jan 5 is known that evening)
        (LEFT_T[1], 1.0, utc(2026, 1, 5)),
        (LEFT_T[2], 2.0, utc(2026, 1, 6)),
        # Jan 2's revision became known at 12:00, later than Jan 6, but Jan 6 is
        # still the latest day known: an as-of on knowledge_time alone gave 0.5.
        (LEFT_T[3], 2.0, utc(2026, 1, 6)),
        # Jan 7 and Dec 31's revision became known together: the latest day wins.
        (LEFT_T[4], 3.0, utc(2026, 1, 7)),
    ]
    assert all(r["knowledge_time"] is None or r["knowledge_time"] <= r["t"] for r in rows)
    assert any(
        n.startswith(
            f"Point in time: each row of {right} is matched from when it became usable, the later of"
        )
        for n in out.notes
    )
    assert any("1 of 5 left rows found no right row (usable at or before their time)" in n for n in out.notes)
    assert out.provenance.request["match_on"] == "available"
    assert unexplained_dynamic(rows, out.absent) == []


def test_match_on_time_aligns_on_the_time_column_with_a_look_ahead_note(store, ctx):
    left, right = daily_left(store), known_series(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], match_on="time")
    assert [(r["value"], r["matched_t"]) for r in out.table.to_pylist()] == [
        (2.0, utc(2026, 1, 6)),
        (3.0, utc(2026, 1, 7)),
        (3.0, utc(2026, 1, 7)),
        (3.0, utc(2026, 1, 7)),
        (3.0, utc(2026, 1, 7)),
    ]
    assert "__late" not in out.table.column_names
    assert any(
        n.startswith("match_on=time: matched on the time column event_time")
        and "4 of 5 matched left rows got a right row known only after their time (look-ahead)" in n
        for n in out.notes
    )
    assert not any(n.startswith("Point in time") for n in out.notes)


def test_a_knowledge_time_without_the_role_is_matched_on_but_not_called_point_in_time(store, ctx):
    """Review of N2, round 4: a knowledge_time without the role (a query
    answer's that cannot show when its rows became known) was left aside, and
    the time-column match gave four of five rows a value published after them.
    It is matched on all the same, as a lower bound (the safe default), with a
    note that the match is not shown point in time; match_on=time counts the
    look-ahead it shows, at least."""
    left, right = daily_left(store), known_series(store, role=False)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"])
    assert [r["value"] for r in out.table.to_pylist()] == [None, 1.0, 2.0, 2.0, 3.0]
    assert not any(n.startswith("Point in time") for n in out.notes)
    assert (
        f"Not shown point in time: knowledge_time of {right} is named as when each row became known but has no "
        "role available (datastore_query did not mark it), so nothing shows a row was known by then. Each row was matched all the same from the later of "
        "its time column event_time and knowledge_time, the safe default, which keeps out the values "
        "knowledge_time shows were published after the left time, but a value computed from rows known later can "
        "still hold data published after it. match_on=time matches on event_time alone instead."
    ) in out.notes
    assert "1 of 5 left rows found no right row (usable at or before their time)." in out.notes
    ahead = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], match_on="time"
    )
    assert [r["value"] for r in ahead.table.to_pylist()] == [2.0, 3.0, 3.0, 3.0, 3.0]
    assert any(
        n.startswith(
            f"match_on=time: matched on the time column event_time of {right} alone: at least 4 of 5 "
            "matched left rows got a right row with knowledge_time after their time (look-ahead)."
        )
        and "so this counts by knowledge_time alone" in n
        for n in ahead.notes
    )


def test_a_right_result_with_no_clock_is_said_not_point_in_time_unless_it_holds_market_data(store, ctx):
    """Review of N2, round 5: a right result with no clock at all was matched
    on its time column without a word, though a plugin's rows dated by the
    period they describe (research_macro_series' t) pair a bar with a value
    published weeks later, and a query answer or analytics output can hold
    values known after its time. Only fetched rows known at their time (quotes,
    trades) are matched so without a note (bars from their end, with one)."""
    left = daily_left(store)
    macro = store.put_dynamic(
        pa.table(
            {"series_id": ["CPI"] * 2, "t": pa.array([utc(2026, 1, 1), utc(2026, 1, 8)], TS), "v": [1.0, 2.0]}
        ),
        tool="research_macro_series",
        time_column="t",
    )
    out = run(SPEC, ctx, left_result_id=left, right_result_id=macro, right_columns=["v"])
    assert [r["v"] for r in out.table.to_pylist()] == [1.0, 1.0, 2.0, 2.0, 2.0]
    assert (
        f"Not shown point in time: {macro} holds marketlens.QueryRow rows with no column of role available (when "
        "each became known), dated by t, the time a row describes, which can be before it was published (only "
        "fetched market data, such as quotes or bars, is taken as known by its time); its rows were matched on the "
        "time column t alone, which can pair a left row with a value published after it."
    ) in out.notes
    answer = daily_left(store)
    computed = run(SPEC, ctx, left_result_id=left, right_result_id=answer, right_columns=["x"])
    assert (
        f"Not shown point in time: {answer} was computed by results_query with no column of role available (when "
        "each row became known), so its rows can hold values known after their time (a full-sample average, lead(), "
        "a bucket dated at its start); its rows were matched on the time column t alone, which can pair a left row "
        "with a value published after it."
    ) in computed.notes
    assert not any("match_on" in n for n in computed.notes)
    quotes = store.put_rows(
        [
            Quote(
                ticker="DGS10",
                asset_class="us_equity",
                t=t,
                bid_price=1.0,
                bid_size=1.0,
                ask_price=1.1,
                ask_size=1.0,
            )
            for t in LEFT_T[:2]
        ],
        tool="market_quotes",
    )
    market = run(SPEC, ctx, left_result_id=left, right_result_id=quotes, right_columns=["bid_price"])
    assert not any("point in time" in n.lower() or "match_on" in n for n in market.notes)


# --- point in time: a bar is known at its end ---------------------------------------------------

#: Daily bars of a stock start at midnight New York (05:00 UTC in January).
DAY_BARS = [utc(2026, 1, d, 5) for d in (5, 6, 7)]
HOURS = [utc(2026, 1, 6, 4), utc(2026, 1, 6, 5), utc(2026, 1, 6, 14), utc(2026, 1, 7, 15)]


def daily_bars(store, request=None, times=DAY_BARS, timeframe="1d", asset_class="us_equity"):
    rows = bars(
        "SPY",
        [10.0 + i for i in range(len(times))],
        times=times,
        timeframe=timeframe,
        asset_class=asset_class,
    )
    return store.put_rows(rows, provenance=source_provenance(request=request))


def hourly_left(store, times=HOURS):
    return store.put_rows(bars("SPY", [1.0] * len(times), times=times, timeframe="1h"))


def test_a_bar_is_matched_from_its_end_not_its_start(store, ctx):
    """Review of N2, round 6: a bar was matched from its start t, as if known
    then, so an hourly bar of 14:00 UTC got that day's daily close, known only
    when the session ended, under no note at all. A bar is usable from its end,
    t plus its timeframe: each left row gets the last bar that ended by its
    time (forward: the first that ends at or after it); match_on=time matches
    on t and counts the bars that ended after the left row."""
    left, right = hourly_left(store), daily_bars(store)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["close"])
    assert [(r["t"], r["close_right"], r["matched_t"]) for r in out.table.to_pylist()] == [
        (HOURS[0], None, None),  # Jan 5's bar runs to Jan 6 05:00
        (HOURS[1], 10.0, DAY_BARS[0]),
        (HOURS[2], 10.0, DAY_BARS[0]),  # Jan 6's close is not known at 14:00
        (HOURS[3], 11.0, DAY_BARS[1]),
    ]
    assert (
        f"Point in time: each bar of {right} is matched from its end, t plus its timeframe, when its close became "
        "known: each left row gets the bar with the latest t that ended at or before its time. match_on=time "
        "matches on t, a bar's start, instead."
    ) in out.notes
    assert "1 of 4 left rows found no right row (usable at or before their time)." in out.notes
    assert not any(n.startswith("Not shown") or "older right row" in n for n in out.notes)
    forward = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["close"], direction="forward"
    )
    assert [r["close_right"] for r in forward.table.to_pylist()] == [10.0, 10.0, 11.0, 12.0]
    assert any(
        n.endswith(
            "each left row gets the first bar that ends at or after its time. match_on=time "
            "matches on t, a bar's start, instead."
        )
        for n in forward.notes
    )
    ahead = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["close"], match_on="time"
    )
    assert [r["close_right"] for r in ahead.table.to_pylist()] == [10.0, 11.0, 11.0, 12.0]
    assert (
        f"match_on=time: matched on the time column t of {right}, each bar's start, not its end (t plus its "
        "timeframe), when its close became known: 4 of 4 matched left rows got a bar that ended after their time "
        "(look-ahead). match_on=available (the default) matches each bar from its end."
    ) in ahead.notes


def test_a_bars_end_is_counted_on_the_calendar_it_starts_in(store, ctx):
    """A bar of a day or longer ends N days, weeks or months on: a stock's (New
    York midnight) across a clock change is an hour shorter or longer in UTC,
    a crypto pair's (UTC midnight) is not; so the next bar's start gets it,
    never a time before its end."""
    week = daily_bars(store, times=[utc(2026, 3, 2, 5)], timeframe="1w")  # spring forward on March 8
    month = daily_bars(store, times=[utc(2026, 11, 1, 4)], timeframe="1mo")  # fall back on November 1
    crypto = daily_bars(store, times=[utc(2025, 11, 2)], asset_class="crypto")
    for right, ends in (
        (week, utc(2026, 3, 9, 4)),
        (month, utc(2026, 12, 1, 5)),
        (crypto, utc(2025, 11, 3)),
    ):
        left = hourly_left(store, times=[ends - dt.timedelta(minutes=1), ends])
        out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["close"])
        assert [r["close_right"] for r in out.table.to_pylist()] == [None, 10.0], ends


def test_bars_adjusted_after_their_time_are_matched_from_their_end_but_not_called_point_in_time(store, ctx):
    """Review of N2, round 6: bars fetched adjusted for splits and dividends
    (market_bars' default, adjustment=all) were taken as fixed once known,
    though a bar's prices are rescaled for every split and dividend up to the
    fetch: matched from its end all the same, with a note that the match is
    not shown point in time; raw bars are."""
    left = hourly_left(store)
    adjusted = daily_bars(store, request={"adjustment": "all"})
    out = run(SPEC, ctx, left_result_id=left, right_result_id=adjusted, right_columns=["close"])
    assert [r["close_right"] for r in out.table.to_pylist()] == [None, 10.0, 10.0, 11.0]
    assert (
        f"Not shown point in time: {adjusted} holds marketlens.Bar rows adjusted for corporate actions "
        "(adjustment=all) as fetched at 2026-10-02T20:00Z: their prices are rescaled for each split or dividend up "
        "to then, after their time too, so a price level holds what became known later (a return between two of them "
        "does not); fetch them with adjustment=raw to match point in time. Each bar was matched all the same from "
        "its end, t plus its timeframe, the safe default, which keeps out the bars that ended after the left time. "
        "match_on=time matches on t, a bar's start, instead."
    ) in out.notes
    assert not any(n.startswith("Point in time") for n in out.notes)
    raw = daily_bars(store, request={"adjustment": "raw"})
    out = run(SPEC, ctx, left_result_id=left, right_result_id=raw, right_columns=["close"])
    assert any(n.startswith(f"Point in time: each bar of {raw}") for n in out.notes)


def test_point_in_time_forward_takes_the_first_update_known_at_or_after(store, ctx):
    left, right = daily_left(store), known_series(store)
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], direction="forward"
    )
    # Jan 2's revision (12:00 on Jan 8) is no update: Jan 6 was already known.
    assert [(r["value"], r["matched_t"]) for r in out.table.to_pylist()] == [
        (1.0, utc(2026, 1, 5)),
        (2.0, utc(2026, 1, 6)),
        (3.0, utc(2026, 1, 7)),
        (3.0, utc(2026, 1, 7)),
        (None, None),
    ]
    assert any("first update usable at or after its time" in n for n in out.notes)


def test_point_in_time_tolerance_is_measured_to_when_the_row_became_known(store, ctx):
    left, right = daily_left(store), known_series(store)
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], tolerance="PT12H"
    )
    # 15:00 on Jan 8 is 17 hours after Jan 6 became known (22:00 on Jan 7).
    assert [r["value"] for r in out.table.to_pylist()] == [None, 1.0, 2.0, None, 3.0]
    assert any("the tolerance is measured to when it became usable" in n for n in out.notes)


def test_point_in_time_matches_within_each_series(store, ctx):
    """A series whose latest day is later must not hide another series' rows."""
    rows = [
        ("A", utc(2026, 1, 5), utc(2026, 1, 6, 22), 1.0),
        ("B", utc(2026, 1, 6), utc(2026, 1, 6, 21), 9.0),
    ]
    right = known_series(store, rows)
    table = pa.table({"series_id": ["A", "B"], "t": pa.array([LEFT_T[1]] * 2, TS)})
    left = store.put_dynamic(table, time_column="t", group_column="series_id")
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], by="series_id")
    assert [(r["series_id"], r["value"]) for r in out.table.to_pylist()] == [("A", 1.0), ("B", 9.0)]


def test_point_in_time_leaves_out_rows_with_no_knowledge_time(store, ctx):
    rows = [*DAY, (utc(2026, 1, 8), None, 4.0)]
    left, right = daily_left(store), known_series(store, rows)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"])
    assert [r["value"] for r in out.table.to_pylist()] == [None, 1.0, 2.0, 2.0, 3.0]
    assert "1 right row with no knowledge_time was left out: nothing says when it became known." in out.notes


#: PAYEMS in the store as datastore_query serves it, one version of each month
#: (the latest): the February 2026 benchmark revision re-stamped December 2021
#: and February 2022 to November 2025 on its release day; January 2022 last
#: changed in 2025; December 2025 and January 2026 are later revisions.
BENCHMARKED = [
    ("PAYEMS", utc(2021, 12, 1), utc(2026, 2, 11, 23), 149816.0),
    ("PAYEMS", utc(2022, 1, 1), utc(2025, 2, 7, 23), 150006.0),
    ("PAYEMS", utc(2022, 2, 1), utc(2026, 2, 11, 23), 150825.0),
    ("PAYEMS", utc(2025, 10, 1), utc(2026, 2, 11, 23), 158408.0),
    ("PAYEMS", utc(2025, 11, 1), utc(2026, 2, 11, 23), 158449.0),
    ("PAYEMS", utc(2025, 12, 1), utc(2026, 3, 6, 23), 158432.0),
    ("PAYEMS", utc(2026, 1, 1), utc(2026, 4, 3, 22), 158592.0),
]
BARS_T = [utc(2026, 1, 26, 5), utc(2026, 3, 2, 5), utc(2026, 4, 6, 4)]


def test_a_series_held_in_its_latest_version_says_how_much_older_its_matches_are(store, ctx):
    """Review of N2: with one version of each month, a benchmark revision
    hides every month it re-stamped until its release, so a bar of January
    2026 got January 2022's value (the newest month not revised since) with
    nothing said; a note now gives how much older the matches are than the
    latest month by time, and one says the result holds revised rows only in
    their revision, so a newer month may have been known then."""
    right = known_series(store, BENCHMARKED)
    left = daily_left(store, BARS_T)
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value", "knowledge_time"]
    )
    rows = out.table.to_pylist()
    assert [(r["value"], r["matched_t"]) for r in rows] == [
        (150006.0, utc(2022, 1, 1)),
        (158449.0, utc(2025, 11, 1)),
        (158592.0, utc(2026, 1, 1)),
    ]
    assert all(r["knowledge_time"] <= r["t"] for r in rows)
    assert "__newest" not in out.table.column_names and "__at" not in out.table.column_names
    assert (
        "2 of 3 matched left rows got an older right row than the latest event_time at or before their time, "
        f"which {right} has only as known after it: older by a median of 61 days and at most 1,461 days (left "
        "2026-01-26T05:00Z: event_time 2022-01-01 matched, 2026-01-01 the latest)."
    ) in out.notes
    assert any(
        n.startswith(
            f"{right} holds one version of each event_time, and at least 1 of its 7 rows are revisions"
        )
        and "a newer one may have been known at its time in a version this result does not hold" in n
        for n in out.notes
    )


def test_a_series_never_revised_says_only_its_publication_lag(store, ctx):
    """A daily value known 22:00 UTC the next business day: each bar at
    midnight New York gets an older day than its own, by the publication lag;
    nothing is revised, so nothing is said about missing versions."""
    days = [utc(2026, 1, d) for d in (5, 6, 7, 8, 9, 12)]
    known = [utc(2026, 1, 6, 22), utc(2026, 1, 7, 22), utc(2026, 1, 8, 22), utc(2026, 1, 9, 22)]
    known += [utc(2026, 1, 12, 22), utc(2026, 1, 13, 22)]
    right = known_series(
        store, [(d, k, 4.0 + i / 10) for i, (d, k) in enumerate(zip(days, known, strict=True))]
    )
    left = daily_left(store, [utc(2026, 1, d, 5) for d in (7, 8, 9, 12, 13)])
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], by="series_id")
    assert [r["matched_t"] for r in out.table.to_pylist()] == [utc(2026, 1, d) for d in (5, 6, 7, 8, 9)]
    assert any(
        n.startswith(
            "5 of 5 matched left rows got an older right row than the latest event_time at or before"
        )
        and "older by a median of 2 days and at most 4 days (left 2026-01-12T05:00Z: event_time 2026-01-08 "
        "matched, 2026-01-12 the latest)"
        in n
        for n in out.notes
    )
    assert not any("holds one version" in n for n in out.notes)


def test_a_series_with_every_version_matches_the_release_known_then(store, ctx):
    """With every version of each month (first release and revision), the
    match is the value as first published, and no note says versions are missing."""
    rows = [
        (utc(2025, 12, 1), utc(2026, 1, 9, 13, 30), 158400.0),
        (utc(2025, 12, 1), utc(2026, 2, 11, 23), 158432.0),
        (utc(2026, 1, 1), utc(2026, 2, 11, 23), 158500.0),
    ]
    right = known_series(store, rows)
    left = daily_left(store, [utc(2026, 1, 26, 5), utc(2026, 2, 12, 5)])
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"])
    assert [(r["value"], r["matched_t"]) for r in out.table.to_pylist()] == [
        (158400.0, utc(2025, 12, 1)),
        (158500.0, utc(2026, 1, 1)),
    ]
    assert any(n.startswith("1 of 2 matched left rows got an older right row") for n in out.notes)
    assert not any("holds one version" in n for n in out.notes)


def test_forward_says_how_much_older_the_update_is_than_the_latest_by_time_when_it_came(store, ctx):
    right = known_series(store, BENCHMARKED)
    left = daily_left(store, BARS_T)
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], direction="forward"
    )
    assert [r["matched_t"] for r in out.table.to_pylist()] == [utc(2025, 11, 1), utc(2025, 12, 1), None]
    assert (
        "2 of 2 matched left rows got an older right row than the latest event_time at or before when the "
        f"matched row became usable, which {right} has only as known after it: older by a median of 31 days and "
        "at most 61 days (left 2026-01-26T05:00Z: event_time 2025-11-01 matched, 2026-01-01 the latest)."
    ) in out.notes


#: The CPI release schedule as published in advance: every release date known
#: on 2025-12-01, long before it comes.
CPI_DAYS = [utc(2026, m, d, 13, 30) for m, d in ((1, 13), (2, 11), (3, 11), (4, 10), (5, 12), (6, 10))]


def test_a_schedule_known_in_advance_matches_no_row_dated_after_the_left_time(store, ctx):
    """Review of N2: matched on when each row became known alone, a schedule
    known in advance gave a bar of March 20 the June release (the latest date
    known), a row dated after the bar. A row is usable from the later of its
    time and when it became known, so each bar gets the last release at or
    before it, as the time column gives."""
    right = known_series(store, [(d, utc(2025, 12, 1), float(i)) for i, d in enumerate(CPI_DAYS)])
    left = daily_left(store, [utc(2026, 3, 20, 20), utc(2026, 5, 1, 20), utc(2026, 7, 1, 20)])
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"])
    rows = out.table.to_pylist()
    assert [r["matched_t"] for r in rows] == [CPI_DAYS[2], CPI_DAYS[3], CPI_DAYS[5]]
    assert all(r["matched_t"] <= r["t"] for r in rows)
    by_time = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], match_on="time"
    )
    assert out.table.to_pylist() == by_time.table.to_pylist()
    assert not any("older right row" in n or "holds one version" in n for n in out.notes)
    ahead = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], direction="forward"
    )
    assert [r["matched_t"] for r in ahead.table.to_pylist()] == [CPI_DAYS[3], CPI_DAYS[4], None]


def test_an_events_calendar_gets_the_last_print_known_not_the_next_scheduled_row(store, ctx):
    """Review of N2 (nasdaq.economic_events, Initial Jobless Claims): a print
    is known hours after its release, the next weeks are scheduled days
    ahead. The bar of October 12 got the October 15 scheduled row (no actual)
    instead of the October 8 print."""
    rows = [
        (utc(2026, 10, 1, 12, 30), utc(2026, 10, 2, 2, 15), 200000.0),
        (utc(2026, 10, 8, 12, 30), utc(2026, 10, 9, 2, 15), 197000.0),
        (utc(2026, 10, 15, 12, 30), utc(2026, 10, 9, 20, 21), None),
        (utc(2026, 10, 22, 12, 30), utc(2026, 10, 9, 20, 21), None),
    ]
    right = known_series(store, rows)
    left = daily_left(store, [utc(2026, 10, 8, 14), utc(2026, 10, 12, 8), utc(2026, 10, 16, 14)])
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"])
    assert [(r["value"], r["matched_t"]) for r in out.table.to_pylist()] == [
        (200000.0, rows[0][0]),  # the October 8 print was not known until 02:15 the next day
        (197000.0, rows[1][0]),
        (None, rows[2][0]),  # scheduled, and its time has come: the row as this result holds it
    ]
    assert any(n.startswith("1 of 3 matched left rows got an older right row") for n in out.notes)
    assert not any("holds one version" in n for n in out.notes)


def test_an_earnings_calendar_matches_each_tickers_last_report_at_or_before(store, ctx):
    """Every row of an earnings calendar is known before its report date; by
    ticker, a bar gets the last report at or before it, never the next."""
    known = utc(2026, 7, 1)
    rows = [
        ("AAPL", utc(2026, 7, 30), known, 1.6),
        ("AAPL", utc(2026, 10, 29), known, None),
        ("MSFT", utc(2026, 7, 28), known, 3.5),
        ("MSFT", utc(2026, 10, 27), known, None),
    ]
    right = known_series(store, rows)
    table = pa.table(
        {
            "series_id": ["AAPL", "MSFT", "AAPL"],
            "t": pa.array([utc(2026, 9, 15, 20), utc(2026, 10, 28, 20), utc(2026, 7, 2)], TS),
        }
    )
    left = store.put_dynamic(table, time_column="t", group_column="series_id")
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], by="series_id")
    assert [(r["series_id"], r["matched_t"]) for r in out.table.to_pylist()] == [
        ("AAPL", None),
        ("AAPL", utc(2026, 7, 30)),
        ("MSFT", utc(2026, 10, 27)),
    ]
