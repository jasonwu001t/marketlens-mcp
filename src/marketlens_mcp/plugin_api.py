"""The plugin API (version 1.0): how built-in providers and installed plugins
add capabilities, tools and canonical models to marketlens-mcp.

Owner: ml-core (seeded verbatim from the contract; the Registry, the
contexts and ``paginate`` are implemented in ``marketlens_mcp.registry``).

HOW A PACKAGE PLUGS IN
----------------------
Declare an entry point in the group ``marketlens.plugins`` whose value is a
``PluginInfo`` object::

    [project.entry-points."marketlens.plugins"]
    myplugin = "my_package:PLUGIN"

The server discovers entry points at start but imports a plugin only when the
owner names it in ``plugins.enabled`` in the config file. ``register`` is
called once with a staging Registry; if it raises, or anything it registered
fails validation, NOTHING from that plugin is kept, the server keeps running
without it, and ``marketlens-mcp plugins`` shows the reason.

Built-in providers use the same contract: each is a module exposing
``PLUGIN: PluginInfo``, imported from the fixed list in
``marketlens_mcp.builtins`` (a built-in that fails to register stops the
server: that is our bug, not the owner's configuration).

WHAT A PLUGIN CANNOT DO (v1)
----------------------------
* Register a tool that writes. Every tool is read-only; annotations are forced
  to readOnlyHint=True, destructiveHint=False.
* Change a built-in capability's default, or re-declare its id.
* Register a tool name or model schema name that already exists (the plugin
  is rejected whole).
* Bypass the policy, the result offload or the trust envelope: every tool
  passes through the same middleware.
Plugins run in-process: installing one is trusting its code.

VERSIONING
----------
``PLUGIN_API_VERSION`` is (major, minor). A plugin declares the version it
was written against. The server loads it when the majors are equal and the
plugin's minor is <= the server's minor; otherwise it is skipped with a
reason. Additive changes bump the minor; anything else bumps the major.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from marketlens_schema.base import (
    AbsenceReason,
    CanonicalModel,
    Environment,
    PaginationState,
    Provenance,
)

from .results_api import OutputRisk, ResultInfo, ResultStore

if TYPE_CHECKING:
    import pyarrow as pa

PLUGIN_API_VERSION: tuple[int, int] = (1, 0)
ENTRY_POINT_GROUP = "marketlens.plugins"

#: Tool names: snake_case ASCII, domain first ("market_bars"), at most 40
#: characters, so that a client's prefix ("mcp__marketlens__", 17 characters)
#: keeps the full name within the 64 characters providers accept.
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
TOOL_NAME_MAX = 40
#: Capability ids: "market", "results.export", "research".
CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?$")
#: Plugin names (entry-point names): "myplugin".
PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

T = TypeVar("T")


# --- capabilities ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySpec:
    """A switch the owner turns on or off in the config file.

    ``locked`` capabilities are always on and cannot be configured (the
    session-local result tools). ``declared_by`` is set by the registry
    ("builtin" or the plugin name); a value passed in is ignored.
    """

    id: str
    title: str
    description: str
    default_enabled: bool
    locked: bool = False
    declared_by: str = "builtin"


#: The capabilities the server itself declares. Plugins may attach tools to
#: these (a plugin's brokerage tools may sit under "portfolio") and
#: may declare new ones; they may not re-declare these ids.
BUILTIN_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "market",
        "Market data",
        "Stock, option, crypto and fixed-income bars, quotes, trades, snapshots, latest "
        "values, option chains with greeks, crypto order books, screeners.",
        True,
    ),
    CapabilitySpec(
        "reference",
        "Reference data",
        "Assets, option contracts and exchanges, market calendar and clock, corporate "
        "actions and announcements.",
        True,
    ),
    CapabilitySpec(
        "news", "News", "News articles. Third-party text: every response is marked untrusted.", True
    ),
    CapabilitySpec(
        "analytics",
        "Analytics on stored results",
        "Returns, rolling volatility, correlation, resample, as-of align, drawdown and beta, "
        "computed locally on result handles.",
        True,
    ),
    CapabilitySpec(
        "portfolio",
        "Brokerage account reads",
        "Account, balances, positions, orders, activities, portfolio history, account "
        "configuration, broker watchlists and locates (paper account unless "
        "portfolio.environment is live). Off by default: what the model reads leaves the "
        "machine with the model's requests.",
        False,
    ),
    CapabilitySpec(
        "results",
        "Session results",
        "Query, describe, sample, list and drop results stored by this session.",
        True,
        locked=True,
    ),
    CapabilitySpec(
        "results.export",
        "Export results to files",
        "Writes a stored result to CSV or Parquet in the configured export folder. The only "
        "tool that writes a file.",
        False,
    ),
    CapabilitySpec(
        "provider.docs",
        "Provider documentation",
        "Searches the data provider's documentation service (an outbound call to a third "
        "party). Third-party text, marked untrusted.",
        False,
    ),
)


# --- tools ----------------------------------------------------------------------------------


class ToolError(Exception):
    """A readable refusal or failure. The model sees ``code``, ``message`` and
    ``hint``; never a stack trace, a secret or a filesystem path."""

    def __init__(self, code: str, message: str, *, hint: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable


@dataclass
class ToolOutput:
    """What a handler returns. Exactly one of ``rows``, ``table`` or ``stored``.

    * ``rows``: canonical model instances (the common case). The middleware
      converts them to Arrow, decides inline vs stored, and builds the
      response.
    * ``table``: an Arrow table already in the model's canonical Arrow types
      (analytics computed in DuckDB), same treatment as rows.
    * ``stored``: the handler already wrote the result (``ctx.results.put``);
      the middleware returns its marker without re-reading it.

    ``model`` is the row model class, or a schema name string for dynamic
    results ("marketlens.Aligned", "marketlens.QueryRow").
    ``risk`` overrides the tool's declared output risk for this response (a
    results_query over a stored news result is external_text).
    """

    model: type[CanonicalModel] | str
    provenance: Provenance
    rows: Sequence[CanonicalModel] | None = None
    table: pa.Table | None = None
    stored: ResultInfo | None = None
    absent: dict[str, AbsenceReason] = field(default_factory=dict)
    pagination: PaginationState | None = None
    notes: list[str] = field(default_factory=list)
    risk: OutputRisk | None = None
    offload: Literal["auto", "always", "never"] = "auto"


Handler = Callable[["ToolContext", BaseModel], Awaitable[ToolOutput]]


@dataclass(frozen=True)
class ToolSpec:
    """One manifest entry. Drives registration, the policy check, the
    generated README table, ``tools --markdown`` and the parity tests.

    name             unique tool name (TOOL_NAME_RE, <= TOOL_NAME_MAX)
    capability       capability id that must be on for the tool to be listed
    title            short human title
    description      what the model reads: purpose, key parameters, units,
                     size behaviour ("large results are stored; you get a
                     result_id")
    readme           one line for the README table
    input_model      pydantic model of the arguments (extra="forbid"); its
                     JSON Schema is the tool's input schema
    output_model     row model class, or a schema name for dynamic outputs
    provider         "alpaca", "alpaca-docs", "local", or a plugin's
    route            human-readable upstream route, e.g.
                     "GET /v2/stocks/bars (market data API)"
    upstream_operations  upstream operation ids covered (Alpaca operationIds)
    parity_names     alpaca-mcp-server tool names this tool covers (parity test)
    env              environment variable names the tool needs (README column)
    output_risk      "external_text" for third-party prose (news, docs, notes)
    golden_test      repo-relative path of the test module that exercises this
                     tool; the manifest integrity test asserts the file exists
                     and contains the tool name
    handler          async (ctx, args) -> ToolOutput
    """

    name: str
    capability: str
    title: str
    description: str
    readme: str
    input_model: type[BaseModel]
    output_model: type[CanonicalModel] | str
    provider: str
    route: str
    handler: Handler
    golden_test: str
    upstream_operations: tuple[str, ...] = ()
    parity_names: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    output_risk: OutputRisk = "api_structured"


# --- what a handler gets --------------------------------------------------------------------


@dataclass(frozen=True)
class FetchLimits:
    """Upstream fetch ceilings (config ``fetch.*``)."""

    max_rows: int = 50_000
    max_pages: int = 20


@dataclass
class PageResult(Generic[T]):
    rows: list[T]
    pages_fetched: int
    next_page_token: str | None
    row_cap_hit: bool
    page_cap_hit: bool

    @property
    def complete(self) -> bool:
        return self.next_page_token is None

    def state(self) -> PaginationState:
        return PaginationState(
            complete=self.complete,
            pages_fetched=self.pages_fetched,
            rows_fetched=len(self.rows),
            next_page_token=self.next_page_token,
            row_cap_hit=self.row_cap_hit,
            page_cap_hit=self.page_cap_hit,
        )


#: fetch_page(page_token, page_limit) -> (rows, next_page_token). page_limit
#: is how many rows the caller may still take; pass it as the upstream page
#: size (capped at the upstream maximum) so no page is ever cut in half.
FetchPage = Callable[[str | None, int], Awaitable[tuple[list[T], str | None]]]


@runtime_checkable
class RateLimiter(Protocol):
    async def acquire(self) -> None:
        """Wait until one more request fits in the per-minute budget."""
        ...


@runtime_checkable
class ToolContext(Protocol):
    """Per-call context handed to a handler."""

    @property
    def tool(self) -> str: ...

    @property
    def session_id(self) -> str:
        """Opaque id of the MCP session (one per stdio process; one per HTTP session)."""
        ...

    @property
    def settings(self) -> Mapping[str, Any]:
        """This provider's or plugin's own config section (read-only):
        ``providers.<name>`` for built-ins, ``plugins.settings.<name>`` for plugins."""
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        """Ids of the capabilities enabled in this server."""
        ...

    @property
    def portfolio_environment(self) -> Environment: ...

    @property
    def limits(self) -> FetchLimits: ...

    @property
    def results(self) -> ResultStore: ...

    @property
    def log(self) -> logging.Logger: ...

    def env(self, name: str) -> str | None:
        """An environment variable the tool's spec (or its plugin's info)
        declares; KeyError for an undeclared name."""
        ...

    def now(self) -> datetime:
        """Timezone-aware UTC now (injectable in tests)."""
        ...

    async def paginate(self, fetch_page: FetchPage[T], *, start_token: str | None = None) -> PageResult[T]:
        """Call ``fetch_page`` until the upstream has no next token, or
        ``limits.max_rows`` rows, or ``limits.max_pages`` pages, whichever
        comes first. Algorithm (normative):

            rows, token, pages = [], start_token, 0
            while True:
                remaining = limits.max_rows - len(rows)
                page, token = await fetch_page(token, remaining)
                pages += 1; rows.extend(page)
                if token is None: break
                if len(rows) >= limits.max_rows: row_cap_hit = True; break
                if pages >= limits.max_pages: page_cap_hit = True; break

        ``next_page_token`` is the token the loop stopped with (None when the
        upstream is exhausted). Rows beyond max_rows (an upstream that ignored
        page_limit) are cut and row_cap_hit is set.
        """
        ...

    def limiter(self, key: str, per_minute: int) -> RateLimiter:
        """The process-wide limiter for ``key`` (one per upstream API key or
        host), created on first use with ``per_minute`` requests per minute."""
        ...


@runtime_checkable
class PluginContext(Protocol):
    """Handed to ``register``. Nothing here performs I/O."""

    @property
    def name(self) -> str: ...

    @property
    def server_version(self) -> str: ...

    @property
    def settings(self) -> Mapping[str, Any]: ...

    @property
    def portfolio_environment(self) -> Environment: ...

    @property
    def log(self) -> logging.Logger: ...

    def env(self, name: str) -> str | None:
        """A variable named in PluginInfo.env; KeyError otherwise."""
        ...

    def on_shutdown(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run ``callback`` when the server stops (close HTTP clients here)."""
        ...


@runtime_checkable
class Registry(Protocol):
    """What ``register`` may call. Every call validates immediately and raises
    RegistrationError with a readable reason."""

    def add_capability(self, spec: CapabilitySpec) -> None: ...

    def add_tool(self, spec: ToolSpec) -> None: ...

    def add_model(self, model: type[CanonicalModel]) -> None:
        """Register a canonical row model (JSON Schema export, Arrow types).
        A plugin's model schema names must start with "<plugin name>."."""
        ...


class RegistrationError(ValueError):
    """A registration the server refuses (bad name, duplicate, unknown
    capability, a model that is not a CanonicalModel, ...)."""


@dataclass(frozen=True)
class PluginInfo:
    """The object an entry point (or a built-in module's ``PLUGIN``) names."""

    name: str
    api_version: tuple[int, int]
    register: Callable[[Registry, PluginContext], None]
    description: str = ""
    #: Environment variable names this plugin may read through ctx.env().
    env: tuple[str, ...] = ()
