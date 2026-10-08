# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). The canonical schema (`SCHEMA_VERSION`) and the plugin API (`PLUGIN_API_VERSION`) are versioned separately.

## [0.2.0] - Unreleased

### Added

- The `data` extra (`pip install "marketlens-mcp[data]"`, Python 3.13 or later): a built-in provider over marketlens-data (import name `omni`) for official data, 25 tools under five new capabilities:
  - `macro` (on): FRED/ALFRED series with vintages and `as_of` reads, BLS series, BEA NIPA tables (a `Level` value carries a note that BEA's multiplier is not stored), BLS release schedules, the FRED series catalogue.
  - `filings` (on): SEC EDGAR filings, XBRL facts with restatement vintages, point-in-time fundamentals, 8-K Item 2.02 earnings releases and press-release EPS, Form 4 and 144 insider transactions, 13F holdings, fund N-PORT reports and holdings.
  - `fed_treasury` (on): FOMC meetings and statements, NY Fed reference rates (SOFR, EFFR, OBFR, TGCR, BGCR), the Treasury par yield curve, Treasury auctions, debt to the penny and the Treasury General Account.
  - `holidays` (on): US market holidays and early closes from NYSE, SIFMA and OPM, keyless; with Alpaca keys NYSE's dates are cross-checked against Alpaca's trading calendar, and without them the answer says they were not.
  - `calendars` (off: the Nasdaq endpoint is unofficial): the economic calendar with PMI and other actual and consensus figures, the earnings calendar and earnings history.
- Each source names its own key (`FRED_API_KEY`, `BEA_API_KEY`; `BLS_API_KEY` optional; `SEC_CONTACT_EMAIL` optional but strongly recommended for SEC fair access: without it marketlens-data sends a placeholder contact and every `sec_` answer carries a note saying so; the rest keyless); `doctor` reports which sources are ready, where the data store is and in which mode.
- `providers.data` settings: `mode` (`auto` fetches and keeps, `local` reads only what is stored), `data_dir`, `ttl_hours`, `calendar_ttl_minutes`, `call_timeout_seconds`.
- Schema 1.1.0: 23 canonical models for the data tools and the units `as_published`, `USD_per_share`, `times`, `ordinal`.
- The README tool table lists the data tools whether or not the extra is installed, marked "needs the data extra".
- `market_movers` takes `min_price` (USD) and, for stocks, `exclude_warrants_rights_units`, judged by symbol alone: five letters ending in W, R or U (Nasdaq's fifth letter: NRSNW, CHARR, CCAQU) or a WS, RT or U suffix (AAC-WS, BCAT-RT). With either, Alpaca's top 50 gainers and losers are screened, kept in Alpaca's order, ranked from 1 among the kept and cut to `top`, and the notes say how many were dropped for which reason and when fewer than `top` passed. Without them Alpaca is asked for `top` as before and the rows are unchanged.
- A dynamic table (`ToolOutput(table=...)` with a schema name for `model`) labels a column's unit in its Arrow field's metadata (`{b"unit": b"percent"}`), and the result's columns carry it, inline or stored. The plugin API is unchanged: an older server ignores the metadata. The analytics tools read those units as they read a model's: a column labelled `price`, `USD` or `percent_of_par` is a price, so `analytics_correlation` correlates its simple returns rather than its levels, and one labelled `fraction` or `fraction_per_year` is a ratio, which `analytics_returns`, `analytics_drawdown` and `analytics_beta` refuse.

### Changed

- Linux and macOS are the tested platforms: the classifiers name them instead of "OS Independent", and Windows left the CI matrix (not yet supported). Text I/O still names its encoding everywhere.
- A fetch cut at `fetch.max_rows` or `fetch.max_pages` without a page token no longer carries the R16 "call again with page_token" note: the tool's own truncation note stands, and a tool that gave none gets "The data is incomplete; narrow the request." (the answer is still flagged `truncated`).
- The name `data` is reserved for the built-in provider; a plugin may not take it.

### Fixed

- `analytics_align` takes `by_left` and `by_right` when the two results name the series column differently (bars' `ticker`, returns' `series`); `by` stays the shorthand for a shared name and is refused together with them.
- `results_query` with `store=true` (or an answer too large to show) over one result keeps that result's time and series columns and column units when they are still there with the same name and type, so `SELECT * FROM r_bars WHERE ticker='SPY'` chains into analytics as the bars would: the default value column is the parent model's (close), and `analytics_beta` names the benchmark SPY instead of its result id. A benchmark with no series column is named by its one value of the asset's series column when it has that column.
- The analytics tools take `event_time` as the time column of a result with no recorded one whose other timestamp columns are only the point-in-time clocks `knowledge_time` and `ingested_at` (a datastore's values), with a note, instead of refusing it as having several timestamp columns.
- An Alpaca 403 for a data set the plan does not include gets a hint for its data family: stock data names `stock_feed`, options data `options_feed`, and anything else (fixed income, news) says no marketlens setting will enable it, instead of the stock and options feed advice for every endpoint.
- `market_latest_bars`, `market_latest_quotes` and `market_latest_trades` name the requested tickers Alpaca returned nothing for in a note, as the snapshot tools and the crypto and options latest tools already did, instead of dropping them silently.
- A `lookback` for bars of 1d or longer (`market_bars`, `crypto_bars`, `options_bars`) starts at 00:00 UTC of its first day, so that day's bar (stamped at midnight New York) is no longer dropped: `lookback=P1Y` at 2026-10-07T08:31Z starts at 2025-10-07T00:00Z, not 08:31Z. Intraday bars are unchanged.
- `crypto_bars` and `crypto_latest_bars` say when a bar had no trades: Alpaca still sends a bar for such an interval, with `trade_count` and volume 0 but prices and a vwap built from quotes, and a note now counts those bars and names their pairs (at most five), suggesting `trade_count > 0` to keep the traded ones. The rows are unchanged.
- `reference_option_contracts` and `reference_option_contract` say that `open_interest_date` is usually one trading day before `close_price_date` (OCC publishes open interest the next morning), in the tool descriptions and in the `OptionContract` column descriptions of both dates (schema descriptions only; the version stays 1.1.0).

## [0.1.0] - Unreleased

### Added

- A read-only MCP server on FastMCP 3.4.7, over stdio (the default, so `uvx marketlens-mcp` works) or streamable HTTP on 127.0.0.1 / ::1 with a bearer token.
- `marketlens_schema`: vendor-neutral canonical models (schema 1.0.0) for market, reference, brokerage and analytics data, with units on every numeric field, UTC timestamps, exact decimals, a reason for every absent value and a provenance block on every response; JSON Schema export.
- Capabilities the owner switches on and off in `~/.config/marketlens/config.yaml` (market, reference, news, analytics on; portfolio, results.export and provider.docs off; results always on), with one-line refusals for invalid settings.
- A DuckDB-over-Parquet result store: results above 200 rows or about 6,000 tokens are stored and returned as a marker (typed columns, preview, provenance, pagination, three ready-made queries); per-session folders, 24-hour lifetime, 5 GB cap with oldest-first eviction.
- Results tools: `results_query` (one guarded read-only SELECT with a forced LIMIT, run in a locked-down DuckDB), `results_describe`, `results_sample`, `results_list`, `results_drop`, and `results_export` (off by default).
- A trust envelope on every tool result; third-party text carries an untrusted-text notice.
- A plugin API (1.0) through the `marketlens.plugins` entry-point group: plugins load only when enabled, register atomically, and a broken plugin is skipped with a reason.
- The console script: `serve`, `tools [--json|--markdown] [--all]`, `capabilities`, `plugins`, `config path|show|init`, `doctor [--network]`, `schema`, `results list|purge`, `add-capability`, `readme`, `--version`.
- `marketlens_mcp.testing` (`temp_store`, `make_context`, `call_tool`) for plugin authors.
