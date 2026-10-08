"""The data extra as the server sees it, with or without marketlens-data
installed: the five capabilities are always declared, the 25 tools are
registered only when omni is importable, the README always lists them, the
config file knows their settings, and doctor says what is missing. Nothing
here imports omni (tests/data covers the tools themselves)."""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from marketlens_mcp import builtins, cli, readme, registry
from marketlens_mcp import config as cfg
from marketlens_mcp.providers import data
from marketlens_mcp.providers.data import parity, runtime, sources, tools

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = {
    "macro": [
        "macro_series",
        "macro_series_catalog",
        "macro_bls_series",
        "macro_bea_table",
        "macro_release_schedule",
    ],
    "filings": [
        "sec_filings",
        "sec_xbrl_facts",
        "sec_fundamentals",
        "sec_earnings_releases",
        "sec_earnings_figures",
        "sec_insider_trades",
        "sec_13f_holdings",
        "sec_fund_nport",
        "sec_fund_holdings",
    ],
    "fed_treasury": [
        "fed_fomc_meetings",
        "fed_fomc_statements",
        "fed_reference_rates",
        "treasury_yield_curve",
        "treasury_auctions",
        "treasury_debt",
        "treasury_tga",
    ],
    "holidays": ["calendar_us_holidays"],
    "calendars": [
        "calendar_economic",
        "calendar_earnings",
        "calendar_earnings_history",
    ],
}
ALL_TOOLS = [t for names in TOOLS.values() for t in names]
EXTERNAL_TEXT = {"sec_earnings_figures", "fed_fomc_statements", "calendar_economic"}


def catalog(monkeypatch, *, installed: bool):
    monkeypatch.setattr(runtime, "available", lambda: installed)
    return registry.build_catalog(cfg.load_config(), discover=list)


def test_the_provider_is_built_in_and_its_name_reserved():
    assert builtins.BUILTIN_PLUGINS[-1] == "marketlens_mcp.providers.data:PLUGIN"
    assert "data" in registry.BUILTIN_PLUGIN_NAMES
    assert data.PLUGIN.name == "data"
    assert data.PLUGIN.env == (
        "FRED_API_KEY",
        "BLS_API_KEY",
        "BEA_API_KEY",
        "SEC_CONTACT_EMAIL",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "OMNI_DATA_DIR",
    )


@pytest.mark.parametrize("installed", [False, True])
def test_capabilities_are_declared_either_way(monkeypatch, installed):
    cat = catalog(monkeypatch, installed=installed)
    caps = {c.id: c for c in cat.capabilities}
    assert cat.capability_ids[-5:] == ["macro", "filings", "fed_treasury", "holidays", "calendars"]
    assert [caps[i].default_enabled for i in TOOLS] == [True, True, True, True, False]
    for cap_id in TOOLS:
        assert caps[cap_id].declared_by == "builtin"
        assert "data extra" in caps[cap_id].description
    assert "unofficial" in caps["calendars"].description
    assert "unofficial" not in caps["holidays"].description
    assert "holidays" not in caps["calendars"].description
    names = set(cat.tools)
    assert (set(ALL_TOOLS) <= names) is installed
    assert not (set(ALL_TOOLS) & names) or installed


def test_the_25_tools(monkeypatch):
    cat = catalog(monkeypatch, installed=True)
    for cap_id, names in TOOLS.items():
        for name in names:
            entry = cat.tools[name]
            assert entry.spec.capability == cap_id, name
            assert entry.plugin == "builtin:data"
            assert entry.spec.output_risk == ("external_text" if name in EXTERNAL_TEXT else "api_structured")
            assert entry.spec.golden_test.startswith("tests/data/test_"), name
    assert len([t for t in cat.tools.values() if t.plugin == "builtin:data"]) == 25
    specs = {s.name: s for s in tools.all_specs()}
    assert set(specs) == set(ALL_TOOLS)
    assert specs["macro_series"].env == ("FRED_API_KEY",)
    assert specs["macro_bls_series"].env == ("BLS_API_KEY",)
    assert specs["macro_bea_table"].env == ("BEA_API_KEY",)
    for name in TOOLS["filings"]:
        assert specs[name].env == ("SEC_CONTACT_EMAIL",), name
        # the contact is optional: unset, a placeholder is sent and the answer says so
        assert "Fetching needs" not in specs[name].description, name
        assert "Without SEC_CONTACT_EMAIL a placeholder contact is sent" in specs[name].description, name
    assert specs["calendar_us_holidays"].env == ()  # the Alpaca cross-check is optional
    assert specs["macro_series_catalog"].env == ()
    for name in TOOLS["fed_treasury"]:
        assert specs[name].env == (), name
    assert "PMI" in specs["calendar_economic"].description
    assert "no free official PMI source" in specs["calendar_economic"].description
    for name, spec in specs.items():
        assert "as_of" in spec.input_model.model_fields or name == "macro_series_catalog", name


