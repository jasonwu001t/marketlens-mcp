"""The analytics package as a built-in plugin: seven manifest entries under the
``analytics`` capability, registered through ``PLUGIN``; output models in the
canonical Arrow types; no tool reaches the network.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from analytics_harness import schema_of

from marketlens_mcp.analytics import PLUGIN
from marketlens_mcp.analytics.common import canonical_schema
from marketlens_mcp.analytics.tools import MODULES, all_specs
from marketlens_mcp.plugin_api import (
    BUILTIN_CAPABILITIES,
    PLUGIN_API_VERSION,
    TOOL_NAME_MAX,
    TOOL_NAME_RE,
    CapabilitySpec,
    ToolSpec,
)
from marketlens_schema import ALIGNED_SCHEMA_NAME, BUILTIN_MODELS, QUERY_ROW_SCHEMA_NAME
from marketlens_schema.analytics import ANALYTICS_MODELS

REPO = Path(__file__).resolve().parents[2]
NAMES = (
    "analytics_returns",
    "analytics_volatility",
    "analytics_correlation",
    "analytics_resample",
    "analytics_align",
    "analytics_drawdown",
    "analytics_beta",
)


class RecordingRegistry:
    def __init__(self):
        self.tools: list[ToolSpec] = []
        self.capabilities: list[CapabilitySpec] = []
        self.models: list = []

    def add_capability(self, spec):
        self.capabilities.append(spec)

    def add_tool(self, spec):
        self.tools.append(spec)

    def add_model(self, model):
        self.models.append(model)


def test_plugin_registers_exactly_the_seven_tools():
    reg = RecordingRegistry()
    assert PLUGIN.name == "analytics" and PLUGIN.api_version == PLUGIN_API_VERSION and PLUGIN.env == ()
    PLUGIN.register(reg, ctx=None)
    assert tuple(s.name for s in reg.tools) == NAMES
    assert reg.capabilities == [] and reg.models == []  # capability and models are built-in
    assert len(MODULES) == 7 and tuple(s.name for s in all_specs()) == NAMES


def test_analytics_capability_is_builtin_and_on_by_default():
    (cap,) = [c for c in BUILTIN_CAPABILITIES if c.id == "analytics"]
    assert cap.default_enabled and not cap.locked


@pytest.mark.parametrize("spec", all_specs(), ids=lambda s: s.name)
def test_manifest_entry(spec: ToolSpec):
    assert TOOL_NAME_RE.match(spec.name) and len(spec.name) <= TOOL_NAME_MAX
    assert spec.capability == "analytics"
    assert spec.provider == "local"
    assert spec.route == f"duckdb:{spec.name}"
    assert 0 < len(spec.description) <= 1000
    assert spec.readme and len(spec.readme) <= 200
    assert spec.title
    assert spec.input_model.model_config.get("extra") == "forbid"
    out = spec.output_model
    assert (
        (out in (ALIGNED_SCHEMA_NAME, QUERY_ROW_SCHEMA_NAME))
        if isinstance(out, str)
        else (BUILTIN_MODELS[out.schema_name] is out)
    )
    assert inspect.iscoroutinefunction(spec.handler)
    assert spec.output_risk == "api_structured"
    assert spec.upstream_operations == () and spec.parity_names == () and spec.env == ()
    golden = REPO / spec.golden_test
    assert golden.is_file() and spec.name in golden.read_text(encoding="utf-8")
    schema = spec.input_model.model_json_schema()
    ids = [k for k in schema["properties"] if k.endswith("result_id")]
    assert ids and all(schema["properties"][k].get("pattern") == "^r_[0-9a-f]{10}$" for k in ids)


@pytest.mark.parametrize("model", ANALYTICS_MODELS, ids=lambda m: m.schema_name)
def test_canonical_schema_of_the_output_models(model):
    assert canonical_schema(model).equals(schema_of(model))
    assert canonical_schema(model).names[-1] == "absent"


def test_importing_the_package_needs_no_server_modules():
    import marketlens_mcp.analytics.common as common

    source = Path(common.__file__).read_text(encoding="utf-8")
    for forbidden in ("fastmcp", "marketlens_mcp.results.", "marketlens_mcp.registry", "httpx", "socket"):
        assert forbidden not in source
