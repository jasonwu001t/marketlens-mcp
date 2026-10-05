"""Parity with alpacahq/alpaca-mcp-server and the pinned Alpaca OpenAPI specs.

(a) every operationId in both pinned spec files is mapped by exactly one
    registered tool or listed in EXCLUDED_OPERATIONS, never both;
(b) every one of alpaca-mcp-server's 72 tools is covered by a tool's
    parity_names or is one of the 17 writes, never both;
(c) every mapped operationId is a GET in the spec;
(d) no registered tool carries a write tool's name;
(e) the spec files are the pinned bytes (a spec sync updates these deliberately).
The check functions return problems; the meta tests prove they truly fail.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
from dataclasses import replace
from pathlib import Path

import pytest
from alpaca_harness import FIXTURES, registered

from marketlens_mcp.providers import alpaca as alpaca_pkg
from marketlens_mcp.providers.alpaca import exclusions
from marketlens_mcp.providers.alpaca.exclusions import EXCLUDED_OPERATIONS, READ_ONLY_V1, WRITE_TOOLS

SPECS_DIR = Path(alpaca_pkg.__file__).parent / "specs"
PINNED_SHA256 = {
    "trading-api.json": "8b9a45575ce2f9cd593ac8344096dcec9291ec7b92f03fe91d26656f0ac363ce",
    "market-data-api.json": "66382b65ea21baa4025c68a2c3b43e361c5d39a43e87b55243afb3269268bbc1",
}
WRITES_17 = {
    "update_account_config": "patchAccountConfig",
    "place_stock_order": "postOrder",
    "place_crypto_order": "postOrder",
    "place_option_order": "postOrder",
    "replace_order_by_id": "patchOrderByOrderId",
    "cancel_order_by_id": "deleteOrderByOrderID",
    "cancel_all_orders": "deleteAllOrders",
    "close_position": "deleteOpenPosition",
    "close_all_positions": "deleteAllOpenPositions",
    "exercise_options_position": "optionExercise",
    "do_not_exercise_options_position": "optionDoNotExercise",
    "create_watchlist": "postWatchlist",
    "update_watchlist_by_id": "updateWatchlistById",
    "delete_watchlist_by_id": "deleteWatchlistById",
    "add_asset_to_watchlist_by_id": "addAssetToWatchlist",
    "remove_asset_from_watchlist_by_id": "removeAssetFromWatchlist",
    "create_locate": "createLocates",
}
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


def spec_operations() -> dict[str, tuple[str, str, str]]:
    """operationId -> (spec file, METHOD, path) over both pinned specs."""
    out: dict[str, tuple[str, str, str]] = {}
    for name in PINNED_SHA256:
        spec = json.loads((SPECS_DIR / name).read_text(encoding="utf-8"))
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if method in METHODS:
                    assert op["operationId"] not in out, f"duplicate operationId {op['operationId']}"
                    out[op["operationId"]] = (name, method.upper(), path)
    return out


def pinned_tools() -> list[dict]:
    return json.loads((FIXTURES / "alpaca_mcp_server_tools.json").read_text(encoding="utf-8"))["tools"]


def check_operations(specs, excluded, operations) -> list[str]:
    problems = []
    mapped: dict[str, list[str]] = {}
    for spec in specs.values():
        for op in spec.upstream_operations:
            mapped.setdefault(op, []).append(spec.name)
    for op in sorted(operations):
        n = len(mapped.get(op, [])) + (op in excluded)
        if n == 0:
            problems.append(f"{op}: neither mapped nor excluded")
        elif n > 1:
            problems.append(f"{op}: mapped by {mapped.get(op, [])} and/or excluded")
    for op in sorted(set(mapped) | set(excluded)):
        if op not in operations:
            problems.append(f"{op}: not an operationId of the pinned specs")
    return problems


def check_tool_names(specs, write_names, tools) -> list[str]:
    problems = []
    covered: dict[str, list[str]] = {}
    for spec in specs.values():
        for name in spec.parity_names:
            covered.setdefault(name, []).append(spec.name)
    names = {t["name"] for t in tools}
    for name in sorted(names):
        n = len(covered.get(name, [])) + (name in write_names)
        if n == 0:
            problems.append(f"{name}: not covered and not a listed write")
        elif n > 1:
            problems.append(f"{name}: covered by {covered.get(name, [])} and/or listed as a write")
    for name in sorted(set(covered) - names):
        problems.append(f"{name}: not an alpaca-mcp-server tool")
    return problems


def check_get_only(specs, operations) -> list[str]:
    return [
        f"{spec.name} maps {op}, a {operations[op][1]}"
        for spec in specs.values()
        for op in spec.upstream_operations
        if op in operations and operations[op][1] != "GET"
    ]


def check_no_write_names(specs, write_names) -> list[str]:
    return [name for name in specs if name in write_names]


# --- the checks ------------------------------------------------------------------------------


def test_a_every_operation_is_mapped_or_excluded_exactly_once(specs):
    assert check_operations(specs, EXCLUDED_OPERATIONS, spec_operations()) == []


def test_b_every_alpaca_mcp_server_tool_is_covered_or_a_listed_write(specs):
    assert check_tool_names(specs, WRITE_TOOLS, pinned_tools()) == []


def test_c_mapped_operations_are_gets(specs):
    assert check_get_only(specs, spec_operations()) == []


def test_d_no_tool_is_named_like_a_write(specs):
    assert check_no_write_names(specs, WRITE_TOOLS) == []


def test_e_spec_files_are_the_pinned_bytes():
    for name, sha in PINNED_SHA256.items():
        assert hashlib.sha256((SPECS_DIR / name).read_bytes()).hexdigest() == sha, name


# --- the 17 writes ---------------------------------------------------------------------------


def test_the_17_writes_are_listed_with_the_read_only_reason():
    assert READ_ONLY_V1 == (
        "v1 is read-only (owner decision 2026-10-04): not registered, and no configuration can enable it."
    )
    assert dict(WRITE_TOOLS) == WRITES_17
    pinned_writes = {t["name"] for t in pinned_tools() if t["rw"] == "write"}
    assert pinned_writes == set(WRITES_17)
    for tool, op in WRITES_17.items():
        ex = EXCLUDED_OPERATIONS[op]
        assert ex.kind == "write" and ex.reason == READ_ONLY_V1, op
        assert tool in ex.alpaca_tools, (tool, op)


def test_write_tools_cannot_be_imported_or_registered(specs):
    for name in WRITES_17:
        assert name not in specs
        assert not hasattr(alpaca_pkg, name)
        assert not hasattr(exclusions, name)
    tools_pkg = importlib.import_module("marketlens_mcp.providers.alpaca.tools")
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"{tools_pkg.__name__}.{mod.name}")
        for name in WRITES_17:
            assert not hasattr(module, name), f"{mod.name}.{name}"
    for spec in specs.values():
        assert spec.handler.__name__ not in WRITES_17


def test_every_exclusion_has_a_known_kind_and_a_reason():
    kinds = {"write", "streaming", "out_of_scope", "candidate", "covered", "not_data"}
    operations = spec_operations()
    for op, ex in EXCLUDED_OPERATIONS.items():
        assert ex.kind in kinds, op
        assert len(ex.reason) > 20, op
        if ex.kind == "write":
            assert operations[op][1] != "GET", f"{op} is a GET listed as a write"


def test_counts(specs):
    operations = spec_operations()
    assert len(operations) == 103
    assert len(specs) == 53
    assert sum(len(s.parity_names) for s in specs.values()) == 55
    assert len({op for s in specs.values() for op in s.upstream_operations}) == 50
    assert len(EXCLUDED_OPERATIONS) == 53


# --- the checks truly fail -------------------------------------------------------------------


def _without(specs, tool, **changes):
    out = dict(specs)
    out[tool] = replace(specs[tool], **changes)
    return out


def test_meta_an_unmapped_operation_is_reported(specs):
    broken = _without(specs, "market_bars", upstream_operations=())
    assert check_operations(broken, EXCLUDED_OPERATIONS, spec_operations()) == [
        "StockBars: neither mapped nor excluded"
    ]


def test_meta_a_new_upstream_operation_is_reported(specs):
    operations = {**spec_operations(), "NewShinyEndpoint": ("market-data-api.json", "GET", "/v9/new")}
    assert check_operations(specs, EXCLUDED_OPERATIONS, operations) == [
        "NewShinyEndpoint: neither mapped nor excluded"
    ]


def test_meta_an_operation_both_mapped_and_excluded_is_reported(specs):
    excluded = {**EXCLUDED_OPERATIONS, "StockBars": EXCLUDED_OPERATIONS["StockBarSingle"]}
    problems = check_operations(specs, excluded, spec_operations())
    assert problems == ["StockBars: mapped by ['market_bars'] and/or excluded"]


def test_meta_an_uncovered_alpaca_mcp_server_tool_is_reported(specs):
    broken = _without(specs, "news_search", parity_names=())
    assert check_tool_names(broken, WRITE_TOOLS, pinned_tools()) == [
        "get_news: not covered and not a listed write"
    ]


def test_meta_a_dropped_write_is_reported(specs):
    writes = {k: v for k, v in WRITE_TOOLS.items() if k != "create_locate"}
    assert check_tool_names(specs, writes, pinned_tools()) == [
        "create_locate: not covered and not a listed write"
    ]


def test_meta_a_mapped_write_operation_is_reported(specs):
    broken = _without(specs, "portfolio_orders", upstream_operations=("getAllOrders", "postOrder"))
    assert check_get_only(broken, spec_operations()) == ["portfolio_orders maps postOrder, a POST"]


def test_meta_a_tool_named_like_a_write_is_reported(specs):
    broken = {**specs, "place_stock_order": specs["market_bars"]}
    assert check_no_write_names(broken, WRITE_TOOLS) == ["place_stock_order"]


@pytest.mark.parametrize("name", sorted(PINNED_SHA256))
def test_meta_a_changed_spec_byte_changes_the_hash(name, tmp_path):
    data = bytearray((SPECS_DIR / name).read_bytes())
    data[-2] ^= 1
    assert hashlib.sha256(bytes(data)).hexdigest() != PINNED_SHA256[name]


def test_registered_through_plugin_matches_the_session_fixture(specs):
    assert sorted(registered()) == sorted(specs)