@pytest.mark.parametrize(("installed", "count"), [(False, 47), (True, 69)])
def test_tools_json_counts(monkeypatch, capsys, installed, count):
    monkeypatch.setattr(runtime, "available", lambda: installed)
    assert cli.main(["tools", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert len(body["tools"]) == count
    caps = {c["id"]: c for c in body["capabilities"]}
    assert caps["calendars"] == {
        "id": "calendars",
        "enabled": False,
        "default": False,
        "source": "default",
        "declared_by": "builtin",
    }
    assert caps["macro"]["enabled"] is True
    assert caps["holidays"]["enabled"] is True


def test_capabilities_command_lists_the_five(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "available", lambda: False)
    assert cli.main(["capabilities"]) == 0
    out = capsys.readouterr().out
    for cap_id in TOOLS:
        assert f"\n{cap_id} " in out, cap_id


def test_doctor_without_the_extra(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "available", lambda: False)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert (
        "data extra: not installed; install marketlens-mcp[data] (Python 3.13 or later) to list the "
        "macro, filings, fed_treasury, holidays and calendars tools\n"
    ) in out
    assert "data source" not in out and "data store" not in out


def test_readme_lists_the_data_tools_whether_or_not_installed(monkeypatch):
    monkeypatch.setattr(runtime, "available", lambda: False)
    without = readme.builtin_table()
    monkeypatch.setattr(runtime, "available", lambda: True)
    assert readme.builtin_table() == without
    rows = {line.split("|")[1].strip().strip("`"): line for line in without.splitlines()[2:]}
    for name in ALL_TOOLS:
        assert "needs the data extra" in rows[name], name
    assert "| macro (on) |" in rows["macro_series"]
    assert "| calendars (off) |" in rows["calendar_economic"]
    assert "| holidays (on) |" in rows["calendar_us_holidays"]
    assert "| marketlens.MarketHoliday | - |" in rows["calendar_us_holidays"]  # no key needed
    assert "untrusted text; needs the data extra" in rows["fed_fomc_statements"]
    assert "| FRED_API_KEY |" in rows["macro_series"]
    assert "marketlens.EconomicObservation" in rows["macro_series"]
    assert "needs the data extra" not in rows["market_bars"]


def test_readme_documents_the_five_capabilities_and_the_keys():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    for cap_id, default in (
        ("macro", "on"),
        ("filings", "on"),
        ("fed_treasury", "on"),
        ("holidays", "on"),
        ("calendars", "off"),
    ):
        assert f"| `{cap_id}` | {default} |" in text, cap_id
    for name in (
        "FRED_API_KEY",
        "BEA_API_KEY",
        "BLS_API_KEY",
        "SEC_CONTACT_EMAIL",
        "EIA_API_KEY",
        "CENSUS_API_KEY",
    ):
        assert name in text, name
    assert 'uvx --from "marketlens-mcp[data]" marketlens-mcp' in text
    assert 'pipx install "marketlens-mcp[data]"' in text


def test_readme_data_setup_strongly_recommends_the_sec_contact():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    setup = text.split("With the `data` extra, run it from the extra", 1)[1].split("Claude Code:", 1)[0]
    assert "`SEC_CONTACT_EMAIL` is optional but strongly recommended" in setup
    assert "refuse" not in setup
    for words in ("placeholder contact", "logs a warning", "throttle or block", "carries a note"):
        assert words in setup, words


def test_readme_keys_table_says_the_sec_contact_is_optional():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| SEC EDGAR |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1:3] == ["`SEC_CONTACT_EMAIL`", "optional, strongly recommended"]
    assert "placeholder contact" in cells[3] and "throttle or block" in cells[3]


# --- configuration ---------------------------------------------------------------------------


def write(tmp_path, text):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def refusal(tmp_path, text):
    write(tmp_path, text)
    with pytest.raises(cfg.ConfigError) as info:
        cfg.load_config()
    return str(info.value).split(": ", 1)[1]


def test_default_config_text_names_the_data_settings():
    text = cfg.DEFAULT_CONFIG_TEXT
    assert "  analytics: true" in text
    lines = text.splitlines()
    i = lines.index(next(line for line in lines if line.startswith("  analytics: true")))
    assert (
        lines[i + 1].startswith("  macro: true") and "# FRED, BLS, BEA (needs the data extra)" in lines[i + 1]
    )
    assert lines[i + 2].startswith("  filings: true")
    assert lines[i + 3].startswith("  fed_treasury: true")
    assert lines[i + 4].startswith("  holidays: true") and "NYSE, SIFMA, OPM" in lines[i + 4]
    assert lines[i + 5].startswith("  calendars: false") and "an unofficial endpoint" in lines[i + 5]
    for line in (
        "  data:",
        "    mode: auto            # auto (fetch and keep) | local (read only what is stored)",
        "    data_dir: null        # absolute folder for the data store; default: the per-user data folder",
        "    ttl_hours: 12         # how long a fetched slice is reused before it is fetched again",
        "    calendar_ttl_minutes: 60",
        "    call_timeout_seconds: 120",
    ):
        assert line in lines, line
    assert cfg.DEFAULTS["providers"]["data"] == {
        "mode": "auto",
        "data_dir": None,
        "ttl_hours": 12,
        "calendar_ttl_minutes": 60,
        "call_timeout_seconds": 120,
    }


def test_data_settings_load_and_merge(tmp_path):
    write(tmp_path, f"providers:\n  data:\n    mode: local\n    data_dir: '{tmp_path / 'store'}'\n")
    c = cfg.load_config()
    assert c.provider_settings("data") == {
        "mode": "local",
        "data_dir": str(tmp_path / "store"),
        "ttl_hours": 12,
        "calendar_ttl_minutes": 60,
        "call_timeout_seconds": 120,
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "providers:\n  data:\n    mode: live\n",
            "providers.data.mode must be one of auto, local (got 'live').",
        ),
        (
            "providers:\n  data:\n    data_dir: relative/store\n",
            "providers.data.data_dir must be an absolute path (got 'relative/store').",
        ),
        (
            "providers:\n  data:\n    data_dir: 7\n",
            "providers.data.data_dir must be an absolute path (got 7).",
        ),
        (
            "providers:\n  data:\n    ttl_hours: 0\n",
            "providers.data.ttl_hours must be between 1 and 168 (got 0).",
        ),
        (
            "providers:\n  data:\n    calendar_ttl_minutes: 4\n",
            "providers.data.calendar_ttl_minutes must be between 5 and 1440 (got 4).",
        ),
        (
            "providers:\n  data:\n    call_timeout_seconds: 601\n",
            "providers.data.call_timeout_seconds must be between 10 and 600 (got 601).",
        ),
        (
            "providers:\n  data:\n    keys: {}\n",
            "unknown setting 'providers.data.keys'. Known settings here: mode, data_dir, ttl_hours, "
            "calendar_ttl_minutes, call_timeout_seconds.",
        ),
    ],
)
def test_data_settings_are_validated(tmp_path, text, message):
    assert refusal(tmp_path, text) == message


