"""marketlens-mcp is general purpose: no file in the repository names the
private application it was first built for, its components, or their
default address. The needles are assembled here so this file does not
contain them literally."""

from __future__ import annotations

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
NEEDLES = [
    "alpha" + "research",
    "at" + "las",
    "quant" + "ai",
    "127.0.0.1" + ":8000",
]
PATTERN = re.compile("|".join(re.escape(n) for n in NEEDLES), re.IGNORECASE)


def repository_files() -> list[pathlib.Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        files = [REPO / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in REPO.rglob("*") if ".git" not in p.parts]
    return [f for f in files if f.is_file()]


def test_no_private_application_names_anywhere():
    hits = []
    for path in repository_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}")
    assert hits == []
