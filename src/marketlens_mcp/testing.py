"""Helpers for plugin authors and post-merge tests (contract 3.3).

``temp_store`` gives a real result store under a folder you choose,
``make_context`` a real ToolContext, and ``call_tool`` runs a tool exactly
as the server does (validate -> handler -> offload) without MCP. Example::

    from marketlens_mcp.testing import call_tool, make_context, temp_store

    async def test_my_tool(tmp_path):
        ctx = make_context(store=temp_store(tmp_path), env={"MY_TOKEN": "x"})
        out = await call_tool(MY_SPEC, ctx, ticker="AAPL")
        assert out.kind == "inline"

Unlike the server, ``make_context`` does not refuse undeclared environment
names: ``env`` (or the process environment when ``env`` is None) answers
every ``ctx.env(name)``.
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from marketlens_schema import Environment

from . import paths
from .pipeline import execute
from .plugin_api import BUILTIN_CAPABILITIES, FetchLimits, ToolContext, ToolSpec
from .registry import ServerToolContext
from .results.store import StoreLimits, StoreRoot
from .results_api import ResultStore, ToolResponse

__all__ = ["call_tool", "make_context", "temp_store"]


def temp_store(
    root: pathlib.Path, *, session_key: str = "test", ttl_hours: float = 24, max_bytes: int = 5 * 2**30
) -> ResultStore:
    """A result store whose cache directory is ``root`` (results under
    ``root/results/<session_key>``)."""
    return StoreRoot(pathlib.Path(root), StoreLimits(ttl_hours=ttl_hours, max_bytes=max_bytes)).session(
        session_key
    )


def make_context(
    *,
    tool: str = "test_tool",
    store: ResultStore | None = None,
    settings: Mapping[str, Any] | None = None,
    capabilities: Iterable[str] | None = None,
    env: Mapping[str, str] | None = None,
    limits: FetchLimits = FetchLimits(),  # noqa: B008 - frozen dataclass; the contract's signature
    portfolio_environment: Environment = Environment.PAPER,
    now: datetime | None = None,
) -> ToolContext:
    """A ToolContext for tests. Without ``store``, a store is created on first
    use under the cache directory (``MARKETLENS_CACHE_DIR`` in tests)."""
    if capabilities is None:
        capabilities = [c.id for c in BUILTIN_CAPABILITIES if c.default_enabled or c.locked]
    session = uuid.uuid4().hex[:16]

    def factory() -> ResultStore:
        return StoreRoot(paths.cache_dir()).session(session)

    return ServerToolContext(
        tool=tool,
        session_id=session,
        settings=dict(settings or {}),
        capabilities=capabilities,
        portfolio_environment=portfolio_environment,
        limits=limits,
        results=store,
        results_factory=None if store is not None else factory,
        log=None,
        env_names=None,
        env_source=dict(env) if env is not None else None,
        clock=(lambda: now) if now is not None else None,
    )


async def call_tool(spec: ToolSpec, ctx: ToolContext, **arguments) -> ToolResponse:
    """Validate ``arguments`` against the spec, run its handler and offload
    the output exactly as the server does. Raises ToolError on failure."""
    done = await execute(spec, ctx, arguments)
    return done.response
