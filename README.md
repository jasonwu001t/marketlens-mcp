# marketlens-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server for market data and analytics. It answers in one vendor-neutral schema whatever the data provider (Alpaca is built in; other providers plug in), keeps large results out of the model's context window in a local DuckDB result store that the model queries with read-only SQL, computes common analytics (returns, volatility, correlation, drawdown, beta, ...) on those stored results, and lets you choose which capabilities it may use. It works with any MCP client and your own provider keys.

## Status

Alpha (0.1.0, unreleased). The tool names, the canonical schema (1.0.0) and the plugin API (1.0) are versioned; see [CHANGELOG.md](CHANGELOG.md).

## Install

```sh
uvx marketlens-mcp            # run without installing (recommended for MCP clients)
pipx install marketlens-mcp   # or a user-wide install
pip install marketlens-mcp    # or into an environment you manage
```

Python 3.11 or newer. The bare command serves MCP over stdio.

## Add it to a client

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "marketlens": {
      "command": "uvx",
      "args": ["marketlens-mcp"],
      "env": {
        "ALPACA_API_KEY": "your key id",
        "ALPACA_SECRET_KEY": "your secret key"
      }
    }
  }
}
```

Claude Code:

```sh
claude mcp add marketlens -e ALPACA_API_KEY=... -e ALPACA_SECRET_KEY=... -- uvx marketlens-mcp
```

Any other client: run `marketlens-mcp` (stdio), or `marketlens-mcp serve --transport http` for streamable HTTP on `http://127.0.0.1:8765/mcp` with a bearer token (see [Security and privacy](#security-and-privacy)).

Without Alpaca keys the server still starts and lists its tools; Alpaca tools then answer with a readable error.

## Capabilities and defaults

Each tool belongs to one capability. Turn capabilities on or off in the config file; a tool whose capability is off is not listed at all.

| Capability | Default | What it covers |
|---|---|---|
| `market` | on | Stock, option, crypto and fixed-income bars, quotes, trades, snapshots, latest values, option chains with greeks, crypto order books, screeners. |
| `reference` | on | Assets, option contracts and exchanges, market calendar and clock, corporate actions and announcements. |
| `news` | on | News articles. Third-party text: every response is marked untrusted. |
| `analytics` | on | Returns, rolling volatility, correlation, resample, as-of align, drawdown and beta, computed locally on result handles. |
| `portfolio` | off | Brokerage account reads: account, positions, orders, activities, portfolio history, account configuration, broker watchlists and locates (the paper account unless `portfolio.environment: live`). Off by default: what the model reads leaves your machine with the model's requests. |
| `results` | always on | Query, describe, sample, list and drop results stored by this session. |
| `results.export` | off | Write a stored result to CSV or Parquet in your export folder. The only tool that writes a file. |
| `provider.docs` | off | Search the data provider's documentation service (an outbound call to a third party). Third-party text, marked untrusted. |

