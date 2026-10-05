"""Alpaca reference JSON -> canonical rows: assets, option contracts, the market
calendar and clock, corporate-action announcements and processed corporate
actions, option exchanges."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from marketlens_schema.base import AbsenceCode
from marketlens_schema.market import (
    Asset,
    CorporateAction,
    CorporateActionAnnouncement,
    MarketCalendarDay,
    MarketClock,
    OptionContract,
    OptionDeliverable,
    OptionExchange,
)

from .convert import build_row, dec, et_to_utc, integer, num, parse_date, parse_ts, percent, text, to_ticker

NA = AbsenceCode.NOT_APPLICABLE
US_EQUITIES = "US equities"
Raw = Mapping[str, Any]

_CRYPTO_ONLY = ("min_order_size", "min_trade_increment", "price_increment")
_EQUITY_ONLY = (
    "margin_requirement_long",
    "margin_requirement_short",
    "cusip",
    "easy_to_borrow",
    "shortable",
    "marginable",
)


def asset(raw: Raw) -> Asset:
    cls = raw["class"]
    etb = raw.get("easy_to_borrow")
    if etb is None and raw.get("borrow_status"):
        etb = raw["borrow_status"] == "easy_to_borrow"
    not_applicable = _EQUITY_ONLY if cls == "crypto" else _CRYPTO_ONLY
    data = {
        "ticker": to_ticker(raw["symbol"], cls),
        "asset_class": cls,
        "name": text(raw.get("name")),
        "exchange": text(raw.get("exchange")),
        "status": raw["status"],
        "tradable": raw["tradable"],
        "marginable": raw.get("marginable"),
        "shortable": raw.get("shortable"),
        "easy_to_borrow": etb,
        "fractionable": raw.get("fractionable"),
        "min_order_size": dec(raw.get("min_order_size")),
        "min_trade_increment": dec(raw.get("min_trade_increment")),
        "price_increment": dec(raw.get("price_increment")),
        "margin_requirement_long": percent(raw.get("margin_requirement_long")),
        "margin_requirement_short": percent(raw.get("margin_requirement_short")),
        "cusip": text(raw.get("cusip")),
        "attributes": list(raw.get("attributes") or []),
        "provider_asset_id": text(raw.get("id")),
    }
    return build_row(Asset, data, dict.fromkeys(not_applicable, NA))


def deliverable(raw: Raw) -> OptionDeliverable:
    kind = raw["type"]
    return build_row(
        OptionDeliverable,
        {
            "type": kind,
            "ticker": to_ticker(raw["symbol"], "us_equity")
            if kind == "equity" and raw.get("symbol")
            else None,
            "amount": dec(raw["amount"]),
            "allocation_pct": percent(raw.get("allocation_percentage")),
            "settlement_type": text(raw.get("settlement_type")),
            "settlement_method": text(raw.get("settlement_method")),
            "delayed_settlement": raw.get("delayed_settlement"),
        },
        {"ticker": NA} if kind == "cash" else None,
    )


def option_contract(raw: Raw, *, with_deliverables: bool) -> OptionContract:
    return build_row(
        OptionContract,
        {
            "occ_symbol": raw["symbol"],
            "underlying": to_ticker(raw["underlying_symbol"], "us_equity"),
            "root_symbol": raw["root_symbol"],
            "name": text(raw.get("name")),
            "status": raw["status"],
            "tradable": raw["tradable"],
            "expiration_date": parse_date(raw["expiration_date"]),
            "strike": num(raw["strike_price"]),
            "option_type": raw["type"],
            "style": raw["style"],
            "multiplier": num(raw["multiplier"]),
            "size": num(raw.get("size")),
            "open_interest": integer(raw.get("open_interest")),
            "open_interest_date": parse_date(raw.get("open_interest_date")),
            "close_price": num(raw.get("close_price")),
            "close_price_date": parse_date(raw.get("close_price_date")),
            "deliverables": [deliverable(d) for d in raw.get("deliverables") or []]
            if with_deliverables
            else None,
            "provider_contract_id": text(raw.get("id")),
        },
        skip=() if with_deliverables else ("deliverables",),
    )


def calendar_day(raw: Raw) -> MarketCalendarDay:
    day = date.fromisoformat(raw["date"])
    return build_row(
        MarketCalendarDay,
        {
            "market": US_EQUITIES,
            "date": day,
            "open": et_to_utc(day, raw["open"]),
            "close": et_to_utc(day, raw["close"]),
            "session_open": et_to_utc(day, raw.get("session_open")),
            "session_close": et_to_utc(day, raw.get("session_close")),
            "settlement_date": parse_date(raw.get("settlement_date")),
        },
    )


def clock(raw: Raw) -> MarketClock:
    return build_row(
        MarketClock,
        {
            "market": US_EQUITIES,
            "t": parse_ts(raw["timestamp"]),
            "is_open": raw["is_open"],
            "next_open": parse_ts(raw["next_open"]),
            "next_close": parse_ts(raw["next_close"]),
        },
    )


_CA_TYPE = {"dividend": "dividend", "merger": "merger", "spinoff": "spinoff", "split": "split"}


def announcement(raw: Raw) -> CorporateActionAnnouncement:
    def tk(key: str) -> str | None:
        return to_ticker(raw[key], "us_equity") if text(raw.get(key)) else None

    return build_row(
        CorporateActionAnnouncement,
        {
            "announcement_id": raw["id"],
            "corporate_action_id": text(raw.get("corporate_action_id")),
            "ca_type": _CA_TYPE.get(str(raw.get("ca_type", "")).lower(), "other"),
            "ca_sub_type": text(raw.get("ca_sub_type")),
            "initiating_ticker": tk("initiating_symbol"),
            "initiating_cusip": text(raw.get("initiating_original_cusip")),
            "target_ticker": tk("target_symbol"),
            "target_cusip": text(raw.get("target_original_cusip")),
            "declaration_date": parse_date(raw.get("declaration_date")),
            "ex_date": parse_date(raw.get("ex_date")),
            "record_date": parse_date(raw.get("record_date")),
            "payable_date": parse_date(raw.get("payable_date")),
            "effective_date": parse_date(raw.get("effective_date")),
            "cash": dec(raw.get("cash")),
            "old_rate": dec(raw.get("old_rate")),
            "new_rate": dec(raw.get("new_rate")),
        },
    )


# --- processed corporate actions (market data API) --------------------------------------------

#: Alpaca response array -> canonical action_type.
CA_ARRAYS = {
    "cash_dividends": "cash_dividend",
    "stock_dividends": "stock_dividend",
    "forward_splits": "forward_split",
    "reverse_splits": "reverse_split",
    "unit_splits": "unit_split",
    "spin_offs": "spin_off",
    "cash_mergers": "cash_merger",
    "stock_mergers": "stock_merger",
    "stock_and_cash_mergers": "stock_and_cash_merger",
    "redemptions": "redemption",
    "name_changes": "name_change",
    "worthless_removals": "worthless_removal",
    "rights_distributions": "rights_distribution",
    "partial_calls": "partial_call",
    "reorganizations": "reorganization",
    "capital_gains_distributions": "capital_gains_distribution",
}

_D = ("process_date", "ex_date", "record_date", "payable_date")
_SYM = {"ticker": "symbol", "cusip": "cusip", "isin": "isin"}
_ACQ = {
    "ticker": "acquiree_symbol",
    "acquiree_ticker": "acquiree_symbol",
    "acquirer_ticker": "acquirer_symbol",
    "cusip": "acquiree_cusip",
    "isin": "acquiree_isin",
}
_SRC = {
    "ticker": "source_symbol",
    "source_ticker": "source_symbol",
    "new_ticker": "new_symbol",
    "cusip": "source_cusip",
    "isin": "source_isin",
}
_OLD = {
    "ticker": "old_symbol",
    "old_ticker": "old_symbol",
    "new_ticker": "new_symbol",
    "cusip": "old_cusip",
    "isin": "old_isin",
}

#: action_type -> (canonical field -> Alpaca field, dates that apply). Fields not
#: listed are not applicable to the type (None with not_applicable).
CA_FIELDS: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {
    "cash_dividend": ({**_SYM, "rate": "rate", "special": "special", "foreign": "foreign"}, _D),
    "stock_dividend": ({**_SYM, "new_rate": "rate"}, _D),
    "forward_split": ({**_SYM, "old_rate": "old_rate", "new_rate": "new_rate"}, _D),
    "reverse_split": (
        {
            "ticker": "symbol",
            "cusip": "old_cusip",
            "isin": "old_isin",
            "new_ticker": "new_symbol",
            "old_rate": "old_rate",
            "new_rate": "new_rate",
        },
        _D,
    ),
    "unit_split": (
        {**_OLD, "old_rate": "old_rate", "new_rate": "new_rate"},
        ("process_date", "payable_date", "effective_date"),
    ),
    "spin_off": ({**_SRC, "old_rate": "source_rate", "new_rate": "new_rate"}, _D),
    "cash_merger": ({**_ACQ, "rate": "rate"}, ("process_date", "payable_date", "effective_date")),
    "stock_merger": (
        {**_ACQ, "old_rate": "acquiree_rate", "new_rate": "acquirer_rate"},
        ("process_date", "payable_date", "effective_date"),
    ),
    "stock_and_cash_merger": (
        {**_ACQ, "old_rate": "acquiree_rate", "new_rate": "acquirer_rate", "cash_rate": "cash_rate"},
        ("process_date", "payable_date", "effective_date"),
    ),
    "redemption": ({**_SYM, "rate": "rate"}, ("process_date", "payable_date")),
    "name_change": (dict(_OLD), ("process_date",)),
    "worthless_removal": (dict(_SYM), ("process_date",)),
    "rights_distribution": ({**_SRC, "new_rate": "rate"}, _D),
    "partial_call": ({**_SYM, "rate": "price"}, ("process_date", "record_date", "payable_date")),
    "reorganization": (
        {**_SYM, "cash_rate": "cash_rate"},
        ("process_date", "payable_date", "effective_date"),
    ),
    "capital_gains_distribution": ({**_SYM, "rate": "long_term_rate+short_term_rate"}, _D),
}
_TICKER_FIELDS = {"ticker", "new_ticker", "old_ticker", "acquirer_ticker", "acquiree_ticker", "source_ticker"}
_NUMBER_FIELDS = {"rate", "cash_rate", "old_rate", "new_rate"}


def _value(raw: Raw, canonical: str, source: str) -> Any:
    if "+" in source:  # a total of several Alpaca fields (capital gains: long + short term)
        parts = [num(raw.get(s)) for s in source.split("+")]
        present = [p for p in parts if p is not None]
        return sum(present) if present else None
    v = raw.get(source)
    if canonical in _TICKER_FIELDS:
        return to_ticker(v, "us_equity") if text(v) else None
    if canonical in _NUMBER_FIELDS:
        return num(v)
    if canonical in ("special", "foreign"):
        return v
    return text(v)


def corporate_action(raw: Raw, action_type: str) -> CorporateAction:
    fields, dates = CA_FIELDS[action_type]
    data: dict[str, Any] = {
        "action_id": raw["id"],
        "action_type": action_type,
        "currency": text(raw.get("currency")),
    }
    for canonical, source in fields.items():
        data[canonical] = _value(raw, canonical, source)
    for d in dates:
        data[d] = parse_date(raw.get(d))
    applicable = {"action_id", "action_type", "currency", *fields, *dates}
    codes = {f: NA for f in CorporateAction.model_fields if f not in applicable}
    return build_row(CorporateAction, data, codes)


def option_exchange(code: str, name: str) -> OptionExchange:
    return build_row(OptionExchange, {"code": code, "name": name})
