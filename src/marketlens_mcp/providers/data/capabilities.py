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
    "SEC filings, XBRL facts, XBRL frames (one concept across filers) and point-in-time fundamentals, "
    "earnings releases with their press-release EPS, guidance and non-GAAP reconciliation, segment facts, a "
    "foreign issuer's 6-K quarters, pay versus performance, insider trades, Form 144 notices, the IPO "
    "pipeline, EDGAR's daily form index, 13F holdings, fund N-PORT reports, the company documents the "
    "operator lists with their targets, and SEC EDGAR full-text search; and the monthly revenue "
    "Taiwan-listed companies file on MOPS (TWSE / TPEx). Filing text is marked untrusted. The sec_, "
    "company_ and twse_ tools need the data extra (marketlens-mcp[data]); filings_search is built in.",
    True,
)
FED_TREASURY = CapabilitySpec(
    "fed_treasury",
    "Federal Reserve and Treasury",
    "FOMC meetings, statements, economic projections with the dot plot, minutes and the Beige Book, NY Fed "
    "reference rates (SOFR, EFFR, ...), the Treasury par yield curve, Treasury auctions, debt to the penny "
    "and the Treasury's cash balance; the FDIC's bank financials and industry aggregates and the SEC's money "
    f"market fund statistics. {NEEDS}",
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