Plugins may declare more capabilities; see [Plugins and providers](#plugins-and-providers).

**Read-only.** marketlens-mcp has no tool that places, replaces or cancels orders, closes positions, exercises options, changes account settings, or edits broker watchlists. Those tools do not exist in the package, so no configuration can enable them.

## Configuration

Settings live in a YAML file:

- `$MARKETLENS_CONFIG` if set (the file must exist), else
- `$XDG_CONFIG_HOME/marketlens/config.yaml` if `XDG_CONFIG_HOME` is set, else
- `~/.config/marketlens/config.yaml` (on every platform).

No file means every default. `marketlens-mcp config init` writes the commented default file, `config path` prints where it is, `config show` prints the effective settings. The server reads the file once at start: restart it after editing. Unknown settings, wrong types and out-of-range values refuse to start with a one-line reason.

Secrets never go in the file. They come from the environment only: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `MARKETLENS_HTTP_TOKEN` for HTTP. `MARKETLENS_LOG_LEVEL` (default `WARNING`) sets the log level; logs go to stderr.

The default file:

```yaml
version: 1
capabilities:
  market: true
  reference: true
  news: true
  analytics: true
  portfolio: false
  results.export: false
  provider.docs: false
portfolio:
  environment: paper      # paper | live
providers:
  alpaca:
    stock_feed: iex       # iex | sip | delayed_sip | boats | overnight (your plan decides)
    options_feed: indicative   # indicative | opra
    crypto_location: us   # us | us-1 | us-2 | eu-1 | bs-1
    rate_limit_per_minute: 190
    trading_url: null
    data_url: null
results:
  inline_max_rows: 200
  inline_max_tokens: 6000
  query_max_rows: 200
  query_max_bytes: 24000
  query_timeout_seconds: 10
  query_memory_limit: 1GB
  ttl_hours: 24
  max_store_gb: 5
  export_dir: null        # required for results.export
fetch:
  max_rows: 50000
  max_pages: 20
plugins:
  enabled: []
  settings: {}
http:
  port: 8765
```

## Large results

A result of at most 200 rows and about 6,000 tokens comes back inline. Anything bigger is written to the local result store and the tool returns a **marker** instead: a `result_id` (such as `r_8c1f0a9d3e`), the row count, typed columns with units, nulls and min/max, a preview of the first and last rows with the time span, the provenance, the pagination state and three ready-made queries.

The model then works on the handle:

```sql
-- results_query: one read-only SELECT; result ids are table names
SELECT ticker, time_bucket(INTERVAL '1 day', t) AS day, last(close ORDER BY t) AS close
FROM r_8c1f0a9d3e GROUP BY ALL ORDER BY day

-- joins of results, window functions and ASOF JOIN work too
SELECT a.t, a.close, b.close AS benchmark
FROM r_8c1f0a9d3e a ASOF JOIN r_51b0c2d4e6 b ON a.t >= b.t
```

`results_query` accepts exactly one `SELECT` or `WITH ... SELECT` statement. It refuses writes, `PRAGMA`, `SET`, `ATTACH`, `COPY`, `INSTALL`, `LOAD`, file-reading functions and any table that is not a result of the same session; it runs in a fresh in-memory DuckDB that loaded only the referenced results and then disabled file access and locked its configuration. It returns at most `max_rows` rows (default 50, at most 200), stops after 10 seconds, and stores its own answer as a new result when it is still too big (or when you pass `store=true`).

Upstream fetches stop at 50,000 rows or 20 pages and say so, with the token to continue. Results live 24 hours in a per-session folder of your cache directory (`$MARKETLENS_CACHE_DIR`, else `~/Library/Caches/marketlens`, `%LOCALAPPDATA%\marketlens\Cache` or `~/.cache/marketlens`), capped at 5 GB with oldest-first eviction. No tool ever shows a filesystem path. `marketlens-mcp results list` shows the store's size; `results purge --yes` empties it.

## Analytics

The `analytics_*` tools take result handles and compute in DuckDB: simple and log returns (optionally by period), rolling annualised volatility (window stated), a correlation matrix, OHLCV resampling (aggregation rules stated), an as-of alignment of two results, drawdown (maximum and series), and beta against a benchmark result. Inputs are validated against the result's typed columns; large outputs are stored like any other result.

## Tools

Generated from the built-in tool manifest (`make readme`). `marketlens-mcp tools --markdown` prints the same table for your installed configuration, plugins included.

<!-- tools:start -->
| Tool | Capability (default) | Provider route | Returns | Env vars | Notes |
|---|---|---|---|---|---|
| `crypto_bars` | market (on) | `GET /v1beta3/crypto/{loc}/bars (market data API)` (alpaca) | marketlens.Bar | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | OHLCV bars for crypto pairs |
| `crypto_latest_bars` | market (on) | `GET /v1beta3/crypto/{loc}/latest/bars (market data API)` (alpaca) | marketlens.Bar | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest minute bar per pair |
| `crypto_latest_quotes` | market (on) | `GET /v1beta3/crypto/{loc}/latest/quotes (market data API)` (alpaca) | marketlens.Quote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest quote per pair |
| `crypto_latest_trades` | market (on) | `GET /v1beta3/crypto/{loc}/latest/trades (market data API)` (alpaca) | marketlens.Trade | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest trade per pair |
| `crypto_orderbooks` | market (on) | `GET /v1beta3/crypto/{loc}/latest/orderbooks (market data API)` (alpaca) | marketlens.OrderBookLevel | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Order book levels per pair |
| `crypto_quotes` | market (on) | `GET /v1beta3/crypto/{loc}/quotes (market data API)` (alpaca) | marketlens.Quote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Historical crypto quotes |
| `crypto_snapshots` | market (on) | `GET /v1beta3/crypto/{loc}/snapshots (market data API)` (alpaca) | marketlens.Snapshot | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Last trade, quote, day bar, change |
| `crypto_trades` | market (on) | `GET /v1beta3/crypto/{loc}/trades (market data API)` (alpaca) | marketlens.Trade | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Historical crypto trades |
| `fixed_income_latest_quotes` | market (on) | `GET /v1beta1/fixed_income/latest/quotes (market data API)` (alpaca) | marketlens.FixedIncomeQuote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest bond quotes and yields |
| `market_bars` | market (on) | `GET /v2/stocks/bars (market data API)` (alpaca) | marketlens.Bar | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | OHLCV bars for stocks |
| `market_latest_bars` | market (on) | `GET /v2/stocks/bars/latest (market data API)` (alpaca) | marketlens.Bar | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest minute bar per ticker |
| `market_latest_quotes` | market (on) | `GET /v2/stocks/quotes/latest (market data API)` (alpaca) | marketlens.Quote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest NBBO quote per ticker |
| `market_latest_trades` | market (on) | `GET /v2/stocks/trades/latest (market data API)` (alpaca) | marketlens.Trade | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest trade per ticker |
| `market_most_active` | market (on) | `GET /v1beta1/screener/stocks/most-actives (market data API)` (alpaca) | marketlens.MostActive | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Most active stocks today |
| `market_movers` | market (on) | `GET /v1beta1/screener/{market_type}/movers (market data API)` (alpaca) | marketlens.Mover | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Top gainers and losers |
| `market_quotes` | market (on) | `GET /v2/stocks/quotes (market data API)` (alpaca) | marketlens.Quote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Historical NBBO quotes |
| `market_snapshots` | market (on) | `GET /v2/stocks/snapshots (market data API)` (alpaca) | marketlens.Snapshot | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Last trade, quote, day bar, change |
| `market_trades` | market (on) | `GET /v2/stocks/trades (market data API)` (alpaca) | marketlens.Trade | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Historical trades |
| `options_bars` | market (on) | `GET /v1beta1/options/bars (market data API)` (alpaca) | marketlens.OptionBar | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | OHLCV bars for option contracts |
| `options_chain` | market (on) | `GET /v1beta1/options/snapshots/{underlying_symbol} (market data API)` (alpaca) | marketlens.OptionSnapshot | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Chain snapshots for an underlying |
| `options_latest_quotes` | market (on) | `GET /v1beta1/options/quotes/latest (market data API)` (alpaca) | marketlens.OptionQuote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest quote per contract |
| `options_latest_trades` | market (on) | `GET /v1beta1/options/trades/latest (market data API)` (alpaca) | marketlens.OptionTrade | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Latest trade per contract |
| `options_snapshots` | market (on) | `GET /v1beta1/options/snapshots (market data API)` (alpaca) | marketlens.OptionSnapshot | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Contract snapshots with greeks and IV |
| `options_trades` | market (on) | `GET /v1beta1/options/trades (market data API)` (alpaca) | marketlens.OptionTrade | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Historical option trades |
| `reference_asset` | reference (on) | `GET /v2/assets/{symbol_or_asset_id} (trading API)` (alpaca) | marketlens.Asset | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One asset's attributes |
| `reference_assets` | reference (on) | `GET /v2/assets (trading API)` (alpaca) | marketlens.Asset | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Asset list with trading attributes |
| `reference_calendar` | reference (on) | `GET /v2/calendar (trading API)` (alpaca) | marketlens.MarketCalendarDay | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Trading days and session times (UTC) |
| `reference_clock` | reference (on) | `GET /v2/clock (trading API)` (alpaca) | marketlens.MarketClock | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Market open now? Next open and close |
| `reference_corporate_action_announcement` | reference (on) | `GET /v2/corporate_actions/announcements/{id} (trading API)` (alpaca) | marketlens.CorporateActionAnnouncement | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One announcement by id |
| `reference_corporate_action_announcements` | reference (on) | `GET /v2/corporate_actions/announcements (trading API)` (alpaca) | marketlens.CorporateActionAnnouncement | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Announced dividends, splits, mergers |
| `reference_corporate_actions` | reference (on) | `GET /v1/corporate-actions (market data API)` (alpaca) | marketlens.CorporateAction | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Processed corporate actions |
| `reference_option_contract` | reference (on) | `GET /v2/options/contracts/{symbol_or_id} (trading API)` (alpaca) | marketlens.OptionContract | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One contract with deliverables |
| `reference_option_contracts` | reference (on) | `GET /v2/options/contracts (trading API)` (alpaca) | marketlens.OptionContract | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Option contract reference data |
| `reference_option_exchanges` | reference (on) | `GET /v1beta1/options/meta/exchanges (market data API)` (alpaca) | marketlens.OptionExchange | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Option exchange codes |
| `news_search` | news (on) | `GET /v1beta1/news (market data API)` (alpaca) | marketlens.NewsItem | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | untrusted text |
| `analytics_align` | analytics (on) | `duckdb:analytics_align` (local) | marketlens.Aligned | - | As-of join of two results (backward or forward, tolerance, by column); adds matched_t. |
| `analytics_beta` | analytics (on) | `duckdb:analytics_beta` (local) | marketlens.BetaResult | - | Beta, alpha and R squared versus a one-series benchmark; rolling with window. |
| `analytics_correlation` | analytics (on) | `duckdb:analytics_correlation` (local) | marketlens.CorrelationCell | - | Pairwise Pearson correlation (long form) over shared timestamps; prices become returns first. |
| `analytics_drawdown` | analytics (on) | `duckdb:analytics_drawdown` (local) | marketlens.DrawdownSummary | - | Maximum drawdown with peak, trough and recovery per series, or the drawdown series (mode=series). |
| `analytics_resample` | analytics (on) | `duckdb:analytics_resample` (local) | marketlens.Bar | - | Bars to a coarser timeframe (OHLCV rules stated in the notes); other series by one aggregation. |
| `analytics_returns` | analytics (on) | `duckdb:analytics_returns` (local) | marketlens.ReturnPoint | - | Simple or log returns per series, optionally per period (last price per UTC bucket). |
| `analytics_volatility` | analytics (on) | `duckdb:analytics_volatility` (local) | marketlens.VolatilityPoint | - | Rolling annualised volatility; window and periods per year stated on every row. |
| `portfolio_account` | portfolio (off) | `GET /v2/account (trading API)` (alpaca) | marketlens.Account | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Balances and account status |
| `portfolio_account_config` | portfolio (off) | `GET /v2/account/configurations (trading API)` (alpaca) | marketlens.AccountConfig | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Account trading settings (read only) |
| `portfolio_activities` | portfolio (off) | `GET /v2/account/activities (trading API)` (alpaca) | marketlens.Activity | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Fills, dividends, fees, transfers |
| `portfolio_broker_watchlist` | portfolio (off) | `GET /v2/watchlists/{watchlist_id} (trading API)` (alpaca) | marketlens.BrokerWatchlist | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One broker watchlist's tickers |
| `portfolio_broker_watchlists` | portfolio (off) | `GET /v2/watchlists (trading API)` (alpaca) | marketlens.BrokerWatchlist | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Broker watchlists (names) |
| `portfolio_history` | portfolio (off) | `GET /v2/account/portfolio/history (trading API)` (alpaca) | marketlens.PortfolioHistoryPoint | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Equity and P&L over time |
| `portfolio_locate` | portfolio (off) | `GET /v1/locates/{locate_id} (trading API)` (alpaca) | marketlens.Locate | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One locate by id |
| `portfolio_locate_quotes` | portfolio (off) | `GET /v1/locates/quotes (trading API)` (alpaca) | marketlens.LocateQuote | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Locate availability and fees |
| `portfolio_locates` | portfolio (off) | `GET /v1/locates (trading API)` (alpaca) | marketlens.Locate | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Short-sale locates (read only) |
| `portfolio_order` | portfolio (off) | `GET /v2/orders/{order_id} (trading API)` (alpaca) | marketlens.Order | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One order by id |
| `portfolio_orders` | portfolio (off) | `GET /v2/orders (trading API)` (alpaca) | marketlens.Order | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Order history (read only) |
| `portfolio_position` | portfolio (off) | `GET /v2/positions/{symbol_or_asset_id} (trading API)` (alpaca) | marketlens.Position | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | One open position |
| `portfolio_positions` | portfolio (off) | `GET /v2/positions (trading API)` (alpaca) | marketlens.Position | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | Open positions with P&L |
| `results_describe` | results (on) | `store:results_describe` (local) | marketlens.QueryRow | - | A stored result's marker: columns, preview, ready-made queries |
| `results_drop` | results (on) | `store:results_drop` (local) | marketlens.QueryRow | - | Delete one stored result |
| `results_list` | results (on) | `store:results_list` (local) | marketlens.QueryRow | - | This session's stored results, newest first |
| `results_query` | results (on) | `duckdb:results_query` (local) | marketlens.QueryRow | - | Read-only SQL over this session's results (one SELECT, forced LIMIT) |
| `results_sample` | results (on) | `duckdb:results_sample` (local) | marketlens.QueryRow | - | First, last or random rows of a stored result |
| `results_export` | results.export (off) | `file:results_export` (local) | marketlens.QueryRow | - | Write a stored result to CSV or Parquet in results.export_dir |
| `provider_docs_fetch` | provider.docs (off) | `mcp:fetch (docs.alpaca.markets/mcp)` (alpaca-docs) | marketlens.ProviderDocument | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | untrusted text |
| `provider_docs_get_endpoint` | provider.docs (off) | `mcp:get-endpoint (docs.alpaca.markets/mcp)` (alpaca-docs) | marketlens.ProviderDocument | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | untrusted text |
| `provider_docs_list_endpoints` | provider.docs (off) | `mcp:list-endpoints (docs.alpaca.markets/mcp)` (alpaca-docs) | marketlens.ProviderDocument | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | untrusted text |
| `provider_docs_search` | provider.docs (off) | `mcp:search (docs.alpaca.markets/mcp)` (alpaca-docs) | marketlens.ProviderDocument | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | untrusted text |
| `provider_docs_search_endpoints` | provider.docs (off) | `mcp:search-endpoints (docs.alpaca.markets/mcp)` (alpaca-docs) | marketlens.ProviderDocument | ALPACA_API_KEY, ALPACA_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY | untrusted text |
<!-- tools:end -->

## Canonical schema

Every response uses the vendor-neutral models in `marketlens_schema` (pydantic only, so a data pipeline can use them without the server): tickers in SEC style (`BRK-B`; crypto pairs as `BTC/USD`), options by compact OCC symbol, instants in UTC ISO-8601, exact money as decimal strings, a unit on every numeric field, percentages as fractions. A missing value is `null`, never 0, and every `null` carries a reason (`no_data`, `not_entitled`, `not_applicable`, ...). Every response carries a provenance block: provider, route, feed and delay, as-of time, the normalised request, pages fetched, truncation. `marketlens-mcp schema --out DIR` writes the JSON Schemas; the built-in ones are in [`schema/`](schema/).

## Security and privacy

- **Read-only**: no tool writes to your brokerage account or any provider; the only file a tool can write is an export into the folder you configure, and only with `results.export` on.
- **What leaves your machine**: the requests tools make to the provider you configured, and whatever the model reads, which goes to your model provider with the conversation. Brokerage reads are off by default for that reason.
- **Untrusted text**: every result is wrapped in a `_marketlens` envelope that tells the model to treat it as data; news and documentation text carry a stronger notice.
- **No telemetry**: marketlens-mcp sends nothing anywhere except the upstream calls tools make. The FastMCP banner and its update check are off.
- **HTTP**: `serve --transport http` listens only on `127.0.0.1` or `::1`, requires `MARKETLENS_HTTP_TOKEN` (at least 32 characters) as a bearer token on every request, and checks the Host and Origin headers.
- **Local store**: results are Parquet files under your cache directory, owner-only permissions, deleted after 24 hours.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Plugins and providers

A Python package can add capabilities, tools and canonical models through the `marketlens.plugins` entry-point group. Plugins load only when you name them in `plugins.enabled`; a broken plugin is reported (`marketlens-mcp plugins`) and skipped, never stops the server. Installing a plugin is trusting its code: plugins run in the server's process. See [WRITING_A_PLUGIN.md](WRITING_A_PLUGIN.md), which also covers writing a provider for another data vendor, and [ADDING_A_CAPABILITY.md](ADDING_A_CAPABILITY.md) for mapping new Alpaca endpoints.

## Development

```sh
make venv install   # .venv with the package and dev tools (uv)
make test           # pytest (no network, no keys)
make lint           # ruff check + format check
make readme schema  # regenerate the tool table and schema/
make check          # lint + tests + generated-file checks
```

`marketlens-mcp add-capability NAME --capability ID --provider alpaca|local [--operation OPID]` scaffolds a new tool with its golden test. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE). The bundled Alpaca OpenAPI specifications and the adapted maintenance routine come from alpacahq/alpaca-mcp-server (MIT); see [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
