"""Inline vs stored (contract 5.2) and the stored-result marker (5.3)."""

from __future__ import annotations

import datetime as dt
import json

import pyarrow as pa
import pytest
from coresupport import T0, bar_rows, bar_table, news_rows, provenance, quote_rows

from marketlens_mcp import offload
from marketlens_mcp.plugin_api import FetchLimits, ToolOutput
from marketlens_mcp.results import guard
from marketlens_mcp.results.arrow import arrow_schema, rows_to_table
from marketlens_mcp.results_api import InlineResult, ResultMarker
from marketlens_schema import BUILTIN_MODELS, AbsenceCode, AbsenceReason, PaginationState
from marketlens_schema.market import Bar, NewsItem


def finalize(store, out, tool="market_bars", risk="api_structured", limits=FetchLimits()):
    return offload.finalize(out, tool=tool, default_risk=risk, store=store, limits=limits)


def test_200_rows_inline_201_rows_stored(store):
    small = finalize(
        store,
        ToolOutput(
            model="marketlens.QueryRow", provenance=provenance(), table=pa.table({"x": list(range(200))})
        ),
        tool="results_query",
    )
    assert isinstance(small, InlineResult) and small.row_count == 200
    big = finalize(
        store,
        ToolOutput(
            model="marketlens.QueryRow", provenance=provenance(), table=pa.table({"x": list(range(201))})
        ),
        tool="results_query",
    )
    assert isinstance(big, ResultMarker) and big.row_count == 201


def test_canonical_rows_inline_when_small(store):
    out = finalize(store, ToolOutput(model=Bar, provenance=provenance(), rows=bar_rows(n=10)))
    assert isinstance(out, InlineResult) and out.row_count == 30


def test_token_threshold_stores_wide_small_results(store):
    long_text = "word " * 2000  # about 2,500 estimated tokens per row
    rows = [r.model_copy(update={"headline": long_text}) for r in news_rows(3)]
    out = finalize(
        store,
        ToolOutput(model=NewsItem, provenance=provenance(), rows=rows),
        tool="news_search",
        risk="external_text",
    )
    assert isinstance(out, ResultMarker)
    assert out.risk == "external_text"
    assert out.row_count == 3


def test_estimate_tokens_is_bytes_over_four():
    assert offload.estimate_tokens([{"a": "x" * 10}]) == -(
        -len(json.dumps([{"a": "x" * 10}], separators=(",", ":"))) // 4
    )


def test_offload_always_and_never(store):
    rows = quote_rows(3)
    assert isinstance(
        finalize(
            store, ToolOutput(model="marketlens.Quote", provenance=provenance(), rows=rows, offload="always")
        ),
        ResultMarker,
    )
    many = bar_rows(n=400)
    assert isinstance(
        finalize(store, ToolOutput(model=Bar, provenance=provenance(), rows=many, offload="never")),
        InlineResult,
    )


def test_inline_rows_are_json_safe(store):
    rows = bar_rows(n=8)
    out = finalize(store, ToolOutput(model=Bar, provenance=provenance(), rows=rows))
    assert out.rows[1]["t"] == "2026-01-05T14:31:00Z"
    assert "absent" not in out.rows[1]
    assert out.rows[0]["absent"] == {"vwap": "not_provided_by_source"}
    assert out.rows[0]["vwap"] is None
    assert [c.name for c in out.columns][:3] == ["ticker", "asset_class", "timeframe"]
    assert out.columns[3].unit == "UTC"
    json.dumps(out.model_dump(mode="json"))


def test_inline_from_a_table(store):
    t = pa.table({"x": [1, 2], "when": pa.array([T0, T0], pa.timestamp("us", tz="UTC"))})
    out = finalize(
        store, ToolOutput(model="marketlens.QueryRow", provenance=provenance(), table=t), tool="results_query"
    )
    assert isinstance(out, InlineResult)
    assert out.rows == [{"x": 1, "when": "2026-01-05T14:30:00Z"}, {"x": 2, "when": "2026-01-05T14:30:00Z"}]
    assert [c.type for c in out.columns] == ["BIGINT", "TIMESTAMP WITH TIME ZONE"]


def test_exactly_one_payload_is_required(store):
    with pytest.raises(ValueError, match="exactly one"):
        finalize(store, ToolOutput(model=Bar, provenance=provenance()))
    with pytest.raises(ValueError, match="exactly one"):
        finalize(store, ToolOutput(model=Bar, provenance=provenance(), rows=[], table=pa.table({})))


