"""``Provenance.collected_at`` of a computed answer: its stalest input's.

An analytics tool and ``results_query`` compute from stored results, each of
which states when its data was last collected (a live fetch's time, a stored
slice's sync). The answer is as recently collected as the oldest of them, and
unknown where none says.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

testing = pytest.importorskip("marketlens_mcp.testing")

from analytics_harness import bars, table_of, utc  # noqa: E402

from marketlens_mcp.analytics.tools.align import SPEC as ALIGN  # noqa: E402
from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS  # noqa: E402
from marketlens_mcp.results.tools import SPECS as RESULTS_SPECS  # noqa: E402
from marketlens_schema.base import Provenance  # noqa: E402

M0 = utc(2026, 1, 5, 13, 30)
QUERY = next(s for s in RESULTS_SPECS if s.name == "results_query")


def _put(store, symbol, collected):
    rows = bars(symbol, [100, 110, 99, 101])
    return store.put(
        table_of(type(rows[0]), rows),
        tool="market_bars",
        model=type(rows[0]).schema_name,
        provenance=Provenance(
            provider="synthetic", route="GET /v2/stocks/bars", fetched_at=M0, collected_at=collected
        ),
    ).result_id


def _call(spec, ctx, **arguments):
    return asyncio.run(testing.call_tool(spec, ctx, **arguments))


@pytest.fixture
def real(tmp_path):
    store = testing.temp_store(tmp_path)
    return store, testing.make_context(store=store, capabilities=["analytics", "results"])


def test_a_computed_answer_is_as_recently_collected_as_its_stalest_input(real):
    store, ctx = real
    old, new = M0 - dt.timedelta(days=3), M0
    a, b = _put(store, "AAPL", new), _put(store, "MSFT", old)
    assert _call(ALIGN, ctx, left_result_id=a, right_result_id=b).provenance.collected_at == old
    assert _call(RETURNS, ctx, result_id=a).provenance.collected_at == new


def test_a_query_over_stored_results_states_the_oldest_collection(real):
    store, ctx = real
    old = M0 - dt.timedelta(hours=7)
    a, b = _put(store, "AAPL", M0), _put(store, "MSFT", old)
    out = _call(QUERY, ctx, sql=f'SELECT * FROM "{a}" UNION ALL SELECT * FROM "{b}"')
    assert out.provenance.collected_at == old


def test_inputs_that_state_no_collection_leave_it_unknown(real):
    store, ctx = real
    a = _put(store, "AAPL", None)
    assert _call(RETURNS, ctx, result_id=a).provenance.collected_at is None
    out = _call(QUERY, ctx, sql=f'SELECT count(*) AS n FROM "{a}"')
    assert out.provenance.collected_at is None
