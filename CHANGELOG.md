# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). The canonical schema (`SCHEMA_VERSION`) and the plugin API (`PLUGIN_API_VERSION`) are versioned separately.

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