def test_config_show_lists_the_data_keys_as_set_or_unset(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "fred-synthetic-key-0000")
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "someone@example.com")
    monkeypatch.setenv("BEA_API_KEY", "")
    text = cfg.load_config().effective_yaml()
    assert "fred-synthetic-key-0000" not in text and "someone@example.com" not in text
    assert "# FRED_API_KEY: set" in text
    assert "# BEA_API_KEY: unset" in text
    assert "# BLS_API_KEY: unset" in text
    assert "# SEC_CONTACT_EMAIL: set" in text
    assert {"FRED_API_KEY", "BEA_API_KEY", "BLS_API_KEY"} <= set(cfg.SECRET_ENV)
    assert yaml.safe_load(text)["providers"]["data"]["mode"] == "auto"


# --- where the store lives -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", {"HOME": "/h"}, "/h/Library/Application Support/marketlens-data"),
        ("linux", {"HOME": "/h"}, "/h/.local/share/marketlens-data"),
        ("linux", {"HOME": "/h", "XDG_DATA_HOME": "/xdg"}, "/xdg/marketlens-data"),
        ("linux", {"HOME": "/h", "XDG_DATA_HOME": "rel"}, "/h/.local/share/marketlens-data"),
        (
            "win32",
            {"USERPROFILE": "C:/Users/u", "LOCALAPPDATA": "C:/Users/u/AppData/Local"},
            "C:/Users/u/AppData/Local/marketlens-data",
        ),
        ("win32", {"USERPROFILE": "C:/Users/u"}, "C:/Users/u/AppData/Local/marketlens-data"),
    ],
)
def test_default_data_dir(platform, env, expected):
    assert runtime.default_data_dir(env, platform=platform).as_posix() == expected


