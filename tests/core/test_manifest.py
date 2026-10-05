"""The manifest (contract 3.1): one ToolSpec per tool drives registration,
the policy check, the README table and the parity tests. This file holds the
manifest integrity test over every built-in spec."""

from __future__ import annotations

import dataclasses
import pathlib

import ml_example_plugin as ex
import pytest
from pydantic import BaseModel, ConfigDict

from marketlens_mcp import config as cfg
from marketlens_mcp import manifest, registry
from marketlens_mcp.plugin_api import TOOL_NAME_MAX, TOOL_NAME_RE, RegistrationError
from marketlens_schema import ALIGNED_SCHEMA_NAME, QUERY_ROW_SCHEMA_NAME

REPO = pathlib.Path(__file__).resolve().parents[2]


def builtin_catalog():
    return registry.build_catalog(cfg.load_config(), discover=list)


def test_every_builtin_spec_is_sound():
    cat = builtin_catalog()
    declared = {c.id for c in cat.capabilities}
    assert cat.tools, "built-ins register tools"
    for name, entry in cat.tools.items():
        spec = entry.spec
        assert spec.name == name
        assert TOOL_NAME_RE.match(name) and len(name) <= TOOL_NAME_MAX, name
        assert spec.capability in declared, name
        assert spec.description.strip() and len(spec.description) <= 1000, name
        assert spec.input_model.model_config.get("extra") == "forbid", name
        out = spec.output_model
        out_name = out if isinstance(out, str) else out.schema_name
        assert out_name in cat.models or out_name in (ALIGNED_SCHEMA_NAME, QUERY_ROW_SCHEMA_NAME), name
        golden = REPO / spec.golden_test
        assert golden.is_file(), f"{name}: golden test {spec.golden_test} is missing"
        assert name in golden.read_text(), f"{name}: {spec.golden_test} does not mention the tool"
        assert spec.readme.strip(), name


def test_validation_rules():
    cat = builtin_catalog()
    ok = dataclasses.replace(ex.THINGS, capability="market", output_model=QUERY_ROW_SCHEMA_NAME)
    manifest.validate_spec(ok, capabilities={"market"}, models=cat.models)

    def refused(match, **changes):
        with pytest.raises(RegistrationError, match=match):
            manifest.validate_spec(
                dataclasses.replace(ok, **changes), capabilities={"market"}, models=cat.models
            )

    refused("tool name", name="Market-Bars")
    refused("at most 40", name="a" * 41)
    refused("unknown capability", capability="nope")
    refused("description", description="  ")
    refused("description", description="x" * 1001)

    class Loose(BaseModel):
        model_config = ConfigDict(extra="ignore")

    refused("must forbid extra fields", input_model=Loose)
    refused("output model", output_model="nobody.Model")
    refused("handler", handler=lambda ctx, args: None)
    refused("output risk", output_risk="dangerous")


def test_input_schema_is_top_level_and_closed():
    schema = manifest.input_schema(ex.THINGS)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"count", "fail"}
    assert schema["properties"]["count"]["maximum"] == 1000


def test_annotations_are_forced_read_only():
    a = manifest.annotations(ex.THINGS)
    assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) == (
        True,
        False,
        True,
        False,
    )
    b = manifest.annotations(dataclasses.replace(ex.THINGS, provider="alpaca"))
    assert b.openWorldHint is True
    assert a.title == "Example things"
