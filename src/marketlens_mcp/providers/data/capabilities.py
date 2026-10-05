"""The five capabilities of the data provider. They are declared whether or
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
HOLIDAYS = CapabilitySpec(
    "holidays",
    "US market holidays",
    "Market closures and early closes from their publishers: NYSE (stocks), SIFMA (bonds) and OPM "
    "(federal holidays). Keyless; with Alpaca keys, NYSE's dates are also cross-checked against Alpaca's "
    f"trading calendar. {NEEDS}",
    True,
)
CALENDARS = CapabilitySpec(
    "calendars",
    "Market calendars (Nasdaq; unofficial)",
    "Nasdaq's economic calendar (PMI and other actual and consensus figures), earnings calendar and "
    "earnings history. Off by default: the Nasdaq endpoint is unofficial and may change or refuse "
    f"without notice. {NEEDS}",
    False,
)

CAPABILITIES: tuple[CapabilitySpec, ...] = (MACRO, FILINGS, FED_TREASURY, HOLIDAYS, CALENDARS)
