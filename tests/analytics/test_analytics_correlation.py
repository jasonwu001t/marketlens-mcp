"""analytics_correlation: pairwise Pearson correlation in long form.

Expected values: statistics.correlation over the timestamps both series share.
"""

from __future__ import annotations

import statistics

import pyarrow as pa
import pytest
from analytics_harness import DAY, bars, output_rows, run, source_provenance, utc

from marketlens_mcp.analytics.tools.correlation import SPEC
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.analytics import CorrelationCell

T0 = utc(2026, 1, 5, 21)
TIMES = [T0 + i * DAY for i in range(6)]


def simple(prices):
    return [b / a - 1 for a, b in zip(prices, prices[1:], strict=False)]


def long_table(series: dict[str, list[float | None]], times=TIMES) -> pa.Table:
    names, ts, xs = [], [], []
    for name, values in series.items():
        for t, x in zip(times, values, strict=False):
            names.append(name)
            ts.append(t)
            xs.append(x)
    return pa.table({"s": names, "t": pa.array(ts, pa.timestamp("us", tz="UTC")), "x": xs})


def cell(rows, a, b):
    (c,) = [r for r in rows if (r.a, r.b) == (a, b)]
    return c


def test_known_pearson_value_on_values_used_as_is(store, ctx):
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [2.0, 4.0, 5.0, 4.0, 5.0]
    rid = store.put_dynamic(long_table({"A": a, "B": b}), group_column="s")
    out = run(SPEC, ctx, result_id=rid, value_column="x", min_overlap=2)
    rows = output_rows(out, CorrelationCell)
    assert [(r.a, r.b) for r in rows] == [("A", "A"), ("A", "B"), ("B", "A"), ("B", "B")]
    assert cell(rows, "A", "B").correlation == pytest.approx(statistics.correlation(a, b), rel=1e-12)
    assert cell(rows, "B", "A").correlation == cell(rows, "A", "B").correlation
    assert cell(rows, "A", "A").correlation == 1.0 and cell(rows, "B", "B").correlation == 1.0
    assert {r.n_obs for r in rows} == {5}
    assert {(r.start, r.end) for r in rows} == {(TIMES[0], TIMES[4])}
    assert {r.value_column for r in rows} == {"x"}
    assert not any("simple returns" in n for n in out.notes)


def test_price_result_is_turned_into_simple_returns_first(store, ctx):
    pa_ = [100, 102, 101, 105, 104, 108]
    pb = [50, 50.5, 50.2, 51.5, 51.0, 52.6]
    # C misses the third day: its returns differ and its overlap with A and B shrinks.
    times_c = [TIMES[i] for i in (0, 1, 3, 4, 5)]
    pc = [20, 20.4, 21.0, 20.8, 21.9]
    rid = store.put_rows(bars("A", pa_) + bars("B", pb) + bars("C", pc, times=times_c))
    out = run(SPEC, ctx, result_id=rid, min_overlap=2)
    rows = output_rows(out, CorrelationCell)
    ra, rb, rc = simple(pa_), simple(pb), simple(pc)
    assert cell(rows, "A", "B").correlation == pytest.approx(statistics.correlation(ra, rb), rel=1e-12)
    assert cell(rows, "A", "B").n_obs == 5
    # A pair's returns are computed over the timestamps both series share (days 0, 1, 3, 4, 5 for A and
    # C), so both returns of every pair cover the same interval.
    assert cell(rows, "A", "C").n_obs == 4
    ra_common = simple([pa_[i] for i in (0, 1, 3, 4, 5)])
    assert cell(rows, "A", "C").correlation == pytest.approx(statistics.correlation(ra_common, rc), rel=1e-12)
    assert cell(rows, "A", "C").start == TIMES[1] and cell(rows, "A", "C").end == TIMES[5]
    assert cell(rows, "C", "C").n_obs == 4
    assert {r.value_column for r in rows} == {"close"}
    assert any("simple returns" in n and "close" in n and "share" in n for n in out.notes)
    assert len(rows) == 9


