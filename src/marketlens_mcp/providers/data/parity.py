"""Every marketlens-data dataset, mapped to the tools that read it or
excluded with a reason. tests/data/test_parity.py checks this list against
``omni.list_datasets()``, so a dataset added to marketlens-data must be
placed here before the suite passes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Mapped:
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Excluded:
    kind: Literal["covered", "second_wave", "not_planned", "internal"]
    reason: str


_COVERED = "read directly from Alpaca by the built-in Alpaca provider"
_LATER = "official source planned for a later release"
_INTERNAL = "a reference list omni uses to resolve symbols, not a research dataset"

PARITY: dict[str, Mapped | Excluded] = {
    # macro
    "fred.series": Mapped(("macro_series", "macro_series_catalog")),
    "bls.timeseries": Mapped(("macro_bls_series",)),
    "bea.nipa": Mapped(("macro_bea_table",)),
    "bls.cpi_schedule": Mapped(("macro_release_schedule",)),
    "bls.empsit_schedule": Mapped(("macro_release_schedule",)),
    # SEC EDGAR
    "sec.submissions": Mapped(("sec_filings",)),
    "sec.company_facts": Mapped(("sec_xbrl_facts", "sec_fundamentals")),
    "sec.fundamentals": Mapped(("sec_fundamentals",)),
    "sec.earnings_releases": Mapped(("sec_earnings_releases",)),
    "sec.earnings_press_release_figures": Mapped(("sec_earnings_figures",)),
    "sec_insider.transactions": Mapped(("sec_insider_trades",)),
    "sec13f.holdings": Mapped(("sec_13f_holdings",)),
    "sec.fund_nport": Mapped(("sec_fund_nport",)),
    "sec.fund_nport_holdings": Mapped(("sec_fund_holdings",)),
    # the Federal Reserve and the Treasury
    "fomc.meetings": Mapped(("fed_fomc_meetings",)),
    "fomc.statement": Mapped(("fed_fomc_statements",)),
    "nyfed.reference_rates": Mapped(("fed_reference_rates",)),
    "treasury.yield_curve": Mapped(("treasury_yield_curve",)),
    "fiscaldata.auctions": Mapped(("treasury_auctions",)),
    "fiscaldata.debt_to_penny": Mapped(("treasury_debt",)),
    "fiscaldata.tga_balance": Mapped(("treasury_tga",)),
    # calendars
    "nasdaq.economic_events": Mapped(("calendar_economic",)),
    "nasdaq.earnings": Mapped(("calendar_earnings",)),
    "nasdaq.earnings_surprise": Mapped(("calendar_earnings_history",)),
    # holidays
    "nyse.holidays": Mapped(("calendar_us_holidays",)),
    "sifma.holidays": Mapped(("calendar_us_holidays",)),
    "opm.federal_holidays": Mapped(("calendar_us_holidays",)),
    # excluded
    "alpaca.stock_bars": Excluded("covered", _COVERED),
    "alpaca.stock_bars_intraday": Excluded("covered", _COVERED),
    "alpaca.stock_snapshot": Excluded("covered", _COVERED),
    "alpaca.corporate_actions": Excluded("covered", _COVERED),
    "alpaca.news": Excluded("covered", _COVERED),
    "alpaca.option_bars": Excluded("covered", _COVERED),
    "alpaca.option_chain_snapshot": Excluded("covered", _COVERED),
    "eia.series": Excluded("second_wave", f"{_LATER} (needs EIA_API_KEY)"),
    "census.eits": Excluded("second_wave", f"{_LATER} (needs CENSUS_API_KEY)"),
    "fedboard.release": Excluded("second_wave", _LATER),
    "cftc.cot": Excluded("second_wave", _LATER),
    "finra.short_interest": Excluded("second_wave", _LATER),
    "ofr.financial_stress": Excluded("second_wave", _LATER),
    "atlantafed.rate_probs": Excluded("second_wave", _LATER),
    "bis.series": Excluded("second_wave", _LATER),
    "ecb.series": Excluded("second_wave", _LATER),
    "eurostat.series": Excluded("second_wave", _LATER),
    "imf.series": Excluded("second_wave", _LATER),
    "oecd.series": Excluded("second_wave", _LATER),
    "worldbank.indicator": Excluded("second_wave", _LATER),
    "cme.fed_funds_futures": Excluded("second_wave", _LATER),
    "cboe.put_call_ratio": Excluded("not_planned", "the free archive ends in 2019"),
    "cnn.fear_greed": Excluded("not_planned", "unofficial third-party endpoint"),
    "gdelt.news": Excluded("not_planned", "third-party news metadata; accrues only by polling"),
    "gdelt.tone": Excluded("not_planned", "third-party news metadata; accrues only by polling"),
    "reddit.posts": Excluded("not_planned", "third-party user text"),
    "kalshi.markets": Excluded("not_planned", "prediction-market odds; accrue only by polling"),
    "polymarket.markets": Excluded("not_planned", "prediction-market odds; accrue only by polling"),
    "sec.fund_tickers": Excluded("internal", _INTERNAL),
    "sec.ftd_cusips": Excluded("internal", _INTERNAL),
}
