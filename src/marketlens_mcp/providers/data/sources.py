"""Which key each data source needs, and the doctor's readiness lines.

Every source names its own variable. Keys are read from the server's
environment only (the env block of the MCP client's configuration); this
module never shows a value, only whether one is set.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

State = Literal["ready", "missing", "optional", "keyless", "off"]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_email(value: str | None) -> bool:
    return bool(value) and bool(_EMAIL.match(value.strip()))


@dataclass(frozen=True)
class SourceKey:
    source: str  # the marketlens-data source name
    label: str  # how messages name it ("FRED needs FRED_API_KEY ...")
    capability: str
    variables: tuple[str, ...] = ()
    required: bool = False
    url: str | None = None  # where to get the key (or the publisher's rules)
    note: str | None = None  # what an optional key buys
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceStatus:
    source: str
    label: str
    state: State
    line: str


SOURCES: tuple[SourceKey, ...] = (
    SourceKey(
        "fred",
        "FRED",
        "macro",
        ("FRED_API_KEY",),
        True,
        "https://fred.stlouisfed.org/docs/api/api_key.html",
        tools=("macro_series",),
    ),
    SourceKey(
        "bls",
        "BLS",
        "macro",
        ("BLS_API_KEY",),
        False,
        "https://data.bls.gov/registrationEngine/",
        note="free, raises BLS's daily limit and years per request",
        tools=("macro_bls_series", "macro_release_schedule"),
    ),
    SourceKey(
        "bea",
        "BEA",
        "macro",
        ("BEA_API_KEY",),
        True,
        "https://apps.bea.gov/API/signup/",
        tools=("macro_bea_table",),
    ),
    SourceKey(
        "sec",
        "SEC EDGAR",
        "filings",
        ("SEC_CONTACT_EMAIL",),
        True,
        "https://www.sec.gov/os/accessing-edgar-data",
        tools=(
            "sec_filings",
            "sec_xbrl_facts",
            "sec_fundamentals",
            "sec_earnings_releases",
            "sec_earnings_figures",
            "sec_insider_trades",
            "sec_13f_holdings",
            "sec_fund_nport",
            "sec_fund_holdings",
        ),
    ),
    SourceKey(
        "fomc", "The Federal Reserve", "fed_treasury", tools=("fed_fomc_meetings", "fed_fomc_statements")
    ),
    SourceKey("nyfed", "The New York Fed", "fed_treasury", tools=("fed_reference_rates",)),
    SourceKey("treasury", "The US Treasury", "fed_treasury", tools=("treasury_yield_curve",)),
    SourceKey(
        "fiscaldata",
        "Treasury FiscalData",
        "fed_treasury",
        tools=("treasury_auctions", "treasury_debt", "treasury_tga"),
    ),
    SourceKey(
        "nasdaq",
        "Nasdaq",
        "calendars",
        note="unofficial endpoint",
        tools=("calendar_economic", "calendar_earnings", "calendar_earnings_history"),
    ),
    SourceKey(
        "nyse",
        "NYSE",
        "calendars",
        ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
        False,
        note="optional cross-check of NYSE's calendar",
        tools=("calendar_us_holidays",),
    ),
    SourceKey("sifma", "SIFMA", "calendars", tools=("calendar_us_holidays",)),
    SourceKey("opm", "OPM", "calendars", tools=("calendar_us_holidays",)),
)

BY_SOURCE: dict[str, SourceKey] = {s.source: s for s in SOURCES}
#: marketlens-data's SEC sources share the EDGAR contact rule.
BY_SOURCE["sec13f"] = BY_SOURCE["sec"]
BY_SOURCE["sec_insider"] = BY_SOURCE["sec"]

#: Every variable whose value must never appear in a message (redacted by value).
KEY_VARIABLES: tuple[str, ...] = (
    "FRED_API_KEY",
    "BLS_API_KEY",
    "BEA_API_KEY",
    "EIA_API_KEY",
    "CENSUS_API_KEY",
    "SEC_CONTACT_EMAIL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
)


def label(source: str) -> str:
    entry = BY_SOURCE.get(source)
    return entry.label if entry else source


def _names(variables: tuple[str, ...]) -> str:
    return " and ".join(variables)


def _is_set(name: str, env: Mapping[str, str]) -> bool:
    value = env.get(name) or ""
    if name == "SEC_CONTACT_EMAIL":
        return is_email(value)
    return bool(value.strip())


def readiness(env: Mapping[str, str], enabled: Collection[str], *, mode: str = "auto") -> list[SourceStatus]:
    """One status per source, in SOURCES order (the doctor prints their lines)."""
    out: list[SourceStatus] = []
    for s in SOURCES:
        prefix = f"data source {s.source}: "
        if s.capability not in enabled:
            state, text = "off", f"capability {s.capability} is off"
        elif not s.variables:
            state, text = "keyless", "keyless"
        elif all(_is_set(v, env) for v in s.variables):
            state, text = "ready", f"ready ({_names(s.variables)} set)"
        elif s.required and mode == "auto":
            missing = _names(tuple(v for v in s.variables if not _is_set(v, env)))
            state = "missing"
            text = f"missing {missing}; its tools will refuse until it is set (free key: {s.url})"
        elif s.required:
            state, text = (
                "optional",
                f"optional {_names(s.variables)} not set (only fetching needs it; mode is local)",
            )
        else:
            detail = f"{s.note}: {s.url}" if s.url else s.note
            state, text = "optional", f"optional {_names(s.variables)} not set ({detail})"
        out.append(SourceStatus(s.source, s.label, state, prefix + text))
    return out
