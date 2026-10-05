"""JSON Schema export of the canonical models (schema versioning, base.py).

Each schema carries ``$id: urn:marketlens:schema:<major>:<schema_name>`` and
``x-schema-version``. Depends on pydantic only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import BUILTIN_MODELS
from .base import SCHEMA_VERSION, CanonicalModel

MAJOR = SCHEMA_VERSION.split(".", 1)[0]


def model_schema(model: type[CanonicalModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    return {
        "$id": f"urn:marketlens:schema:{MAJOR}:{model.schema_name}",
        "x-schema-version": SCHEMA_VERSION,
        **schema,
    }


def export_all(models: Mapping[str, type[CanonicalModel]] | None = None) -> dict[str, dict[str, Any]]:
    """schema name -> JSON Schema for every model (default: the built-ins)."""
    source = BUILTIN_MODELS if models is None else models
    return {name: model_schema(source[name]) for name in sorted(source)}


def index(schemas: Mapping[str, Any]) -> dict[str, Any]:
    """The ``index.json`` written next to the schema files."""
    return {"schema_version": SCHEMA_VERSION, "models": {name: f"{name}.json" for name in sorted(schemas)}}
