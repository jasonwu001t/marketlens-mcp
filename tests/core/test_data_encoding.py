"""The data provider's own text I/O names its encoding. Its tests run beside
marketlens-data, whose I/O this repository does not control, so they cannot
use the EncodingWarning-as-error rule; this checks the provider's source
instead."""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "marketlens_mcp"
FILES = sorted((SRC / "providers" / "data").rglob("*.py")) + [SRC / "cli.py", SRC / "readme.py"]
#: The builtin open(), and the text helpers of pathlib and os (os.open and a
#: store's .open() are not text I/O).
BUILTIN = {"open"}
METHODS = {"read_text", "write_text", "fdopen"}


def _is_binary(call: ast.Call) -> bool:
    modes = [a for a in call.args[1:2] if isinstance(a, ast.Constant)]
    modes += [k.value for k in call.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
    return any(isinstance(m, ast.Constant) and "b" in str(m.value) for m in modes)


def test_text_io_names_its_encoding():
    offenders = []
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                text_call = node.func.attr in METHODS
            else:
                text_call = getattr(node.func, "id", "") in BUILTIN
            if text_call and not _is_binary(node) and "encoding" not in {k.arg for k in node.keywords}:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
