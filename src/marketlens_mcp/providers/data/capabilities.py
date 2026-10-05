"""The four capabilities of the data provider. They are declared whether or
not marketlens-data is installed, so a config file that names them is never
refused; their tools are listed only when the extra is installed."""

from __future__ import annotations

from marketlens_mcp.plugin_api import CapabilitySpec

NEEDS = "Needs the data extra (marketlens-mcp[data])."

MACRO = CapabilitySpec(
    "macro",
    "Economic data",
    "FRED/ALFRED, BLS and BEA series with vintages and as-of reads (CPI, the unemployment rate, "
    f"payrolls, GDP, ...), BLS release schedules and the series catalogue. {NEEDS}",
    True,
)
FILINGS = CapabilitySpec(
    "filings",
    "SEC EDGAR",
    "SEC filings, XBRL facts and point-in-time fundamentals, earnings releases and press-release EPS, "
    f"insider trades, 13F holdings and fund N-PORT reports. Filing text is marked untrusted. {NEEDS}",
    True,
)
FED_TREASURY = CapabilitySpec(
    "fed_treasury",
    "Federal Reserve and Treasury",
    "FOMC meetings and statements, NY Fed reference rates (SOFR, EFFR, ...), the Treasury par yield "
    f"curve, Treasury auctions, debt to the penny and the Treasury's cash balance. {NEEDS}",
    True,
)
CALENDARS = CapabilitySpec(
    "calendars",
    "Market calendars (Nasdaq; unofficial)",
    "Nasdaq's economic calendar (PMI and other actual and consensus figures), earnings calendar and "
    "earnings history, and US market holidays (NYSE, SIFMA, OPM). Off by default: the Nasdaq endpoint "
    f"is unofficial and may change or refuse without notice. {NEEDS}",
    False,
)

CAPABILITIES: tuple[CapabilitySpec, ...] = (MACRO, FILINGS, FED_TREASURY, CALENDARS)
