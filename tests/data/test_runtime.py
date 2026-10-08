"""The runtime under the tools: every D-code, the row cap, mode local, TTL
freshness, the store folder, the single worker thread, the timeout, secret
and path redaction, and the offload of a large answer."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from datetime import timedelta

import httpx
import pytest
from data_harness import KEYS, NOW, SPECS, call, context, drain, fixture_json, refused
from test_macro import BEA, fred_routes

from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_mcp.providers.data import runtime
from marketlens_mcp.results.store import StoreLimits, StoreRoot
from marketlens_mcp.testing import call_tool, make_context

NYFED = r"markets\.newyorkfed\.org/api/rates/all/search\.json"


# --- D1 ------------------------------------------------------------------------------------------

D1 = "The data tools need the marketlens-data package: install marketlens-mcp[data] (Python 3.13 or later)."


def test_d1_when_omni_cannot_be_imported(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "available", lambda: False)
    err = refused("fed_reference_rates", context(tmp_path))
    assert (err.code, err.message) == ("data_extra_missing", D1)


def test_d1_when_omni_lacks_a_name(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "REQUIRED_API", (*runtime.REQUIRED_API, "omni.not_there"))
    err = refused("fed_reference_rates", context(tmp_path))
    assert (err.code, err.message) == ("data_extra_missing", D1)


# --- D2, D5, D6, D10 ---------------------------------------------------------------------------------


def test_d2_missing_key_names_the_variable(tmp_path, upstream):
    fred_routes(upstream)
    err = refused("macro_series", context(tmp_path), series_ids=["CPIAUCSL"])
    assert err.code == "data_key_missing"
    assert err.message == "FRED needs FRED_API_KEY, which is not set in this server's environment."
    assert err.hint == (
        "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html and add FRED_API_KEY to the env "
        "block of this server in your MCP client's configuration."
    )
    assert upstream.requests == []


def test_d5_rejected_requests_never_echo_the_key(tmp_path, upstream, keys):
    upstream.add(
        r"api\.stlouisfed\.org/fred/series/observations", httpx.Response(400, json={"error_message": "Bad"})
    )
    # refused, marketlens-data lists the series' vintages, which FRED refuses for an unknown series too
    upstream.add(
        r"api\.stlouisfed\.org/fred/series/vintagedates", httpx.Response(400, json={"error_message": "Bad"})
    )
    err = refused("macro_series", context(tmp_path), series_ids=["NOSUCH"])
    assert err.code == "data_rejected"
    assert err.message.startswith("FRED refused the request: ")
    assert KEYS["FRED_API_KEY"] not in err.message
    upstream.add(
        BEA, {"BEAAPI": {"Results": {"Error": {"APIErrorCode": "201", "APIErrorDescription": "Bad table"}}}}
    )
    err = refused("macro_bea_table", context(tmp_path), table_name="T99999")
    assert err.code == "data_rejected" and err.message.startswith("BEA refused the request: BEA API error:")
    assert len(err.message) <= len("BEA refused the request: ") + 300


def test_d6_unavailable_is_retryable(tmp_path, upstream):
    upstream.add(NYFED, httpx.Response(503, text="maintenance"))
    err = refused("fed_reference_rates", context(tmp_path))
    assert (err.code, err.retryable) == ("data_unavailable", True)
    assert err.message == "The New York Fed did not answer (http_503); try again later."


def test_d10_anything_else_is_redacted(tmp_path, monkeypatch, keys):
    from omni.sources.base import get_source

    def boom(*_a, **_k):
        raise RuntimeError(
            f"parser broke on {KEYS['FRED_API_KEY']} at {tmp_path}/secret/file.json\nsecond line"
        )

    monkeypatch.setattr(get_source("fred"), "fetch", boom)
    err = refused("macro_series", context(tmp_path), series_ids=["CPIAUCSL"])
    assert err.code == "data_upstream"
    assert err.message == "FRED failed: parser broke on <redacted> at <path>"


# --- D7 and the worker thread ------------------------------------------------------------------------


def test_d7_timeout_keeps_fetching_in_the_background(tmp_path, monkeypatch):
    from omni.sources.base import get_source

    release, finished = threading.Event(), threading.Event()
    nyfed = get_source("nyfed")

    def slow(*_a, **_k):
        release.wait(10)
        finished.set()
        return fixture_json("nyfed.reference_rates.json")

    monkeypatch.setattr(nyfed, "fetch", slow)
    ctx = context(tmp_path, settings={"call_timeout_seconds": 0.2})

    async def scenario():
        with pytest.raises(ToolError) as info:
            await SPECS["fed_reference_rates"].handler(ctx, SPECS["fed_reference_rates"].input_model())
        release.set()
        await runtime.drain()
        return info.value

    ctx._tool = "fed_reference_rates"
    err = asyncio.run(scenario())
    assert (err.code, err.retryable) == ("data_timeout", True)
    assert err.message == (
        "The New York Fed is still fetching after 0.2 s; the fetch continues in the background, so call "
        "fed_reference_rates again in a minute."
    )
    assert finished.is_set()
    # the background fetch completed and was stored: the next call reads it
    again = call("fed_reference_rates", context(tmp_path))
    assert again.rows and again.provenance.cached is True


def test_every_omni_call_runs_on_one_worker_thread(tmp_path, upstream, monkeypatch):
    from omni.sources.base import get_source

    seen = []
    nyfed = get_source("nyfed")
    real = nyfed.fetch

    def record(*a, **k):
        seen.append(threading.current_thread().name)
        return real(*a, **k)

    upstream.json_file(NYFED, "nyfed.reference_rates.json")
    monkeypatch.setattr(nyfed, "fetch", record)
    call("fed_reference_rates", context(tmp_path))
    call("fed_reference_rates", context(tmp_path), start="2020-01-01")
    assert seen and set(seen) == {seen[0]} and seen[0].startswith("marketlens-data")
    assert threading.current_thread().name != seen[0]


# --- D8 ----------------------------------------------------------------------------------------------


def test_d8_store_folder_unusable(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder", encoding="utf-8")
    err = refused("fed_reference_rates", context(tmp_path, settings={"data_dir": str(blocker / "store")}))
    assert err.code == "data_store"
    assert err.message.startswith("The local data store cannot be read or written (")
    assert err.message.endswith("); check providers.data.data_dir.")
    assert str(tmp_path) not in err.message


def test_d8_store_lock_timeout(tmp_path, monkeypatch, omni_store):
    from omni.store import StoreLockTimeout

    def locked(*_a, **_k):
        raise StoreLockTimeout("lock on fiscaldata.debt_to_penny held by another process")

    monkeypatch.setattr(omni_store, "is_fresh", locked)
    err = refused("treasury_debt", context(tmp_path))
    assert (err.code, err.message) == (
        "data_store",
        "The local data store cannot be read or written (StoreLockTimeout); check providers.data.data_dir.",
    )


# --- the store folder ---------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_store_folder_is_created_owner_only(tmp_path, upstream):
    upstream.json_file(NYFED, "nyfed.reference_rates.json")
    target = tmp_path / "configured" / "store"
    call("fed_reference_rates", context(tmp_path, settings={"data_dir": str(target)}))
    assert os.environ["OMNI_DATA_DIR"] == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert os.environ["OMNI_ENV_FILE"] == ""


# --- marketlens never lets omni read a .env -----------------------------------------------------------
# tests/conftest.py sets OMNI_ENV_FILE="" for every test, so each test here first
# puts back what a user's shell may hold: nothing, or a path to a .env.


@pytest.mark.parametrize("shell", [None, "a path"])
def test_prepare_environment_turns_the_env_file_off(tmp_path, monkeypatch, shell):
    if shell is None:
        monkeypatch.delenv("OMNI_ENV_FILE")
    else:
        monkeypatch.setenv("OMNI_ENV_FILE", str(tmp_path / "keys.env"))
    runtime.prepare_environment()
    assert os.environ["OMNI_ENV_FILE"] == ""


def test_the_first_tool_call_and_doctor_turn_the_env_file_off(tmp_path, monkeypatch, capsys):
    from marketlens_mcp import cli

    monkeypatch.setenv("OMNI_ENV_FILE", str(tmp_path / "keys.env"))
    runtime.ensure(runtime.settings_of({"data_dir": str(tmp_path / "store")}))
    assert os.environ["OMNI_ENV_FILE"] == ""
    monkeypatch.delenv("OMNI_ENV_FILE")
    cli.main(["doctor"])
    assert "data extra: installed" in capsys.readouterr().out
    assert os.environ["OMNI_ENV_FILE"] == ""


# --- freshness, mode local, row cap -----------------------------------------------------------------


def test_ttl_second_call_is_served_from_the_store(tmp_path, upstream, keys):
    fred_routes(upstream)
    first = call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"])
    second = call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"])
    assert (first.provenance.cached, first.provenance.pages_fetched) == (False, 1)
    assert (second.provenance.cached, second.provenance.pages_fetched) == (True, 0)
    assert len(upstream.requests) == 1
    assert [r.model_dump() for r in first.rows] == [r.model_dump() for r in second.rows]


def test_ttl_expired_slice_is_fetched_again(tmp_path, upstream, keys):
    import importlib

    router = importlib.import_module("omni.query.router")
    fred_routes(upstream)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(router, "utcnow", lambda: NOW - timedelta(days=30))  # a sync stamped long ago
        call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"])
    out = call("macro_series", context(tmp_path, settings={"ttl_hours": 1}), series_ids=["CPIAUCSL"])
    assert out.provenance.cached is False and len(upstream.requests) == 2


def test_mode_local_reads_what_is_stored_and_fetches_nothing(tmp_path, upstream, keys):
    fred_routes(upstream)
    call("macro_series", context(tmp_path), series_ids=["CPIAUCSL"])
    local = call(
        "macro_series", context(tmp_path, settings={"mode": "local"}), series_ids=["CPIAUCSL", "UNRATE"]
    )
    assert {r.series_id for r in local.rows} == {"CPIAUCSL"}
    assert local.provenance.cached is True and len(upstream.requests) == 1
    empty = call("macro_series", context(tmp_path, settings={"mode": "local"}), series_ids=["UNRATE"])
    assert empty.rows == [] and empty.notes == [
        "mode is local and nothing is stored for this request; set providers.data.mode: auto to fetch it"
    ]


def test_row_cap(tmp_path, upstream, keys):
    fred_routes(upstream)
    out = call("macro_series", context(tmp_path, limits=FetchLimits(max_rows=2)), series_ids=["CPIAUCSL"])
    assert len(out.rows) == 2
    note = "Stopped at 2 rows (fetch.max_rows); narrow the request (fewer series, a shorter window, or metrics=...)."
    assert out.notes == [note]
    assert out.pagination.model_dump() == {
        "complete": False,
        "pages_fetched": 1,
        "rows_fetched": 2,
        "next_page_token": None,
        "row_cap_hit": True,
        "page_cap_hit": False,
    }
    assert out.provenance.truncated is True and out.provenance.truncation_note == note
    # through the pipeline: no R16 ("call again with page_token") for a cut without a token
    ctx = make_context(
        tool="macro_series", store=StoreRoot(tmp_path / "r").session("s"), limits=FetchLimits(max_rows=2)
    )
    response = asyncio.run(call_tool(SPECS["macro_series"], ctx, series_ids=["CPIAUCSL"]))
    assert response.notes == [note]


def test_a_large_answer_is_stored_not_inlined(tmp_path, upstream):
    def respond(request):
        from data_harness import fixture_text

        return fixture_text("treasury.yield_curve__2026.csv")

    upstream.add(r"home\.treasury\.gov/.*", respond)
    store = StoreRoot(tmp_path / "results", StoreLimits(inline_max_rows=10)).session("s")
    ctx = make_context(tool="treasury_yield_curve", store=store, now=NOW)
    marker = asyncio.run(call_tool(SPECS["treasury_yield_curve"], ctx))
    assert marker.kind == "stored" and marker.model == "marketlens.YieldCurvePoint"
    assert marker.row_count == 28 and marker.preview.time_column == "date"
    assert marker.provenance.dataset == "treasury.yield_curve"


def test_drain_helper_returns(tmp_path):
    drain()