def test_store_path_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {"HOME": "/h", "OMNI_DATA_DIR": "/from/env"}
    assert (
        runtime.store_path({"data_dir": "/from/config"}, env, platform="linux").as_posix() == "/from/config"
    )
    assert runtime.store_path({"data_dir": None}, env, platform="linux").as_posix() == "/from/env"
    assert runtime.store_path({}, {"HOME": "/h"}, platform="linux").as_posix() == (
        "/h/.local/share/marketlens-data"
    )
    assert runtime.store_path({}, {"HOME": "/h", "OMNI_DATA_DIR": ""}, platform="linux").as_posix() == (
        "/h/.local/share/marketlens-data"
    )
    rel = runtime.store_path({}, {"HOME": "/h", "OMNI_DATA_DIR": "rel/store"}, platform="linux")
    assert rel == (tmp_path / "rel" / "store").resolve()


def test_settings_defaults_and_overrides():
    s = runtime.settings_of({})
    assert (s.mode, s.data_dir, s.ttl_seconds, s.calendar_ttl_seconds, s.call_timeout_seconds) == (
        "auto",
        None,
        12 * 3600,
        60 * 60,
        120,
    )
    s = runtime.settings_of(
        {"mode": "local", "ttl_hours": 2, "calendar_ttl_minutes": 5, "call_timeout_seconds": 30}
    )
    assert (s.mode, s.ttl_seconds, s.calendar_ttl_seconds, s.call_timeout_seconds) == ("local", 7200, 300, 30)


# --- keys per source -------------------------------------------------------------------------


