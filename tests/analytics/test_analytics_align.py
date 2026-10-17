"""analytics_align: as-of join of two results (latest right row at or before,
or first at or after, each left row), with tolerance and per-series matching.
"""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest
from analytics_harness import MINUTE, bars, run, unexplained_dynamic, utc

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
        n.startswith(f"Point in time: matched on when each row of {right} became known") for n in out.notes
    )
    assert any("1 of 5 left rows found no right row (known at or before their time)" in n for n in out.notes)
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


def test_without_a_role_the_time_column_is_matched_with_no_clock_note(store, ctx):
    left, right = daily_left(store), known_series(store, role=False)
    out = run(SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"])
    assert [r["value"] for r in out.table.to_pylist()] == [2.0, 3.0, 3.0, 3.0, 3.0]
    assert not any("Point in time" in n or "match_on" in n for n in out.notes)


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
    assert any("first update known at or after its time" in n for n in out.notes)


def test_point_in_time_tolerance_is_measured_to_when_the_row_became_known(store, ctx):
    left, right = daily_left(store), known_series(store)
    out = run(
        SPEC, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], tolerance="PT12H"
    )
    # 15:00 on Jan 8 is 17 hours after Jan 6 became known (22:00 on Jan 7).
    assert [r["value"] for r in out.table.to_pylist()] == [None, 1.0, 2.0, None, 3.0]
    assert any("the tolerance is measured to knowledge_time" in n for n in out.notes)


def test_point_in_time_matches_within_each_series(store, ctx):
    """A series whose latest day is later must not hide another series' rows."""
    rows = [
        ("A", utc(2026, 1, 5), utc(2026, 1, 6, 22), 1.0),
        ("B", utc(2026, 1, 8), utc(2026, 1, 6, 21), 9.0),
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
