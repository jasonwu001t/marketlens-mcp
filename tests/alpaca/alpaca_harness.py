"""Test harness of the Alpaca provider (owner: ml-alpaca). Imported by
conftest.py (fixtures) and by the test modules (helpers); the module name is
unique across tests/ so it never collides with another lane's conftest.

Everything here is synthetic and offline: Alpaca is an ``httpx.MockTransport``
serving hand-written fixtures shaped from the pinned OpenAPI specs (see
fixtures/README), the tool context is a small fake implementing the
``ToolContext`` protocol (pagination per its normative docstring), and outbound
sockets are refused.

Golden files: ``tests/alpaca/golden/<tool>[__case].json`` hold the expected
rows, response ``absent``, pagination, notes and provenance (minus
``fetched_at``). A missing golden file fails the test; set
``MARKETLENS_UPDATE_GOLDEN=1`` to (re)write them, then review the diff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

# Before the core lane merges, ``marketlens_mcp`` is imported from the interface
# stubs (PYTHONPATH=src:<stub>), whose own ``providers`` package would shadow this
# checkout's ``src/marketlens_mcp/providers/alpaca``. Put this checkout's
# providers directory first. Once ``src/marketlens_mcp`` is the real package this
# is a no-op (the directory is already the package's path).
_PROVIDERS_SRC = Path(__file__).resolve().parents[2] / "src" / "marketlens_mcp" / "providers"
import marketlens_mcp.providers as _providers  # noqa: E402

if _PROVIDERS_SRC.is_dir() and str(_PROVIDERS_SRC) not in [
    str(Path(p).resolve()) for p in _providers.__path__
]:
    _providers.__path__.insert(0, str(_PROVIDERS_SRC))
    sys.modules.pop("marketlens_mcp.providers.alpaca", None)

from marketlens_mcp.plugin_api import (  # noqa: E402
    CapabilitySpec,
    FetchLimits,
    PageResult,
    PluginContext,
    ToolOutput,
    ToolSpec,
)
from marketlens_schema.base import Environment, unexplained_absences  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"
REPO = HERE.parents[1]
NOW = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
TEST_KEYS = {"ALPACA_API_KEY": "PKTEST000SYNTHETIC", "ALPACA_SECRET_KEY": "synthetic-secret-not-a-key"}


# --- network guard (the shared tests/conftest.py adds the same guard after the merge) ---------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost") or isinstance(address, str):
            return real_connect(self, address)
        raise RuntimeError(f"test tried to reach the network: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retries never really wait in tests; the waits are recorded instead."""
    from marketlens_mcp.providers.alpaca import client

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(client, "_sleep", fake_sleep)
    yield slept


# --- a fake ToolContext ----------------------------------------------------------------------


class FakeLimiter:
    def __init__(self, key: str, per_minute: int):
        self.key = key
        self.per_minute = per_minute
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1


@dataclass
class FakeContext:
    """Implements marketlens_mcp.plugin_api.ToolContext for handler tests."""

    tool: str = "test_tool"
    session_id: str = "test-session"
    settings: Mapping[str, Any] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset({"market", "reference", "news", "portfolio", "provider.docs"})
    portfolio_environment: Environment = Environment.PAPER
    limits: FetchLimits = field(default_factory=FetchLimits)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(TEST_KEYS))
    declared_env: tuple[str, ...] = (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    )
    fixed_now: datetime = NOW
    limiters: dict[str, FakeLimiter] = field(default_factory=dict)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("marketlens.test"))

    @property
    def results(self):  # the Alpaca provider never touches the store directly
        raise AssertionError("the Alpaca provider must not use ctx.results")

    def env(self, name: str) -> str | None:
        if name not in self.declared_env:
            raise KeyError(name)
        return self.environ.get(name)

    def now(self) -> datetime:
        return self.fixed_now

    async def paginate(self, fetch_page, *, start_token: str | None = None) -> PageResult:
        rows: list = []
        token, pages = start_token, 0
        row_cap_hit = page_cap_hit = False
        while True:
            remaining = self.limits.max_rows - len(rows)
            page, token = await fetch_page(token, remaining)
            pages += 1
            rows.extend(page)
            if token is None:
                break
            if len(rows) >= self.limits.max_rows:
                row_cap_hit = True
                break
            if pages >= self.limits.max_pages:
                page_cap_hit = True
                break
        if len(rows) > self.limits.max_rows:
            rows = rows[: self.limits.max_rows]
            row_cap_hit = True
        return PageResult(rows, pages, token, row_cap_hit, page_cap_hit)

    def limiter(self, key: str, per_minute: int) -> FakeLimiter:
        if key not in self.limiters:
            self.limiters[key] = FakeLimiter(key, per_minute)
        return self.limiters[key]


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()


# --- a fake Registry and PluginContext -------------------------------------------------------


