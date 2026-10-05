"""analytics_align: as-of join of two results (latest right row at or before,
or first at or after, each left row), with tolerance and per-series matching.
"""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest
from analytics_harness import MINUTE, bars, run, unexplained_dynamic, utc

from marketlens_mcp.analytics.tools.align import SPEC
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
