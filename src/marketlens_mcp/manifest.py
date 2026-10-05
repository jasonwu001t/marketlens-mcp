"""The manifest: validation of one ToolSpec and the MCP surface derived from
it (contract 3.1).

Input schema = ``input_model.model_json_schema()`` as top-level properties,
``additionalProperties: false``. Annotations are forced read-only:
``readOnlyHint=True, destructiveHint=False, idempotentHint=True,
openWorldHint=(provider != "local")``. No output schema: results are
structured content inside the trust envelope.
"""

from __future__ import annotations

import inspect
from collections.abc import Collection, Mapping
from typing import Any, get_args

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from marketlens_schema import ALIGNED_SCHEMA_NAME, QUERY_ROW_SCHEMA_NAME, CanonicalModel

from .plugin_api import TOOL_NAME_MAX, TOOL_NAME_RE, RegistrationError, ToolSpec
from .results_api import OutputRisk

DYNAMIC_MODELS = frozenset({ALIGNED_SCHEMA_NAME, QUERY_ROW_SCHEMA_NAME})
DESCRIPTION_MAX = 1000


def output_model_name(spec: ToolSpec) -> str:
    out = spec.output_model
    return out if isinstance(out, str) else out.schema_name


def validate_spec(
    spec: ToolSpec, *, capabilities: Collection[str], models: Mapping[str, type[CanonicalModel]]
) -> None:
    """Raise RegistrationError naming the first rule ``spec`` breaks."""
    if not isinstance(spec, ToolSpec):
        raise RegistrationError(f"add_tool needs a ToolSpec, got {type(spec).__name__}")
    name = spec.name
    if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        raise RegistrationError(f"tool name {name!r} must be snake_case ASCII, starting with a letter")
    if len(name) > TOOL_NAME_MAX:
        raise RegistrationError(f"tool name {name!r} must be at most {TOOL_NAME_MAX} characters")
    if spec.capability not in capabilities:
        raise RegistrationError(f"tool {name!r} names an unknown capability {spec.capability!r}")
    if not isinstance(spec.description, str) or not spec.description.strip():
        raise RegistrationError(f"tool {name!r} needs a description")
    if len(spec.description) > DESCRIPTION_MAX:
        raise RegistrationError(f"tool {name!r} has a description longer than {DESCRIPTION_MAX} characters")
    if not (isinstance(spec.input_model, type) and issubclass(spec.input_model, BaseModel)):
        raise RegistrationError(f"tool {name!r}: input model must be a pydantic model")
    if spec.input_model.model_config.get("extra") != "forbid":
        raise RegistrationError(
            f"tool {name!r}: input model {spec.input_model.__name__} must forbid extra fields (extra='forbid')"
        )
    out = spec.output_model
    if isinstance(out, type):
        if not issubclass(out, CanonicalModel) or models.get(out.schema_name) is not out:
            raise RegistrationError(
                f"tool {name!r}: output model {getattr(out, '__name__', out)!r} is not registered"
            )
    elif not isinstance(out, str) or (out not in models and out not in DYNAMIC_MODELS):
        raise RegistrationError(
            f"tool {name!r}: output model {out!r} is not a registered model or a dynamic name"
        )
    if not inspect.iscoroutinefunction(spec.handler):
        raise RegistrationError(f"tool {name!r}: handler must be an async function (ctx, args) -> ToolOutput")
    if spec.output_risk not in get_args(OutputRisk):
        raise RegistrationError(
            f"tool {name!r}: output risk {spec.output_risk!r} is not one of {get_args(OutputRisk)}"
        )
    if not isinstance(spec.golden_test, str) or not spec.golden_test.strip():
        raise RegistrationError(f"tool {name!r} needs golden_test (the test module that exercises it)")
    for env_name in spec.env:
        if not isinstance(env_name, str) or not env_name:
            raise RegistrationError(f"tool {name!r}: env names must be non-empty strings")


def input_schema(spec: ToolSpec) -> dict[str, Any]:
    schema = spec.input_model.model_json_schema()
    schema["type"] = "object"
    schema.setdefault("properties", {})
    schema["additionalProperties"] = False
    return schema


def annotations(spec: ToolSpec) -> ToolAnnotations:
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=spec.provider != "local",
    )
