"""Test harness of the data provider. Imported by conftest.py (fixtures) and
by the test modules (helpers); the module name is unique across tests/.

Everything is synthetic and offline (fixtures/README):

* The publishers are an ``httpx.MockTransport`` installed as marketlens-data's
  own shared HTTP client, so marketlens-data's ``fetch`` (credential checks,
  paging, the SEC ticker lookup, Nasdaq's prior-state read), ``normalize``,
  store write, point-in-time read and freshness all run for real; only the
  bytes on the wire are fixtures. An unrouted request fails the test.
* Each test gets its own store under its ``tmp_path`` (``set_store``).
* The capture clocks of the snapshot and schedule sources are frozen where a
  golden depends on them (``frozen_clock``).

Golden files: ``tests/data/golden/<tool>[__case].json`` hold the expected rows,
response ``absent``, pagination, notes, risk and provenance (minus
``fetched_at``). Set ``MARKETLENS_UPDATE_GOLDEN=1`` to (re)write them, then
review the diff.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from marketlens_mcp.plugin_api import FetchLimits, ToolOutput, ToolSpec
from marketlens_mcp.providers.data import runtime
from marketlens_mcp.providers.data.tools import all_specs
from marketlens_mcp.testing import make_context, temp_store
from marketlens_schema import unexplained_absences

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"
NOW = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
#: Synthetic values; none is a real key or address.
KEYS = {
    "FRED_API_KEY": "fredsynthetickey000000000000000",
    "BEA_API_KEY": "beasyntheticuserid0000",
    "BLS_API_KEY": "blssyntheticregistration0000",
    "SEC_CONTACT_EMAIL": "tests@example.com",
}
CAPABILITIES = (
    "market",
    "reference",
    "news",
    "analytics",
    "results",
    "macro",
    "filings",
    "fed_treasury",
    "holidays",
    "calendars",
)
SPECS: dict[str, ToolSpec] = {s.name: s for s in all_specs()}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


# --- the fake publishers -------------------------------------------------------------------


Responder = Callable[[httpx.Request], Any]


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    respond: Responder
    hits: int = 0


@dataclass
class Upstream:
    """Routes ``METHOD host/path`` (a regex, full match) to a responder. A
    responder returns an httpx.Response, a dict or list (JSON), str (text) or
    bytes."""

    routes: list[Route] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    unrouted: list[str] = field(default_factory=list)

    def add(self, pattern: str, respond: Responder | Any, *, method: str = "GET") -> Route:
        fn = respond if callable(respond) else (lambda _r, body=respond: body)
        route = Route(method, re.compile(pattern), fn)
        self.routes.insert(0, route)  # the latest route wins
        return route

    def json_file(self, pattern: str, name: str, **kw) -> Route:
        return self.add(pattern, lambda _r: fixture_json(name), **kw)

    def text_file(self, pattern: str, name: str, **kw) -> Route:
        return self.add(pattern, lambda _r: fixture_text(name), **kw)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        target = f"{request.url.host}{request.url.path}"
        for route in self.routes:
            if route.method == request.method and route.pattern.fullmatch(target):
                route.hits += 1
                body = route.respond(request)
                if isinstance(body, httpx.Response):
                    return body
                if isinstance(body, (dict, list)):
                    return httpx.Response(200, json=body)
                if isinstance(body, bytes):
                    return httpx.Response(200, content=body)
                return httpx.Response(200, text=str(body))
        self.unrouted.append(f"{request.method} {target}")
        return httpx.Response(599, text=f"unrouted {target}")

    def params(self, i: int = -1) -> dict[str, str]:
        return dict(parse_qsl(self.requests[i].url.query.decode()))

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


def install_upstream(monkeypatch) -> Upstream:
    from omni.http import client as omni_http

    upstream = Upstream()

    class Client(omni_http.HttpClient):
        def set_rate(self, host: str, per_second: float) -> None:  # no pacing against fixtures
            pass

    monkeypatch.setattr(omni_http, "_client", Client(transport=httpx.MockTransport(upstream.handle)))
    monkeypatch.setattr(omni_http.time, "sleep", lambda _s: None)  # no retry backoff
    return upstream


def freeze_clocks(monkeypatch, now: datetime = NOW) -> None:
    """Capture clocks of the snapshot and schedule sources, and the store's sync stamp."""
    import importlib

    from omni.sources import nasdaq, nyse, opm, sifma

    router = importlib.import_module("omni.query.router")  # omni.query is also the query function

    for module in (nasdaq, nyse, sifma, opm):
        monkeypatch.setattr(module, "_utcnow", lambda: now)
    monkeypatch.setattr(router, "utcnow", lambda: now)


# --- calling a tool ---------------------------------------------------------------------------


def context(
    tmp_path: Path,
    *,
    settings: dict[str, Any] | None = None,
    limits: FetchLimits | None = None,
    now: datetime = NOW,
):
    return make_context(
        store=temp_store(tmp_path / "results"),
        settings=settings or {},
        capabilities=CAPABILITIES,
        limits=limits or FetchLimits(),
        now=now,
    )


def call(name: str, ctx, **arguments) -> ToolOutput:
    """validate -> handler (the offload is checked separately with call_tool)."""
    spec = SPECS[name]
    ctx._tool = name  # the server sets the tool name per call
    args = spec.input_model.model_validate(arguments)
    return asyncio.run(spec.handler(ctx, args))


def refused(name: str, ctx, **arguments):
    from marketlens_mcp.plugin_api import ToolError

    with pytest.raises(ToolError) as info:
        call(name, ctx, **arguments)
    return info.value


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
    assert out.rows is not None and out.table is None and out.stored is None
    assert unexplained_absences(out.rows, out.absent) == []
    doc = output_doc(out)
    path = GOLDEN / f"{name}.json"
    if os.environ.get("MARKETLENS_UPDATE_GOLDEN") == "1":
        path.write_text(
            json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    assert path.exists(), (
        f"missing golden file {path.name}; run with MARKETLENS_UPDATE_GOLDEN=1 and review it"
    )
    assert doc == json.loads(path.read_text(encoding="utf-8"))
    return doc


def drain() -> None:
    asyncio.run(runtime.drain())
