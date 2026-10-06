"""doctor with marketlens-data installed: the extra, the store and one line
per source, in the source table's order; key values never printed."""

from __future__ import annotations

import re

from data_harness import KEYS

from marketlens_mcp import cli
from marketlens_mcp.providers.data import runtime

SOURCES = [
    "fred",
    "bls",
    "bea",
    "sec",
    "fomc",
    "nyfed",
    "treasury",
    "fiscaldata",
    "nasdaq",
    "nyse",
    "sifma",
    "opm",
]


def doctor(capsys):
    code = cli.main(["doctor"])
    return code, capsys.readouterr().out


def write_config(tmp_path, text):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_doctor_with_the_extra_and_keys(capsys, keys, tmp_path, monkeypatch):
    store = tmp_path / "data-store"
    monkeypatch.setenv("OMNI_DATA_DIR", str(store))
    code, out = doctor(capsys)
    assert code == 0, out
    lines = out.splitlines()
    extra = next(line for line in lines if line.startswith("data extra:"))
    assert re.fullmatch(
        r"data extra: installed (\(marketlens-data \S+; omni API ok\)|from a pre-release omni \(\S+ \S+; omni API ok\))",
        extra,
    ), extra
    i = lines.index(extra)
    assert lines[i + 1] == f"data store: ok ({store}; mode auto)"
    source_lines = lines[i + 2 : i + 2 + len(SOURCES)]
    assert [line.split(":")[0] for line in source_lines] == [f"data source {s}" for s in SOURCES]
    assert source_lines[0] == "data source fred: ready (FRED_API_KEY set)"
    assert source_lines[3] == "data source sec: ready (SEC_CONTACT_EMAIL set)"
    assert source_lines[8:] == [
        "data source nasdaq: capability calendars is off",
        "data source nyse: optional ALPACA_API_KEY and ALPACA_SECRET_KEY not set "
        "(optional cross-check of NYSE's calendar)",
        "data source sifma: keyless",
        "data source opm: keyless",
    ]
    assert "environment FRED_API_KEY: set" in out
    for value in KEYS.values():
        assert value not in out
    assert store.is_dir()


def test_doctor_sec_contact_not_set(capsys):
    # a contact address, not a key: optional, and no "free key" link
    _, out = doctor(capsys)
    sec = [line for line in out.splitlines() if line.startswith("data source sec:")]
    assert sec == [
        "data source sec: SEC_CONTACT_EMAIL not set (optional, strongly recommended; a placeholder contact "
        "is sent)"
    ]


def test_doctor_store_failure_counts_in_mode_auto_only(capsys, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    write_config(tmp_path, f"providers:\n  data:\n    data_dir: '{blocker / 'store'}'\n")
    code, out = doctor(capsys)
    assert code == 1
    assert re.search(r"^data store: FAILED \(.+ is not writable: \w+\)$", out, re.M)
    write_config(tmp_path, f"providers:\n  data:\n    mode: local\n    data_dir: '{blocker / 'store'}'\n")
    code, out = doctor(capsys)
    assert code == 0 and "data store: FAILED" in out
    assert "data source fred: optional FRED_API_KEY not set (only fetching needs it; mode is local)" in out


def test_doctor_reports_an_incomplete_omni(capsys, monkeypatch):
    monkeypatch.setattr(runtime, "REQUIRED_API", (*runtime.REQUIRED_API, "omni.calendars.nothing"))
    code, out = doctor(capsys)
    assert code == 1
    assert "data extra: FAILED (omni is importable but its API is incomplete: omni.calendars.nothing)" in out
    assert "data source" not in out
