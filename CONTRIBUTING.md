# Contributing

Thanks for helping. marketlens-mcp is a small, read-only server; the rules below keep it that way.

## Set up

```sh
make venv install    # .venv with the package (editable) and pytest, ruff (needs uv)
make test            # the suite: no network, no keys, nothing outside temp folders
make lint            # ruff check + ruff format --check
```

`make check` runs lint, the tests and the generated-file checks that CI runs.

The data provider's tests (`tests/data`) need the `data` extra (Python 3.13): `make install-data`, then `make test-data`. Without the extra they are skipped. Before marketlens-data is on PyPI, install it from a checkout: `make install-data MARKETLENS_DATA_SRC=../omni`.

## Ground rules

- **Read-only.** v1 maps read operations only. A pull request that adds a tool which places, changes or cancels anything upstream will not be merged, and no setting may make one possible.
- **Tests first.** Every behaviour has a test that failed before the change. Tests never touch the network (use `httpx.MockTransport`) and never need real keys; fixtures are synthetic or trimmed from the bundled specifications' examples and say so.
- **No paths, no secrets** in responses, errors or logs sent to the model. Secrets come from the environment only.
- **The canonical schema and the plugin API are versioned.** Adding an optional field or a model is a minor schema change; renaming, removing or changing a type or unit is a major one. The same holds for `marketlens_mcp.plugin_api` and `marketlens_mcp.results_api` (`PLUGIN_API_VERSION`). `tests/core/test_interfaces.py` guards them.
- **Generated files** are regenerated, never edited: run `make readme schema` after changing a tool's manifest entry or a model, and commit the result.
- Keep the code style ruff enforces (`make fmt`).

## Adding a tool

- For a new Alpaca endpoint, follow [ADDING_A_CAPABILITY.md](ADDING_A_CAPABILITY.md).
- For another data vendor, or tools that belong to one application, write a plugin instead: [WRITING_A_PLUGIN.md](WRITING_A_PLUGIN.md).
- `marketlens-mcp add-capability NAME --capability ID --provider alpaca|local [--operation OPID] [--model SCHEMA_NAME]` writes the skeleton: the tool module with its manifest entry, a golden test, a fixture and a golden file.

Every tool's manifest entry (`ToolSpec`) names its capability, its input and output models, its upstream route and operations, and the test module that exercises it; `tests/core/test_manifest.py` checks all of them.

## Pull requests

- One topic per pull request, with tests and a `CHANGELOG.md` entry under "Unreleased".
- Describe what changed for users (new tools and their capability, changed defaults, breaking changes).
- CI runs the suite on Linux and macOS with Python 3.11 and 3.13 (Windows is not yet supported).
