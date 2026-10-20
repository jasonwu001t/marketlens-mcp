"""Test harness of the official provider. Imported by conftest.py (fixtures)
and by the test modules (helpers); the module name is unique across tests/.

Everything here is offline: the publishers are an ``httpx.MockTransport``
serving the hand-written fixtures in fixtures/ (see its README) by URL, the
tool context is ``marketlens_mcp.testing.make_context`` with a fixed clock,
and outbound sockets are refused by tests/conftest.py.

Golden files: ``tests/official/golden/<name>.json`` hold the expected rows,
response ``absent``, notes and provenance (minus ``fetched_at``). A missing
golden file fails the test; set ``MARKETLENS_UPDATE_GOLDEN=1`` to (re)write
them, then review the diff.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketlens_mcp import ratelimit
from marketlens_mcp.plugin_api import ToolOutput
from marketlens_mcp.providers.official import feeds, fetch
from marketlens_mcp.providers.official.tool import SPECS
from marketlens_mcp.testing import make_context
from marketlens_schema import unexplained_absences

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"
NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
CONTACT = "tester@example.com"
SPEC = SPECS[0]

Answer = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture_bytes(feed_id: str) -> bytes:
    (path,) = FIXTURES.glob(f"{feed_id}.*")
    return path.read_bytes()


class FakePublishers:
    """Answers GETs of the allow-listed URLs with their fixtures; ``set``
    replaces a URL's answers (the last one repeats). Unknown URLs answer 404.
    Any other method answers 405, and the ``publishers`` fixture fails the
    test at teardown if one was sent: the provider is read-only on the wire."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, list[Answer]] = {
            f.url: [httpx.Response(200, content=fixture_bytes(f.id))] for f in feeds.FEEDS
        }
        self.served: dict[str, int] = {}
        self.transport = httpx.MockTransport(self._handle)

    def set(self, url: str, *answers: Answer) -> None:
        self.answers[url] = list(answers)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            return httpx.Response(405, text=f"the fake publishers answer GET only, not {request.method}")
        url = str(request.url)
        answers = self.answers.get(url)
        if not answers:
            return httpx.Response(404, text="no such feed")
        i = self.served.get(url, 0)
        self.served[url] = i + 1
        answer = answers[min(i, len(answers) - 1)]
        return answer(request) if callable(answer) else answer

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def non_get(self) -> list[str]:
        return [f"{r.method} {r.url}" for r in self.requests if r.method != "GET"]


@pytest.fixture
def publishers():
    fake = FakePublishers()
    with fetch.use_transport(fake.transport):
        yield fake
    assert fake.non_get() == [], "the official provider sent a request other than GET"


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The feed cache and the hosts' rate limiters (process-wide) start empty in every test."""

    def clear() -> None:
        fetch.clear_cache()
        for key in [k for k in ratelimit._LIMITERS if k.startswith("official:")]:
            del ratelimit._LIMITERS[key]

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retries never really wait in tests; the waits are recorded instead."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(fetch, "_sleep", fake_sleep)
    yield slept


def context(*, contact: str | None = CONTACT, now: datetime = NOW, settings: dict | None = None):
    env = {"SEC_CONTACT_EMAIL": contact} if contact is not None else {}
    return make_context(tool=SPEC.name, env=env, now=now, settings=settings)


def call(ctx=None, **arguments) -> ToolOutput:
    """validate -> handler, as the server does (without the offload)."""
    args = SPEC.input_model.model_validate(arguments)
    return asyncio.run(SPEC.handler(ctx or context(), args))


def output_doc(out: ToolOutput) -> dict[str, Any]:
    prov = out.provenance.model_dump(mode="json")
    prov.pop("fetched_at")
    return {
        "model": out.model.schema_name,
        "rows": [r.model_dump(mode="json") for r in (out.rows or [])],
        "absent": {k: v.model_dump(mode="json") for k, v in out.absent.items()},
        "notes": out.notes,
        "provenance": prov,
    }


def check_golden(out: ToolOutput, name: str) -> dict[str, Any]:
    """Assert the absence rule and compare with golden/<name>.json."""
    assert out.rows is not None and out.table is None and out.stored is None
    assert out.offload == "auto"
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