def test_source_table_order_and_keys():
    assert [s.source for s in sources.SOURCES] == [
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
    by = {s.source: s for s in sources.SOURCES}
    assert (by["fred"].variables, by["fred"].required) == (("FRED_API_KEY",), True)
    assert (by["bls"].variables, by["bls"].required) == (("BLS_API_KEY",), False)
    assert (by["bea"].variables, by["bea"].required) == (("BEA_API_KEY",), True)
    assert (by["sec"].variables, by["sec"].required) == (("SEC_CONTACT_EMAIL",), False)
    assert by["nyse"].variables == ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") and not by["nyse"].required
    assert [by[s].capability for s in ("nasdaq", "nyse", "sifma", "opm")] == [
        "calendars",
        "holidays",
        "holidays",
        "holidays",
    ]
    assert by["fred"].url == "https://fred.stlouisfed.org/docs/api/api_key.html"
    assert by["bea"].url == "https://apps.bea.gov/API/signup/"
    assert by["bls"].url == "https://data.bls.gov/registrationEngine/"
    assert by["sec"].url == "https://www.sec.gov/os/accessing-edgar-data"


ALL_ON = {"macro", "filings", "fed_treasury", "holidays", "calendars"}


def lines(env, enabled=ALL_ON, mode="auto"):
    return [s.line for s in sources.readiness(env, enabled, mode=mode)]


def test_readiness_lines_with_nothing_set():
    assert lines({}) == [
        "data source fred: missing FRED_API_KEY; its tools will refuse until it is set "
        "(free key: https://fred.stlouisfed.org/docs/api/api_key.html)",
        "data source bls: optional BLS_API_KEY not set (free, raises BLS's daily limit and years per "
        "request: https://data.bls.gov/registrationEngine/)",
        "data source bea: missing BEA_API_KEY; its tools will refuse until it is set "
        "(free key: https://apps.bea.gov/API/signup/)",
        "data source sec: SEC_CONTACT_EMAIL not set (optional, strongly recommended; a placeholder "
        "contact is sent)",
        "data source fomc: keyless",
        "data source nyfed: keyless",
        "data source treasury: keyless",
        "data source fiscaldata: keyless",
        "data source nasdaq: keyless",
        "data source nyse: optional ALPACA_API_KEY and ALPACA_SECRET_KEY not set "
        "(optional cross-check of NYSE's calendar)",
        "data source sifma: keyless",
        "data source opm: keyless",
    ]


def test_readiness_lines_with_keys_and_capabilities_off():
    env = {
        "FRED_API_KEY": "x" * 32,
        "BLS_API_KEY": "y" * 32,
        "BEA_API_KEY": "z" * 32,
        "SEC_CONTACT_EMAIL": "me@example.com",
        "ALPACA_API_KEY": "k" * 20,
        "ALPACA_SECRET_KEY": "s" * 40,
    }
    out = lines(env, enabled={"macro", "filings", "fed_treasury"})
    assert out[:4] == [
        "data source fred: ready (FRED_API_KEY set)",
        "data source bls: ready (BLS_API_KEY set)",
        "data source bea: ready (BEA_API_KEY set)",
        "data source sec: ready (SEC_CONTACT_EMAIL set)",
    ]
    assert out[8:] == [
        "data source nasdaq: capability calendars is off",
        "data source nyse: capability holidays is off",
        "data source sifma: capability holidays is off",
        "data source opm: capability holidays is off",
    ]
    assert lines(env, enabled={"holidays"})[8:] == [
        "data source nasdaq: capability calendars is off",
        "data source nyse: ready (ALPACA_API_KEY and ALPACA_SECRET_KEY set)",
        "data source sifma: keyless",
        "data source opm: keyless",
    ]
    for value in env.values():
        assert not any(value in line for line in out)
    # a value that is set is sent as the contact: the sec_ tools refuse one that is not an email address
    malformed = sources.readiness({**env, "SEC_CONTACT_EMAIL": "not-an-email"}, ALL_ON)[3]
    assert (malformed.state, malformed.line) == (
        "missing",
        "data source sec: SEC_CONTACT_EMAIL is not an email address; its tools refuse to fetch until it is",
    )
    assert sources.readiness({}, ALL_ON)[3].state == "optional"


def test_readiness_in_local_mode_needs_no_key():
    out = lines({}, mode="local")
    assert out[0] == "data source fred: optional FRED_API_KEY not set (only fetching needs it; mode is local)"
    assert out[3] == (
        "data source sec: SEC_CONTACT_EMAIL not set (optional, strongly recommended; a placeholder "
        "contact is sent)"
    )
    assert sources.readiness({"SEC_CONTACT_EMAIL": "x"}, ALL_ON, mode="local")[3].state == "optional"


# --- parity ----------------------------------------------------------------------------------


def test_parity_list_shape():
    assert len(parity.PARITY) == 59
    mapped = {k: v for k, v in parity.PARITY.items() if isinstance(v, parity.Mapped)}
    excluded = {k: v for k, v in parity.PARITY.items() if isinstance(v, parity.Excluded)}
    assert (len(mapped), len(excluded)) == (27, 32)
    kinds = [v.kind for v in excluded.values()]
    assert {k: kinds.count(k) for k in set(kinds)} == {
        "covered": 7,
        "second_wave": 16,
        "not_planned": 7,
        "internal": 2,
    }
    specs = {s.name for s in tools.all_specs()}
    for dataset_id, entry in mapped.items():
        assert entry.tools and set(entry.tools) <= specs, dataset_id
    by_tool = tools.datasets_by_tool()
    assert set(by_tool) == specs
    for tool, datasets in by_tool.items():
        for dataset_id in datasets:
            assert tool in mapped[dataset_id].tools, (tool, dataset_id)
    for dataset_id, entry in mapped.items():
        for tool in entry.tools:
            assert dataset_id in by_tool[tool] or tool == "macro_series_catalog", (dataset_id, tool)
    assert parity.PARITY["eia.series"] == parity.Excluded(
        "second_wave", "official source planned for a later release (needs EIA_API_KEY)"
    )
    assert parity.PARITY["alpaca.news"] == parity.Excluded(
        "covered", "read directly from Alpaca by the built-in Alpaca provider"
    )
    # Schedule 13D/G stakes and 13F cover pages: official SEC data that no tool reads yet.
    for dataset_id in ("sec_ownership.stakes", "sec13f.covers"):
        assert parity.PARITY[dataset_id] == parity.Excluded(
            "second_wave", "official source planned for a later release"
        ), dataset_id
    # The CSV archive ends in 2019, but marketlens-data reads every later session from Cboe's daily page.
    assert parity.PARITY["cboe.put_call_ratio"] == parity.Excluded(
        "not_planned",
        "the CSV archive ends in 2019; later sessions come only from scraping a fragile HTML page",
    )