def test_short_overlap_gives_none_with_insufficient_data(store, ctx):
    rid = store.put_dynamic(long_table({"A": [1.0, 2.0, 3.0], "B": [3.0, 1.0, 2.0]}), group_column="s")
    rows = output_rows(run(SPEC, ctx, result_id=rid, value_column="x"), CorrelationCell)
    ab = cell(rows, "A", "B")
    assert ab.correlation is None and ab.n_obs == 3
    assert ab.absent == {"correlation": "insufficient_data"}
    assert cell(rows, "A", "A").correlation is None  # below min_overlap=20 too


def test_constant_series_gives_none_not_applicable(store, ctx):
    rid = store.put_dynamic(
        long_table({"A": [1.0, 2.0, 3.0, 4.0], "K": [5.0, 5.0, 5.0, 5.0]}), group_column="s"
    )
    out = run(SPEC, ctx, result_id=rid, value_column="x", min_overlap=2)
    rows = output_rows(out, CorrelationCell)
    assert cell(rows, "A", "K").correlation is None
    assert cell(rows, "A", "K").absent == {"correlation": "not_applicable"}
    assert any("constant" in n for n in out.notes)


def test_pair_without_overlap_has_zero_observations(store, ctx):
    table = long_table({"A": [1.0, 2.0, 3.0, None, None, None], "B": [None, None, None, 1.0, 2.0, 4.0]})
    rid = store.put_dynamic(table, group_column="s")
    out = run(SPEC, ctx, result_id=rid, value_column="x", min_overlap=2)
    ab = cell(output_rows(out, CorrelationCell), "A", "B")
    assert (ab.n_obs, ab.correlation, ab.start, ab.end) == (0, None, None, None)
    assert ab.absent == {"correlation": "insufficient_data", "start": "no_data", "end": "no_data"}
    assert any("6 rows with a NULL x" in n for n in out.notes)


def test_one_series_is_refused(store, ctx):
    rid = store.put_rows(bars("A", [1, 2, 3]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "too_few_series"


def test_more_than_50_series_is_refused(store, ctx):
    rows = []
    for i in range(51):
        rows += bars(f"T{i}", [1, 2, 3])
    rid = store.put_rows(rows)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "too_many_series" and "51" in e.value.message and "50" in e.value.message


def test_result_without_series_column_is_refused(store, ctx):
    table = pa.table({"t": pa.array(TIMES[:3], pa.timestamp("us", tz="UTC")), "x": [1.0, 2.0, 3.0]})
    rid = store.put_dynamic(table)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, value_column="x")
    assert e.value.code == "no_series_column" and "series_column" in e.value.message


def test_single_row_input_is_refused(store, ctx):
    rid = store.put_rows(bars("A", [1]) + bars("B", [2]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "too_few_points"


def test_input_bounds():
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "r_0000000001", "min_overlap": 1})
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "r_0000000001", "method": "spearman"})


def test_dynamic_result_defaults_to_a_ret_column(store, ctx):
    table = long_table({"A": [0.01, 0.02, -0.01, 0.0], "B": [0.02, 0.03, -0.02, 0.01]})
    rid = store.put_dynamic(table.rename_columns(["s", "t", "ret"]), group_column="s")
    rows = output_rows(run(SPEC, ctx, result_id=rid, min_overlap=2), CorrelationCell)
    assert {r.value_column for r in rows} == {"ret"}
    a, b = [0.01, 0.02, -0.01, 0.0], [0.02, 0.03, -0.02, 0.01]
    assert cell(rows, "A", "B").correlation == pytest.approx(statistics.correlation(a, b), rel=1e-12)


def test_raw_bars_carry_the_split_note(store, ctx):
    prov = source_provenance(request={"adjustment": "raw"})
    rid = store.put_rows(bars("A", [100, 102, 51, 52]) + bars("B", [10, 10.2, 10.3, 10.4]), provenance=prov)
    out = run(SPEC, ctx, result_id=rid, min_overlap=2)
    assert any("not split-adjusted" in n for n in out.notes)
