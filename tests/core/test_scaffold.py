"""The add-capability scaffold (contract 9.4)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from marketlens_mcp import cli, manifest, scaffold
from marketlens_schema import BUILTIN_MODELS

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/v2/stocks/auctions": {"get": {"operationId": "StockAuctions"}},
        "/v2/orders": {"post": {"operationId": "postOrder"}, "get": {"operationId": "getAllOrders"}},
    },
}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    specs = tmp_path / "src" / "marketlens_mcp" / "providers" / "alpaca" / "specs"
    specs.mkdir(parents=True)
    (specs / "market-data-api.json").write_text(json.dumps(SPEC), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def load_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_alpaca_tool_from_an_operation(checkout, capsys):
    assert (
        cli.main(
            [
                "add-capability",
                "market_auctions",
                "--capability",
                "market",
                "--provider",
                "alpaca",
                "--operation",
                "StockAuctions",
                "--model",
                "marketlens.Trade",
            ]
        )
        == 0
    )
    tool = checkout / "src/marketlens_mcp/providers/alpaca/tools/market_auctions.py"
    test = checkout / "tests/alpaca/test_market_auctions.py"
    # Named by its operation, as tests/alpaca/fixtures/README names every Alpaca
    # fixture, and without a "_synthetic" key (the README marks them all).
    fixture = checkout / "tests/alpaca/fixtures/StockAuctions.json"
    golden = checkout / "tests/alpaca/golden/market_auctions.json"
    for f in (tool, test, fixture, golden):
        assert f.is_file(), f
    assert not (checkout / "tests/alpaca/fixtures/market_auctions.json").exists()
    assert json.loads(fixture.read_text(encoding="utf-8")) == {}
    assert 'FIXTURE = HERE / "fixtures" / "StockAuctions.json"' in test.read_text(encoding="utf-8")
    assert json.loads(golden.read_text(encoding="utf-8")) == {}
    assert "market_auctions" in test.read_text(encoding="utf-8") and "pytest.mark.skip" in test.read_text(
        encoding="utf-8"
    )
    out = capsys.readouterr().out.strip()
    assert out == (
        "Add 'market_auctions' to MODULES in src/marketlens_mcp/providers/alpaca/tools/__init__.py, implement the "
        "handler, fill the fixture and golden file, then run make test and make readme."
    )
    mod = load_module(tool)
    (spec,) = mod.SPECS
    assert spec.name == "market_auctions" and spec.capability == "market"
    assert spec.route == "GET /v2/stocks/auctions" and spec.upstream_operations == ("StockAuctions",)
    assert spec.output_model is BUILTIN_MODELS["marketlens.Trade"]
    assert spec.golden_test == "tests/alpaca/test_market_auctions.py"
    assert spec.env == ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
    manifest.validate_spec(spec, capabilities={"market"}, models=BUILTIN_MODELS)


@pytest.mark.parametrize(
    ("argv", "commands"),
    [
        (
            [
                "market_auctions",
                "--capability",
                "market",
                "--provider",
                "alpaca",
                "--operation",
                "StockAuctions",
            ],
            (["check"], ["format", "--check"]),
        ),
        (
            ["analytics_skew", "--capability", "analytics", "--provider", "local"],
            (["check"], ["format", "--check"]),
        ),
        # A plugin's import sections follow its own project, so only the layout is checked.
        (
            ["myplugin_things", "--capability", "myplugin", "--provider", "local", "--package", "my_plugin"],
            (["format", "--check"],),
        ),
    ],
    ids=["alpaca", "local", "plugin"],
)
def test_the_scaffold_passes_make_lint_as_written(checkout, capsys, argv, commands):
    """2026-10-19: the tool module wrote its tuples with repr()'s single quotes, so
    `make lint` (ruff format --check) failed on it until `make fmt` was run."""
    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff is a dev dependency")
    root = checkout / "plugin" if "--package" in argv else checkout
    if "--package" in argv:
        argv = [*argv, "--root", str(root), "--model", "myplugin.Thing"]
    assert cli.main(["add-capability", *argv]) == 0
    written = sorted(root.rglob("*.py"))
    assert len(written) == 2, written
    for path in written:
        # Linted as if it were in this checkout, under this repository's ruff settings.
        stdin_name = str(REPO / path.relative_to(root))
        for command in commands:
            ruff = [sys.executable, "-m", "ruff", *command, "--stdin-filename", stdin_name, "-"]
            text = path.read_text(encoding="utf-8")
            run = {"input": text, "cwd": REPO, "capture_output": True, "encoding": "utf-8", "check": False}
            result = subprocess.run(ruff, **run)  # noqa: S603
            assert result.returncode == 0, f"{command} {path.name}: {result.stdout}{result.stderr}"


def test_refuses_to_overwrite(checkout, capsys):
    args = ["add-capability", "market_auctions", "--capability", "market", "--provider", "alpaca"]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert cli.main(args) == 1
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["Bad-Name", "--capability", "market", "--provider", "alpaca"], "tool name"),
        (["x" * 41, "--capability", "market", "--provider", "alpaca"], "at most 40"),
        (["market_x", "--capability", "Not An Id", "--provider", "alpaca"], "capability id"),
        (["market_x", "--capability", "market", "--provider", "alpaca", "--operation", "Nope"], "not found"),
        (
            ["market_x", "--capability", "market", "--provider", "alpaca", "--operation", "postOrder"],
            "GET only",
        ),
        (
            ["market_x", "--capability", "market", "--provider", "local", "--operation", "StockAuctions"],
            "alpaca",
        ),
        (
            ["market_x", "--capability", "market", "--provider", "alpaca", "--model", "marketlens.Nope"],
            "unknown model",
        ),
    ],
)
def test_refusals(checkout, capsys, argv, message):
    assert cli.main(["add-capability", *argv]) == 1
    assert message in capsys.readouterr().err


def test_local_analytics_tool(checkout):
    written = scaffold.add_capability("analytics_skew", capability="analytics", provider="local")
    assert pathlib.Path("src/marketlens_mcp/analytics/tools/analytics_skew.py") in written
    assert (checkout / "tests/analytics/test_analytics_skew.py").is_file()
    spec = load_module(checkout / "src/marketlens_mcp/analytics/tools/analytics_skew.py").SPECS[0]
    assert spec.provider == "local" and spec.env == ()


def test_outside_a_checkout_needs_package_and_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert (
        cli.main(["add-capability", "myplugin_things", "--capability", "myplugin", "--provider", "local"])
        == 1
    )
    assert "marketlens-mcp checkout" in capsys.readouterr().err
    root = tmp_path / "plugin"
    assert (
        cli.main(
            [
                "add-capability",
                "myplugin_things",
                "--capability",
                "myplugin",
                "--provider",
                "local",
                "--package",
                "my_plugin",
                "--root",
                str(root),
                "--model",
                "myplugin.Thing",
            ]
        )
        == 0
    )
    tool = root / "src/my_plugin/tools/myplugin_things.py"
    assert tool.is_file()
    assert (root / "tests/test_myplugin_things.py").is_file()
    assert (root / "tests/fixtures/myplugin_things.json").is_file()
    assert (root / "tests/golden/myplugin_things.json").is_file()
    spec = load_module(tool).SPECS[0]
    assert spec.output_model == "myplugin.Thing"
    assert spec.golden_test == "tests/test_myplugin_things.py"
    assert "Add 'myplugin_things' to MODULES in" in capsys.readouterr().out
