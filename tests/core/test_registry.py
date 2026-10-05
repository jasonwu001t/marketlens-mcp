"""Registration (contract 3.2): built-ins first (a failure stops the
server), then plugins named in plugins.enabled only, each registered
atomically or dropped whole with a reason. The example plugin's tool
example_things is exercised here."""

from __future__ import annotations

import ml_example_plugin as ex
import pytest
from coresupport import run

from marketlens_mcp import config as cfg
from marketlens_mcp import registry
from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, PluginInfo, ToolError
from marketlens_mcp.results_api import InlineResult
from marketlens_mcp.testing import call_tool, make_context
from marketlens_schema import BUILTIN_MODELS


class FakeEntryPoint:
    def __init__(self, name, obj=None, *, error=None, dist="example-dist"):
        self.name = name
        self.value = f"ml_example_plugin:{name}"
        self.distribution = dist
        self._obj, self._error = obj, error
        self.loaded = False

    def load(self):
        self.loaded = True
        if self._error:
            raise self._error
        return self._obj


def conf(tmp_path, text=""):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return cfg.load_config()


def catalog(tmp_path, eps, enabled=(), extra=""):
    text = f"plugins:\n  enabled: [{', '.join(enabled)}]\n" + extra
    return registry.build_catalog(conf(tmp_path, text), discover=lambda: list(eps))


def status(cat, name):
    return next(p for p in cat.plugins if p.name == name)


def test_builtins_only(tmp_path):
    cat = catalog(tmp_path, [])
    assert [c.id for c in cat.capabilities][:8] == [
        "market",
        "reference",
        "news",
        "analytics",
        "portfolio",
        "results",
        "results.export",
        "provider.docs",
    ]
    assert set(BUILTIN_MODELS) <= set(cat.models)
    assert {
        "results_query",
        "results_describe",
        "results_sample",
        "results_list",
        "results_drop",
        "results_export",
    } <= set(cat.tools)
    assert cat.tools["results_query"].plugin == "builtin:results"
    assert cat.plugins == []


def test_good_plugin_loads_when_enabled(tmp_path):
    ep = FakeEntryPoint("example", ex.PLUGIN)
    cat = catalog(tmp_path, [ep], enabled=["example"])
    assert ep.loaded
    st = status(cat, "example")
    assert (st.enabled, st.loaded, st.api_version, st.error, st.distribution) == (
        True,
        True,
        (1, 0),
        None,
        "example-dist",
    )
    assert cat.tools["example_things"].plugin == "example"
    assert cat.models["example.Thing"] is ex.Thing
    cap = next(c for c in cat.capabilities if c.id == "example")
    assert cap.declared_by == "example"
    assert cat.tools["example_things"].env == ("EXAMPLE_TOKEN", "EXAMPLE_HOME")


def test_plugin_not_enabled_is_never_imported(tmp_path):
    ep = FakeEntryPoint("example", error=AssertionError("imported although not enabled"))
    cat = catalog(tmp_path, [ep])
    assert not ep.loaded
    st = status(cat, "example")
    assert (st.enabled, st.loaded, st.error) == (False, False, None)
    assert "example_things" not in cat.tools


def test_enabled_but_not_installed(tmp_path):
    cat = catalog(tmp_path, [], enabled=["ghost"])
    st = status(cat, "ghost")
    assert (st.enabled, st.loaded, st.error) == (True, False, "not installed")


@pytest.mark.parametrize(
    ("name", "obj", "reason"),
    [
        ("raiser", ex.RAISES, "register() raised RuntimeError: boom while registering"),
        ("collider", ex.COLLIDES, "tool name 'results_query' is already registered"),
        ("future", ex.FUTURE, "plugin API 2.0 is not supported by this server (plugin API 1.0)"),
        ("newerminor", ex.NEWER_MINOR, f"plugin API 1.{PLUGIN_API_VERSION[1] + 1} is not supported"),
        ("badmodel", ex.BAD_MODEL, "model schema name 'marketlens.Sneaky' must start with 'badmodel.'"),
        ("redeclares", ex.REDECLARES, "capability 'market' is built in and cannot be declared again"),
        ("openargs", ex.OPEN_ARGS, "input model OpenArgs must forbid extra fields"),
    ],
)
def test_broken_plugins_are_skipped_whole(tmp_path, name, obj, reason):
    good = FakeEntryPoint("example", ex.PLUGIN)
    cat = catalog(tmp_path, [FakeEntryPoint(name, obj), good], enabled=[name, "example"])
    st = status(cat, name)
    assert st.loaded is False
    assert reason in st.error
    assert not any(c.declared_by == name for c in cat.capabilities)
    assert not any(e.plugin == name for e in cat.tools.values())
    assert not any(m.startswith(name + ".") for m in cat.models)
    assert status(cat, "example").loaded  # the others still load
    assert registry.skip_message(st) == f"plugin '{name}' not loaded: {st.error}"


def _exits_while_registering(registry, ctx):
    raise SystemExit("a dependency is missing")


