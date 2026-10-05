# Adding a capability (keeping the Alpaca provider in step with Alpaca)

This is the routine for changing what the built-in Alpaca provider covers: when Alpaca publishes new specs, when you want a read endpoint that is not mapped yet, or when an endpoint changes. It is adapted from the spec-sync routine in alpacahq/alpaca-mcp-server's AGENTS.md (MIT; see THIRD_PARTY_NOTICES). To add a different data vendor instead, write a provider plugin: see "Writing a provider" in [WRITING_A_PLUGIN.md](WRITING_A_PLUGIN.md).

Ground rules that every change keeps:

- **v1 is read-only.** Only GET endpoints are mapped. Writes (orders, positions, exercises, watchlist edits, locates, account changes, transfers) are listed in `EXCLUDED_OPERATIONS` with kind `write` and are never registered; there is no setting that enables them. The HTTP wrapper exposes `get()` only. A source scan fails if `.post(`, `.put(`, `.patch(`, `.delete(`, `.request(`, `.stream(`, `send(`, `build_request(`, `httpx.Request(` or a quoted `"POST"`, `"PUT"`, `"PATCH"` or `"DELETE"` appears anywhere under `src/marketlens_mcp/providers/alpaca/` (the documentation lookup's `method` argument is the one allowed line). On the wire, the tests' fake Alpaca answers GET only and fails any test whose code sent another method, and `tests/alpaca/test_alpaca_read_only.py` replays every tool's golden case and asserts each request is a GET.
- **Every operation is accounted for.** The parity test (`tests/alpaca/test_parity.py`) fails unless every operationId in both pinned spec files is either mapped by exactly one tool (`ToolSpec.upstream_operations`) or listed in `src/marketlens_mcp/providers/alpaca/exclusions.py`, never both.
- **Canonical output.** Tools return canonical rows (`marketlens_schema`), not Alpaca JSON: SEC-style tickers (`BRK-B`), compact OCC symbols, UTC timestamps, exact decimals for money, units in `x-unit`, a provenance block, and a reason for every `None` (never 0 for a missing value).

Where things live:

| What | Where |
|---|---|
| Pinned specs | `src/marketlens_mcp/providers/alpaca/specs/{trading-api,market-data-api}.json` |
| HTTP client (keys, base URLs, limiter, retries, error codes) | `providers/alpaca/client.py` |
| Raw value conversions | `providers/alpaca/convert.py` |
| Raw object -> canonical row | `providers/alpaca/mappers*.py` |
| Tools (input model, handler, `SPECS`) | `providers/alpaca/tools/<module>.py`, listed in `tools/__init__.py` `MODULES` |
| Not mapped, and why | `providers/alpaca/exclusions.py` |
| Tests, fixtures, golden outputs | `tests/alpaca/`, `tests/alpaca/fixtures/`, `tests/alpaca/golden/` |

## 1. Sync the specs

```bash
make sync-alpaca-specs        # runs scripts/sync-alpaca-specs.sh
```

The script downloads `https://docs.alpaca.markets/openapi/trading-api.json` and `market-data-api.json` into the specs folder and prints their sha256. Note both values; step 4 pins them. (The copies first taken from alpaca-mcp-server declare `"openapi": "3.1.1"` where Alpaca publishes 3.1.2; that one-line difference is expected on the first sync and harmless, because marketlens-mcp does not generate tools from the specs.)

## 2. Diff and classify every change

```bash
git diff src/marketlens_mcp/providers/alpaca/specs/
```

Put each change in one of three groups.

**A. An endpoint that is already mapped changed** (parameters, response fields, enums, descriptions). Mappers and input models are written by hand, so nothing follows the spec automatically. For each such endpoint:

1. List the spec's query and path parameters and compare them with the tool's input model. Add a useful new parameter (optional, with a description the model can act on); remove one the API no longer accepts; update descriptions and enums that changed.
2. List the response fields and compare them with the mapper. Map a new field if a canonical field exists for it; if the canonical model lacks one, that is a schema change (minor version bump, made in `marketlens_schema` with its own tests) before the mapper can use it.
3. Check units and conventions again: percent vs fraction, round lots vs shares, empty string vs missing, 0 meaning "no value".
4. Update the fixture and the golden file for that tool.

**B. A new endpoint.**

- Read-only and useful for analysis: map it (step 3).
- A write of any kind: add it to `EXCLUDED_OPERATIONS` as `Exclusion("write", ...)` (v1 maps GET only).
- Otherwise exclude it with the kind that fits: `streaming` (server-sent events), `out_of_scope` (funding, wallets, tokenization), `candidate` (a read worth adding later), `covered` (a single-symbol variant of a mapped list endpoint), `not_data` (binary content). Give a reason a reader can check.

**C. An endpoint was removed or renamed.** This breaks a tool or an exclusion. Remove the tool (or move it to the new operationId), remove the stale exclusion, and record the change under "Removed" or "Changed" in CHANGELOG.md.

## 3. Map an endpoint

Scaffold the files:

```bash
marketlens-mcp add-capability <tool_name> --capability <id> --provider alpaca --operation <operationId> [--model <SchemaName>]
```

Then fill them in:

1. **Name and capability.** `<domain>_<thing>`, snake_case, at most 40 characters, domain one of `market_`, `crypto_`, `options_`, `fixed_income_`, `reference_`, `news_`, `portfolio_`, `provider_docs_`. The capability decides who sees the tool: `market`, `reference`, `news` (on by default), `portfolio` (off by default: brokerage data), `provider.docs` (off by default).
2. **Model.** Reuse a canonical model when the meaning matches (any bar is a `Bar`, any snapshot of a stock or pair is a `Snapshot`). A new model is a schema change (minor bump) with its own JSON Schema and tests.
3. **Input model.** Subclass `tools.common.Inputs` (unknown arguments refused). Use the shared field types: `EquityTickers`, `CryptoPairs`, `OccSymbols`, `Instant`, `Duration`, `TimeframeIn`. Describe defaults, units and limits in each field's description: that text is what the model reads.
4. **Handler.** Open `AlpacaClient(ctx)`; call `api.get("data" | "trading", path, params)` (`decimals=True` for money); page list endpoints through `ctx.paginate` (or `marketdata.fetch_grouped` / `fetch_once`), sizing each upstream page from the `limit` the paginator passes; map records with `collect(...)` so a record that does not fit the model is skipped and noted instead of failing the call; return `output(...)` with the provenance fields (route template, operationId, feed and delay where they apply, `environment` for brokerage reads, `as_of`, the normalised request).
5. **Absence.** Every `None` needs a reason: per row through `build_row(..., codes={...})`, or once for the whole response in the output's `absent` map when a field never applies to that endpoint.
6. **Spec.** Add a `spec(...)` entry to the module's `SPECS` with `operations=(...)` (every operationId the tool calls) and `parity=(...)` (the alpaca-mcp-server tool names it covers, if any), `risk="external_text"` for third-party prose, `golden_test` pointing at the test module. Add the module to `MODULES` if it is new.
7. **Fixture and golden.** Write `tests/alpaca/fixtures/<operationId>[__case].json` by hand from the spec's schema and examples (synthetic values, no recordings, no keys) and a test that calls the tool through `alpaca_harness.call` and `check_golden`. Generate the golden file once with `MARKETLENS_UPDATE_GOLDEN=1`, then read it line by line: tickers, timestamps, units, absence codes, provenance. Add the same case to `HTTP_CASES` in `tests/alpaca/test_alpaca_read_only.py` (a tool without one fails there).

## 4. Validate

```bash
make test
```

This runs the parity test (operations and alpaca-mcp-server tools accounted for, GET only, no write names, pinned spec hashes), the manifest test (names, capabilities, descriptions, input models, golden files), the golden tests (with the absence rule) and the client tests. After a spec sync the hash check fails on purpose: update `PINNED_SHA256` in `tests/alpaca/test_parity.py` to the values step 1 printed, and nothing else.

## 5. Document

```bash
make readme schema
```

Regenerates the tool table in README.md and the JSON Schemas. Add a CHANGELOG.md entry: tools added (with capability), endpoints excluded (with kind), behaviour changes, breaking changes.

## 6. Commit

One commit per sync or endpoint, with a message that lists:

- what changed in the specs;
- tools added or changed, and their capability;
- endpoints excluded, and why;
- breaking changes (removed or renamed endpoints and tools);
- that README, schemas, fixtures, golden files and the pinned hashes were updated.

## Adding an official-data tool (the data extra)

The `data` provider (`src/marketlens_mcp/providers/data/`) reads publishers through marketlens-data (import name `omni`) rather than calling them itself, so a new official-data tool starts from a marketlens-data dataset:

1. **Find the dataset and its identity.** `omni.list_datasets()` names every dataset; `src/marketlens_mcp/providers/data/parity.py` says which are mapped and why the rest are not. The tool's *identity* is the set of fetch params it passes (`{"series_id": ...}`, `{"ticker": ...}`, or `{}`): freshness is kept per identity, so pass only what names the slice and filter dates and columns after the read.
2. **Write the tool** in `providers/data/tools/<module>.py`: an input model on `tools.common.Inputs` (with `as_of`), a handler that runs its omni work through `common.run(...)` (the single worker thread, the timeout and the error mapping), `Session.refresh(dataset_id, identity)` then `Session.read(dataset_id, as_of=..., **column_filters)`, canonical rows built with `common.row(...)` (every `None` explained), and `common.provenance(...)` / `common.output(...)` (row cap included). Convert units in the handler: percents to fractions, millions and billions to USD.
3. **Describe it**: its description carries the dataset's point-in-time line (`common.describe(text, policy)`), the key it needs, and the "stored" note; `ToolSpec.env` names the key; `DATASETS` maps the tool to its dataset ids.
4. **Account for it** in `parity.PARITY` (Mapped), and the key in `sources.SOURCES` if the source is new (doctor prints its line).
5. **Test it** in `tests/data/test_<module>.py` with a synthetic fixture in the publisher's own shape, served by the fake publisher of `tests/data/data_harness.py` behind marketlens-data's HTTP client, and a golden file (`MARKETLENS_UPDATE_GOLDEN=1`, then read it). `make test-data` runs them; they need the extra installed.

## Adding a provider instead

A different vendor (another broker, a data API, a local database) is a plugin, not a change to this provider: map its responses onto the existing canonical models, prefix its tools with its own name, set `provenance.provider` to the vendor, keep its own exclusion list and parity test against the vendor's API. See [WRITING_A_PLUGIN.md](WRITING_A_PLUGIN.md).