def test_marker_shape_and_preview(store):
    absent = {"vwap": AbsenceReason(code=AbsenceCode.NOT_PROVIDED_BY_SOURCE, detail="omitted")}
    out = finalize(
        store,
        ToolOutput(
            model=Bar,
            provenance=provenance(),
            table=bar_table(n=1000),
            absent=absent,
            pagination=PaginationState(complete=True, pages_fetched=2, rows_fetched=3000),
        ),
    )
    assert isinstance(out, ResultMarker)
    m = ResultMarker.model_validate(out.model_dump(mode="json"))
    assert m.kind == "stored" and m.model == "marketlens.Bar" and m.schema_version == "1.0.0"
    assert m.preview.time_column == "t" and m.preview.group_column == "ticker"
    assert m.preview.time_span_start == T0
    assert m.preview.groups_count == 3 and m.preview.groups_sample == ["AAPL", "MSFT", "NVDA"]
    assert len(m.preview.first_rows) == 3 and len(m.preview.last_rows) == 3
    assert m.preview.first_rows[0]["t"] == "2026-01-05T14:30:00Z"
    assert m.preview.last_rows[-1]["t"] == (T0 + dt.timedelta(minutes=999)).isoformat().replace("+00:00", "Z")
    assert "absent" not in m.preview.first_rows[0]
    assert m.absent == absent
    assert {c.name: c for c in m.columns}["vwap"].null_reason == absent["vwap"]
    assert m.pagination.pages_fetched == 2
    assert m.how_to.startswith(
        "This result is stored, not shown. Query it with results_query using its result_id"
    )
    assert m.result_id in m.how_to
    assert [q.purpose for q in m.queries] == [
        "Coverage per series",
        "Latest row per series",
        "Daily summary of close",
    ]


def test_preview_truncates_long_strings(store):
    rows = [r.model_copy(update={"headline": "h" * 500}) for r in news_rows(3)]
    out = finalize(store, ToolOutput(model=NewsItem, provenance=provenance(), rows=rows, offload="always"))
    assert out.preview.first_rows[0]["headline"] == "h" * 200 + "…"


def test_ready_queries_pass_the_guard_and_run(store):
    out = finalize(store, ToolOutput(model=Bar, provenance=provenance(), table=bar_table(n=300)))
    for q in out.queries:
        guard.check(q.sql, live_ids=store.live_ids())
        res = store.query(q.sql, max_rows=50)
        assert res.row_count >= 1


@pytest.mark.parametrize("name", sorted(BUILTIN_MODELS))
def test_ready_queries_for_every_builtin_model(store, name):
    model = BUILTIN_MODELS[name]
    out = finalize(
        store,
        ToolOutput(
            model=model, provenance=provenance(), table=arrow_schema(model).empty_table(), offload="always"
        ),
        tool="x",
    )
    assert len(out.queries) == 3
    for q in out.queries:
        guard.check(q.sql, live_ids=store.live_ids())
        store.query(q.sql, max_rows=5)


def test_queries_for_a_dynamic_result_without_time_or_group(store):
    t = pa.table({"name": ["a", "b", "a"], "v": [1.0, 2.0, 3.0]})
    out = finalize(
        store,
        ToolOutput(model="marketlens.QueryRow", provenance=provenance(), table=t, offload="always"),
        tool="results_query",
    )
    assert [q.purpose for q in out.queries] == ["Row count", "First 20 rows", "Rows per name"]
    for q in out.queries:
        store.query(q.sql, max_rows=5)


def test_truncated_fetch_gets_the_r16_note(store):
    p = PaginationState(
        complete=False, pages_fetched=20, rows_fetched=9000, next_page_token="abc", page_cap_hit=True
    )
    out = finalize(
        store,
        ToolOutput(model=Bar, provenance=provenance(), rows=bar_rows(n=5), pagination=p),
        limits=FetchLimits(max_rows=50000, max_pages=20),
    )
    note = (
        "Stopped after 20 pages and 9000 rows (limits fetch.max_pages=20, fetch.max_rows=50000). The data is "
        'incomplete; call market_bars again with page_token="abc" to continue.'
    )
    assert note in out.notes
    assert out.provenance.truncated is True
    assert out.provenance.truncation_note == note


def test_stored_output_returns_the_existing_marker(store):
    info = store.put(
        rows_to_table(bar_rows(n=5), Bar),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
    )
    out = finalize(
        store,
        ToolOutput(model=Bar, provenance=info.provenance, stored=info, notes=["kept"]),
        tool="results_describe",
    )
    assert isinstance(out, ResultMarker)
    assert out.result_id == info.result_id
    assert "kept" in out.notes


def test_inline_table_for_a_model_without_its_absent_column_types_the_real_columns(store):
    from marketlens_schema.analytics import ReturnPoint

    t = pa.table(
        {
            "series": ["AAPL"],
            "t": pa.array([T0], pa.timestamp("us", tz="UTC")),
            "ret": [0.01],
            "kind": ["simple"],
            "period": pa.array([None], pa.string()),
            "price_column": ["close"],
        }
    )
    out = finalize(
        store, ToolOutput(model=ReturnPoint, provenance=provenance(), table=t), tool="analytics_returns"
    )
    assert [c.name for c in out.columns] == t.column_names
    assert {c.name: c.unit for c in out.columns}["ret"] == "fraction"
    stored = finalize(
        store,
        ToolOutput(model=ReturnPoint, provenance=provenance(), table=t, offload="always"),
        tool="analytics_returns",
    )
    assert [c.name for c in stored.columns] == t.column_names
    assert {c.name: c.unit for c in stored.columns}["ret"] == "fraction"
