"""The seeded interfaces stay verbatim (additive changes only), and
marketlens_mcp.testing has exactly the signatures the contract names.

The snapshot in fixtures/interface_snapshot.json was generated from the
contract's interface stubs before any implementation was written
(``python tests/core/test_interfaces.py --write`` with the stubs first on
PYTHONPATH). Every name, field and signature in it must still exist,
unchanged; new names are allowed (additive).
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import pathlib
import re
import sys
import typing

SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "interface_snapshot.json"
MODULES = (
    "marketlens_mcp",
    "marketlens_mcp.plugin_api",
    "marketlens_mcp.results_api",
    "marketlens_schema",
    "marketlens_schema.base",
    "marketlens_schema.market",
    "marketlens_schema.portfolio",
    "marketlens_schema.analytics",
)


def _norm(text: str) -> str:
    # Annotations print with module paths that differ between the stub tree and
    # the installed tree only by object addresses; strip those.
    return re.sub(r" at 0x[0-9a-f]+", "", text)


def _describe(obj: typing.Any) -> typing.Any:
    if inspect.isclass(obj):
        if issubclass(obj, enum.Enum):
            return {"kind": "enum", "members": {m.name: m.value for m in obj}}
        if hasattr(obj, "model_fields"):
            fields = {
                name: {
                    "annotation": _norm(repr(f.annotation)),
                    "required": f.is_required(),
                    "default": None
                    if f.is_required()
                    else _norm(repr(f.get_default(call_default_factory=True))),
                }
                for name, f in obj.model_fields.items()
            }
            classvars = {
                name: _norm(repr(getattr(obj, name))) for name in sorted(getattr(obj, "__class_vars__", ()))
            }
            config = {k: _norm(repr(v)) for k, v in sorted(obj.model_config.items())}
            return {"kind": "model", "fields": fields, "classvars": classvars, "config": config}
        if dataclasses.is_dataclass(obj):
            return {
                "kind": "dataclass",
                "fields": {f.name: _norm(str(f.type)) for f in dataclasses.fields(obj)},
                "methods": _methods(obj),
            }
        return {"kind": "class", "methods": _methods(obj), "bases": [b.__name__ for b in obj.__bases__]}
    if inspect.isfunction(obj):
        return {"kind": "function", "signature": _norm(str(inspect.signature(obj)))}
    if isinstance(obj, frozenset):
        return {"kind": "value", "repr": _norm(repr(sorted(obj, key=repr)))}
    if isinstance(obj, (str, int, float, bool, tuple, re.Pattern)) or obj is None:
        return {"kind": "value", "repr": _norm(repr(obj))}
    if isinstance(obj, dict):
        return {"kind": "mapping", "keys": sorted(map(str, obj))}
    return {"kind": "other", "type": type(obj).__name__}


def _methods(cls: type) -> dict[str, str]:
    out = {}
    for name, member in vars(cls).items():
        if name.startswith("_") and name != "__init__":
            continue
        if isinstance(member, property):
            out[name] = "property"
        elif inspect.isfunction(member):
            out[name] = _norm(str(inspect.signature(member)))
    return out


def snapshot() -> dict[str, dict[str, typing.Any]]:
    out: dict[str, dict[str, typing.Any]] = {}
    for modname in MODULES:
        mod = importlib.import_module(modname)
        names = getattr(mod, "__all__", None) or [
            n
            for n, v in vars(mod).items()
            if not n.startswith("_")
            and not inspect.ismodule(v)
            and getattr(v, "__module__", modname) == modname
        ]
        out[modname] = {n: _describe(getattr(mod, n)) for n in sorted(names)}
    return out


def _subset_problems(expected: typing.Any, actual: typing.Any, where: str) -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: was a mapping, now {actual!r}"]
        problems = []
        for key, value in expected.items():
            if key not in actual:
                problems.append(f"{where}.{key}: missing")
            else:
                problems.extend(_subset_problems(value, actual[key], f"{where}.{key}"))
        return problems
    if expected != actual:
        return [f"{where}: {expected!r} -> {actual!r}"]
    return []


def test_seeded_interfaces_are_unchanged_or_only_extended():
    expected = json.loads(SNAPSHOT.read_text())
    problems = _subset_problems(expected, snapshot(), "")
    assert problems == []


def test_testing_helpers_have_the_contract_signatures():
    from marketlens_mcp import testing

    assert str(inspect.signature(testing.temp_store)) == (
        "(root: 'pathlib.Path', *, session_key: 'str' = 'test', ttl_hours: 'float' = 24, "
        "max_bytes: 'int' = 5368709120) -> 'ResultStore'"
    )
    assert str(inspect.signature(testing.make_context)) == (
        "(*, tool: 'str' = 'test_tool', store: 'ResultStore | None' = None, "
        "settings: 'Mapping[str, Any] | None' = None, capabilities: 'Iterable[str] | None' = None, "
        "env: 'Mapping[str, str] | None' = None, limits: 'FetchLimits' = FetchLimits(max_rows=50000, max_pages=20), "
        "portfolio_environment: 'Environment' = <Environment.PAPER: 'paper'>, "
        "now: 'datetime | None' = None) -> 'ToolContext'"
    )
    assert str(inspect.signature(testing.call_tool)) == (
        "(spec: 'ToolSpec', ctx: 'ToolContext', **arguments) -> 'ToolResponse'"
    )
    assert inspect.iscoroutinefunction(testing.call_tool)


if __name__ == "__main__" and "--write" in sys.argv:
    SNAPSHOT.write_text(json.dumps(snapshot(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT}")
