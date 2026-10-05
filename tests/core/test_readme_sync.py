"""README tool table and schema/ are generated files: this fails when they
differ from a fresh generation. Runs only with MARKETLENS_CHECK_GENERATED=1
(make test-all, CI); regenerate with make readme schema."""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from marketlens_mcp import readme
from marketlens_schema import jsonschema

REPO = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("MARKETLENS_CHECK_GENERATED") != "1",
    reason="generated-file check (MARKETLENS_CHECK_GENERATED=1)",
)


def test_readme_tool_table_is_current():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert readme.replace_table(text, readme.builtin_table()) == text, "run: make readme"


def test_schema_folder_is_current():
    schemas = jsonschema.export_all()
    folder = REPO / "schema"
    expected = {f"{n}.json": s for n, s in schemas.items()}
    expected["index.json"] = jsonschema.index(schemas)
    on_disk = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")}
    assert on_disk == expected, "run: make schema"