@pytest.mark.parametrize(
    "ep",
    [
        FakeEntryPoint("exits", error=SystemExit(3)),
        FakeEntryPoint(
            "exits", PluginInfo(name="exits", api_version=(1, 0), register=_exits_while_registering)
        ),
    ],
    ids=["sys.exit at import", "sys.exit in register"],
)
def test_a_plugin_that_calls_sys_exit_is_skipped_not_fatal(tmp_path, ep):
    """A plugin module that calls sys.exit (say, on a missing dependency) is a broken
    plugin like any other: recorded and skipped, never the end of the server."""
    cat = catalog(tmp_path, [ep, FakeEntryPoint("example", ex.PLUGIN)], enabled=["exits", "example"])
    st = status(cat, "exits")
    assert st.loaded is False and "SystemExit" in st.error
    assert status(cat, "example").loaded


def test_wrong_object_and_wrong_name_and_reserved_name(tmp_path):
    eps = [
        FakeEntryPoint("notinfo", object()),
        FakeEntryPoint("misnamed", ex.PLUGIN),
        FakeEntryPoint("results", PluginInfo(name="results", api_version=(1, 0), register=ex.register)),
        FakeEntryPoint("crashes", error=ImportError("no module named crashes_dep")),
    ]
    cat = catalog(tmp_path, eps, enabled=["notinfo", "misnamed", "results", "crashes"])
    assert "is not a PluginInfo" in status(cat, "notinfo").error
    assert "names itself 'example'" in status(cat, "misnamed").error
    assert "reserved" in status(cat, "results").error
    assert "could not be imported: ImportError: no module named crashes_dep" in status(cat, "crashes").error


def test_duplicate_entry_points(tmp_path):
    cat = catalog(
        tmp_path,
        [FakeEntryPoint("example", ex.PLUGIN), FakeEntryPoint("example", ex.PLUGIN, dist="other")],
        enabled=["example"],
    )
    assert "installed twice" in status(cat, "example").error


def test_plugin_may_attach_tools_to_builtin_capabilities(tmp_path):
    cat = catalog(tmp_path, [FakeEntryPoint("holdingsx", ex.ON_PORTFOLIO)], enabled=["holdingsx"])
    assert cat.tools["holdingsx_things"].spec.capability == "portfolio"


def test_failing_builtin_stops_the_server(tmp_path, monkeypatch):
    import types

    bad = types.ModuleType("bad_builtin_for_test")

    def register(reg, ctx):
        raise RuntimeError("bug")

    bad.PLUGIN = PluginInfo(name="alpaca", api_version=(1, 0), register=register)
    monkeypatch.setitem(__import__("sys").modules, "bad_builtin_for_test", bad)
    with pytest.raises(registry.BuiltinPluginError) as info:
        registry.build_catalog(conf(tmp_path), discover=list, builtins=("bad_builtin_for_test:PLUGIN",))
    assert "bad_builtin_for_test" in str(info.value)


def test_plugin_context_env_is_limited_to_declared_names(tmp_path, monkeypatch):
    seen = {}

    def register(reg, ctx):
        seen["home"] = ctx.env("EXAMPLE_HOME")
        with pytest.raises(KeyError):
            ctx.env("ALPACA_SECRET_KEY")
        seen["settings"] = dict(ctx.settings)
        seen["env"] = ctx.portfolio_environment

    monkeypatch.setenv("EXAMPLE_HOME", "/somewhere")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "nope")
    info = PluginInfo(name="example", api_version=(1, 0), register=register, env=("EXAMPLE_HOME",))
    cat = catalog(
        tmp_path,
        [FakeEntryPoint("example", info)],
        enabled=["example"],
        extra="  settings:\n    example: {colour: blue}\n",
    )
    assert status(cat, "example").loaded, status(cat, "example").error
    assert seen == {"home": "/somewhere", "settings": {"colour": "blue"}, "env": "paper"}


def test_tool_context_env_is_limited_to_declared_names(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "tok")
    monkeypatch.setenv("ALPACA_API_KEY", "secret")
    cat = catalog(tmp_path, [FakeEntryPoint("example", ex.PLUGIN)], enabled=["example"])
    ctx = registry.ServerToolContext(
        tool="example_things",
        session_id="s",
        settings={},
        capabilities=frozenset(),
        portfolio_environment="paper",
        limits=registry.FetchLimits(),
        results=None,
        log=None,
        env_names=cat.tools["example_things"].env,
    )
    assert ctx.env("EXAMPLE_TOKEN") == "tok"
    assert ctx.env("EXAMPLE_HOME") is None
    with pytest.raises(KeyError):
        ctx.env("ALPACA_API_KEY")


def test_example_things_through_the_pipeline(store):
    ctx = make_context(tool="example_things", store=store)
    out = run(call_tool(ex.THINGS, ctx, count=4))
    assert isinstance(out, InlineResult)
    assert out.model == "example.Thing" and out.row_count == 4
    with pytest.raises(ToolError) as info:
        run(call_tool(ex.THINGS, ctx, fail=True))
    assert info.value.code == "example_failed"


def test_entry_point_discovery_failure_is_reported_not_fatal(tmp_path):
    def broken_discovery():
        raise RuntimeError("corrupt metadata")

    cat = registry.build_catalog(
        conf(tmp_path, "plugins:\n  enabled: [example]\n"), discover=broken_discovery
    )
    st = status(cat, "example")
    assert st.loaded is False
    assert "entry-point discovery failed: RuntimeError: corrupt metadata" in st.error
    assert "results_query" in cat.tools
