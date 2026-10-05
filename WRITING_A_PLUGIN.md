# Writing a plugin

A plugin is an ordinary Python package that adds **capabilities**, **tools** and **canonical models** to marketlens-mcp. The built-in Alpaca provider, the analytics tools and the results tools use the same API, so a plugin's tools get everything theirs get: the policy check, the large-result store, the trust envelope, the generated tool table.

Plugin API version: **1.0** (`marketlens_mcp.PLUGIN_API_VERSION`).

## What a plugin can and cannot do

A plugin **can**:

- declare new capabilities (switches the owner turns on or off), each with its own default;
- attach tools to its own capabilities or to built-in ones (a brokerage plugin's tools under `portfolio`);
- register canonical row models named `<plugin>.<Model>`;
- read its own settings (`plugins.settings.<plugin>` in the config file) and the environment variables it declares.

A plugin **cannot**:

- register a tool that writes. Every tool is read-only: annotations are forced to `readOnlyHint=True, destructiveHint=False`, and there is no flag to change that. A tool that changes anything upstream does not belong in marketlens-mcp v1;
- re-declare a built-in capability or change its default;
- reuse a tool name or a model schema name that already exists (the whole plugin is rejected);
- bypass the policy, the offload to the result store, or the trust envelope.

**Plugins run in the server's process: installing one is trusting its code.**

## The entry point

Declare an entry point in the group `marketlens.plugins` whose value is a `PluginInfo`:

```toml
[project]
name = "marketlens-myplugin"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["marketlens-mcp>=0.1,<0.2", "httpx>=0.28.1,<1"]

[project.entry-points."marketlens.plugins"]
myplugin = "my_plugin:PLUGIN"
```

The server discovers entry points at start but imports a plugin only when the owner lists it in `plugins.enabled`. `register` is called once with a staging registry; if it raises, or anything it registered fails validation, nothing from the plugin is kept, the server starts without it, and `marketlens-mcp plugins` shows the reason.

## A complete example

`my_plugin/__init__.py`, a plugin for a fictional "Example Prices API":

```python
from __future__ import annotations

from typing import ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import (
    PLUGIN_API_VERSION,
    CapabilitySpec,
    PluginContext,
    PluginInfo,
    Registry,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
)
from marketlens_schema import AbsenceCode, CanonicalModel, Delay, Provenance, Ticker, UtcDatetime, normalize_ticker, unit
from marketlens_schema.base import TICKER_RE

BASE_URL = "https://api.example.com"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here


class DailyPrice(CanonicalModel):
    """One daily close from the Example Prices API."""

    schema_name: ClassVar[str] = "myplugin.DailyPrice"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("close", "volume")

    ticker: Ticker
    t: UtcDatetime = unit("UTC", description="Session close")
    close: float = unit("price")
    volume: float | None = unit("shares", default=None)


class DailyPricesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(description="Ticker, e.g. AAPL or BRK-B")
    page_token: str | None = Field(None, description="Continue a truncated fetch")


async def daily_prices(ctx: ToolContext, args: DailyPricesArgs) -> ToolOutput:
    ticker = normalize_ticker(args.ticker)
    if not TICKER_RE.match(ticker):
        raise ToolError("invalid_ticker", f"{args.ticker!r} is not a ticker.", hint="Use the form AAPL or BRK-B.")
    token = ctx.env("EXAMPLE_API_TOKEN")
    if not token:
        raise ToolError(
            "example_token_missing",
            "EXAMPLE_API_TOKEN is not set in this server's environment.",
            hint="Add it to the env block of this server in your MCP client's configuration.",
        )
    limiter = ctx.limiter("example-prices", per_minute=60)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=BASE_URL, transport=TRANSPORT, headers=headers, timeout=20) as client:

        async def fetch_page(page_token: str | None, page_limit: int):
            await limiter.acquire()
            params = {"symbol": ticker, "limit": min(page_limit, 1000)}
            if page_token:
                params["page_token"] = page_token
            response = await client.get("/v1/daily", params=params)
            if response.status_code != 200:
                raise ToolError("example_error", f"The Example Prices API returned HTTP {response.status_code}.")
            body = response.json()
            rows = [
                DailyPrice(
                    ticker=ticker,
                    t=item["t"],
                    close=item["c"],
                    volume=item.get("v"),
                    absent=None if item.get("v") is not None else {"volume": AbsenceCode.NOT_PROVIDED_BY_SOURCE},
                )
                for item in body["bars"]
            ]
            return rows, body.get("next_page_token")

        page = await ctx.paginate(fetch_page, start_token=args.page_token)

    provenance = Provenance(
        provider="example-prices",
        route="GET /v1/daily",
        fetched_at=ctx.now(),
        delay=Delay.END_OF_DAY,
        request={"ticker": ticker},
        pages_fetched=page.pages_fetched,
    )
    return ToolOutput(model=DailyPrice, provenance=provenance, rows=page.rows, pagination=page.state())


DAILY_PRICES = ToolSpec(
    name="myplugin_daily_prices",
    capability="myplugin",
    title="Daily prices (Example Prices API)",
    description="Daily closes and volumes for one ticker from the Example Prices API. Large results are "
    "stored; you get a result_id.",
    readme="Daily closes for one ticker",
    input_model=DailyPricesArgs,
    output_model=DailyPrice,
    provider="example-prices",
    route="GET /v1/daily",
    handler=daily_prices,
    golden_test="tests/test_daily_prices.py",
    env=("EXAMPLE_API_TOKEN",),
)


def register(registry: Registry, ctx: PluginContext) -> None:
    registry.add_capability(
        CapabilitySpec(
            "myplugin",
            "Example prices",
            "Daily closes from the Example Prices API (outbound calls).",
            default_enabled=False,
        )
    )
    registry.add_model(DailyPrice)
    registry.add_tool(DAILY_PRICES)


PLUGIN = PluginInfo(
    name="myplugin",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description="Daily prices from the Example Prices API.",
)
```

What the example shows:

- **The manifest entry** (`ToolSpec`) is the single source of truth: the server registers the tool from it, checks the policy against `capability`, builds the input schema from `input_model` (which must forbid extra fields), and prints its row in `tools --markdown`.
- **Readable failures** are `ToolError(code, message, hint=, retryable=)`. Any other exception becomes `internal_error` for the model; the traceback goes to the server log only.
- **`ctx.paginate`** walks the upstream's pages within the owner's limits (`fetch.max_rows`, `fetch.max_pages`); when it stops early, the server adds a note telling the model how to continue with `page_token`.
- **`ctx.limiter(key, per_minute)`** is a process-wide token bucket shared by every session.
- **`ctx.env(name)`** reads only names declared in `ToolSpec.env` or `PluginInfo.env`; anything else is a `KeyError`. Secrets come from the environment, never from the config file.
- **Absence**: a value the source does not give is `None` with a reason, per row (`absent`) or for the whole response (`ToolOutput.absent`), never 0.

## Naming rules

- Plugin names (entry-point names): `^[a-z][a-z0-9_]{1,31}$`, not `results`, `alpaca`, `analytics`, `core` or `marketlens`.
- Tool names: snake_case, domain first, at most 40 characters (`<domain>_<thing>`), so that a client's prefix still fits provider limits.
- Capability ids: `research`, `myplugin.extra` (lower case, at most one dot).
- Model schema names: `<plugin>.<Model>`.

## Schema conventions in brief

The full text is the docstring of `marketlens_schema/base.py`. Field names are snake_case and vendor-neutral; equities are `ticker` in SEC style (`BRK-B`), crypto pairs `BTC/USD`, options `occ_symbol`; instants are timezone-aware UTC (`UtcDatetime`); exact money is `DecimalStr` (never built from a float); every numeric field states its unit with `unit("price")`, `unit("shares")`, ... (`marketlens_schema.UNITS`); percentages are fractions. Models subclass `CanonicalModel`, which forbids extra fields and is frozen; set `time_column`, `group_column` and `value_columns` so that the result store's preview and the analytics tools pick sensible defaults.

## Large results

Return rows and let the server decide: results above 200 rows or about 6,000 tokens are stored and the model gets a marker with a `result_id`. For advanced cases a handler may write to the store itself (`ctx.results.put(...)`) and return `ToolOutput(stored=info, ...)`, or set `offload="always"`.

## Trust

Set `output_risk="external_text"` on any tool that returns prose written by third parties (articles, documents, notes, filings). The envelope then carries the security notice telling the model to treat the text as material, never as instructions. `results_query` over such a result inherits the risk.

## Enabling a plugin

```sh
pip install marketlens-myplugin        # into the same environment as marketlens-mcp
marketlens-mcp config init             # if you have no config file yet
```

Then in the config file:

```yaml
plugins:
  enabled: [myplugin]
  settings:
    myplugin: {}          # whatever the plugin documents; read with ctx.settings
capabilities:
  myplugin: true          # only needed when the plugin's capability is off by default
```

Check with `marketlens-mcp plugins` and `marketlens-mcp tools --markdown`, then restart the server.

## Testing

`marketlens_mcp.testing` runs a tool exactly as the server does (validate the arguments, run the handler, offload) without MCP:

```python
import asyncio

import httpx
import pytest

import my_plugin
from marketlens_mcp.testing import call_tool, make_context, temp_store


@pytest.fixture
def api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": [{"t": "2026-01-02T21:00:00Z", "c": 101.5, "v": 1200}]})

    monkeypatch.setattr(my_plugin, "TRANSPORT", httpx.MockTransport(handler))


def test_daily_prices(api, tmp_path):
    ctx = make_context(store=temp_store(tmp_path), env={"EXAMPLE_API_TOKEN": "test"})
    out = asyncio.run(call_tool(my_plugin.DAILY_PRICES, ctx, ticker="aapl"))
    assert out.kind == "inline" and out.rows[0]["ticker"] == "AAPL"
```

Keep a golden file (the expected rows, `absent`, pagination and provenance minus `fetched_at`) next to a synthetic fixture of the upstream's JSON, and assert `marketlens_schema.unexplained_absences(rows, absent) == []`. Never record live responses or keys.

## Versioning

`PluginInfo.api_version` is the plugin API version you wrote against. The server loads a plugin when the major versions are equal and the plugin's minor is not newer than the server's; otherwise it skips it with a reason. Additive changes bump the minor; anything else bumps the major. The canonical schema has its own version (`SCHEMA_VERSION`, carried in every provenance block).

## Writing a provider

A provider is a plugin that maps another data vendor onto the **existing** canonical models, so the model and the analytics tools see the same shapes whatever the source:

- reuse `marketlens.Bar`, `marketlens.Quote`, `marketlens.Trade`, ... as output models, and name your tools with your own prefix (`myvendor_bars`), never a built-in tool name;
- set `Provenance.provider` to your vendor and `route` to the upstream route, with `feed` and `delay` stated;
- translate the vendor's symbols, timestamps and units into the conventions above, and give every missing value a reason (a vendor's 0 that means "no quote" becomes `None` with `no_data`);
- keep an exclusion list of the vendor endpoints you do not map, each with a reason, and a parity test that fails when the vendor's API gains an endpoint you have not classified (see [ADDING_A_CAPABILITY.md](ADDING_A_CAPABILITY.md) for the routine the Alpaca provider follows);
- never map an endpoint that writes.

## Security notes

- The plugin runs in the server's process with the server's permissions.
- Read secrets from the environment through `ctx.env`, declared in `env`; never from settings, never into logs or responses.
- Do no network or file I/O at import time or in `register` (both run on every `marketlens-mcp` command, including `tools --json`); open clients lazily and close them with `ctx.on_shutdown(...)` in `register`.
- Never put a filesystem path in a response or an error message.