class FakeRegistry:
    """Implements marketlens_mcp.plugin_api.Registry; keeps what was added."""

    def __init__(self) -> None:
        self.tools: dict[str, ToolSpec] = {}
        self.capabilities: list[CapabilitySpec] = []
        self.models: list[type] = []

    def add_capability(self, spec: CapabilitySpec) -> None:
        self.capabilities.append(spec)

    def add_tool(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self.tools[spec.name] = spec

    def add_model(self, model: type) -> None:
        self.models.append(model)


class FakePluginContext:
    name = "alpaca"
    server_version = "0.1.0"
    settings: Mapping[str, Any] = {}
    portfolio_environment = Environment.PAPER
    log = logging.getLogger("marketlens.test")

    def env(self, name: str) -> str | None:
        raise AssertionError("register() must not read the environment")

    def on_shutdown(self, callback) -> None:
        pass


def registered() -> dict[str, ToolSpec]:
    from marketlens_mcp.providers.alpaca import PLUGIN

    reg = FakeRegistry()
    assert isinstance(FakePluginContext(), PluginContext)
    PLUGIN.register(reg, FakePluginContext())
    return reg.tools


@pytest.fixture(scope="session")
def specs() -> dict[str, ToolSpec]:
    return registered()


# --- a fake Alpaca ---------------------------------------------------------------------------


@dataclass
class Route:
    path: str
    responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]]
    match: dict[str, str] = field(default_factory=dict)
    calls: int = 0


class FakeAlpaca:
    """An httpx MockTransport answering GETs from registered routes, recording
    every request. Unmatched requests answer 599 so a test sees them fail.
    Any other method answers 405, and the ``alpaca`` fixture fails the test at
    teardown if one was sent: the provider is read-only on the wire."""

    def __init__(self) -> None:
        self.routes: list[Route] = []
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def add(
        self,
        path: str,
        *responses: Any,
        match: dict[str, str] | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Route:
        built = []
        for r in responses:
            if isinstance(r, httpx.Response) or callable(r):
                built.append(r)
            else:
                built.append(httpx.Response(status, json=r, headers=headers))
        route = Route(path, built, match or {})
        self.routes.append(route)
        return route

    def fixture(self, path: str, *names: str, match: dict[str, str] | None = None) -> Route:
        return self.add(path, *[load_fixture(n) for n in names], match=match)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            return httpx.Response(
                405, json={"message": f"the fake Alpaca answers GET only, not {request.method}"}
            )
        params = dict(parse_qsl(request.url.query.decode()))
        for route in self.routes:
            if route.path != request.url.path:
                continue
            if any(params.get(k) != v for k, v in route.match.items()):
                continue
            i = min(route.calls, len(route.responses) - 1)
            route.calls += 1
            r = route.responses[i]
            return r(request) if callable(r) else r
        return httpx.Response(599, json={"message": f"no fake route for {request.url}"})

    def params(self, i: int = -1) -> dict[str, str]:
        return dict(parse_qsl(self.requests[i].url.query.decode()))

    def non_get(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.method != "GET"]


@pytest.fixture
def alpaca():
    from marketlens_mcp.providers.alpaca.client import use_transport

    fake = FakeAlpaca()
    with use_transport(fake.transport):
        yield fake
    assert fake.non_get() == [], "the Alpaca provider sent a request other than GET"


def load_fixture(name: str) -> Any:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


# --- calling a tool and checking its golden file ---------------------------------------------


def call(spec: ToolSpec, ctx: FakeContext, **arguments) -> ToolOutput:
    """validate -> handler, as the server does (without the offload)."""
    ctx.tool = spec.name
    args = spec.input_model.model_validate(arguments)
    return asyncio.run(spec.handler(ctx, args))


def output_doc(out: ToolOutput) -> dict[str, Any]:
    model = out.model if isinstance(out.model, str) else out.model.schema_name
    prov = out.provenance.model_dump(mode="json")
    prov.pop("fetched_at")
    return {
        "model": model,
        "rows": [r.model_dump(mode="json") for r in (out.rows or [])],
        "absent": {k: v.model_dump(mode="json") for k, v in out.absent.items()},
        "pagination": out.pagination.model_dump(mode="json") if out.pagination else None,
        "notes": out.notes,
        "risk": out.risk,
        "offload": out.offload,
        "provenance": prov,
    }


def check_golden(out: ToolOutput, name: str) -> dict[str, Any]:
    """Assert the absence rule and compare with golden/<name>.json."""
    assert out.rows is not None, "Alpaca tools return canonical rows"
    assert out.table is None and out.stored is None
    assert out.offload == "auto", "every Alpaca tool goes through the offload"
    assert unexplained_absences(out.rows, out.absent) == []
    for row in out.rows:
        for nested in _nested_models(row):
            assert unexplained_absences([nested], {}) == [], nested
    doc = output_doc(out)
    path = GOLDEN / f"{name}.json"
    if os.environ.get("MARKETLENS_UPDATE_GOLDEN") == "1":
        path.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    assert path.exists(), (
        f"missing golden file {path.name}; run with MARKETLENS_UPDATE_GOLDEN=1 and review it"
    )
    expected = json.loads(path.read_text())
    assert doc == expected
    return doc


def _nested_models(row):
    from marketlens_schema.base import CanonicalModel

    for name in type(row).model_fields:
        value = getattr(row, name)
        if isinstance(value, CanonicalModel):
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, CanonicalModel))
