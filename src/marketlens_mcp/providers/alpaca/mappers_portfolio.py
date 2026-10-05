"""Alpaca trading-API JSON -> canonical brokerage rows. Every row carries the
environment (paper or live) it was read from; money is exact (Decimal from
Alpaca's strings, or from JSON numbers read as Decimal)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time
from typing import Any

from marketlens_schema.base import AbsenceCode, Environment
from marketlens_schema.portfolio import (
    Account,
    AccountConfig,
    Activity,
    BrokerWatchlist,
    Locate,
    LocateQuote,
    Order,
    OrderLeg,
    PortfolioHistoryPoint,
    Position,
)

from .convert import (
    build_row,
    dec,
    integer,
    is_occ,
    num,
    parse_date,
    parse_occ,
    parse_ts,
    text,
    timeframe_from_alpaca,
    to_ticker,
)

NA = AbsenceCode.NOT_APPLICABLE
NO_DATA = AbsenceCode.NO_DATA
Raw = Mapping[str, Any]


def mask_account_number(number: str | None) -> str | None:
    """Only the last four characters are ever served."""
    if not number:
        return None
    return "****" + number[-4:]


def instrument(symbol: str | None, asset_class: str | None) -> tuple[str | None, str | None]:
    """(ticker, occ_symbol): an option's ticker is its underlying (OCC root)."""
    if not symbol:
        return None, None
    if asset_class == "us_option" or (asset_class in (None, "") and is_occ(symbol)):
        return parse_occ(symbol).root, symbol
    return to_ticker(symbol, asset_class), None


def account(raw: Raw, env: Environment) -> Account:
    money = (
        "cash",
        "equity",
        "last_equity",
        "buying_power",
        "regt_buying_power",
        "daytrading_buying_power",
        "non_marginable_buying_power",
        "options_buying_power",
        "long_market_value",
        "short_market_value",
        "initial_margin",
        "maintenance_margin",
        "last_maintenance_margin",
        "sma",
        "accrued_fees",
        "pending_transfer_in",
        "pending_transfer_out",
        "multiplier",
    )
    flags = (
        "pattern_day_trader",
        "trading_blocked",
        "transfers_blocked",
        "account_blocked",
        "trade_suspended_by_user",
        "shorting_enabled",
    )
    data: dict[str, Any] = {
        "environment": env,
        "account_id": raw["id"],
        "account_number_masked": mask_account_number(raw.get("account_number")),
        "status": raw["status"],
        "crypto_status": text(raw.get("crypto_status")),
        "currency": text(raw.get("currency")) or "USD",
        **{f: dec(raw.get(f)) for f in money},
        **{f: raw.get(f) for f in flags},
        "daytrade_count": integer(raw.get("daytrade_count")),
        "options_approved_level": integer(raw.get("options_approved_level")),
        "options_trading_level": integer(raw.get("options_trading_level")),
        "created_at": parse_ts(raw.get("created_at")),
        "balance_as_of": parse_date(raw.get("balance_asof")),
    }
    return build_row(Account, data)


def account_config(raw: Raw, env: Environment) -> AccountConfig:
    return build_row(
        AccountConfig,
        {
            "environment": env,
            "dtbp_check": text(raw.get("dtbp_check")),
            "pdt_check": text(raw.get("pdt_check")),
            "trade_confirm_email": text(raw.get("trade_confirm_email")),
            "suspend_trade": raw.get("suspend_trade"),
            "no_shorting": raw.get("no_shorting"),
            "fractional_trading": raw.get("fractional_trading"),
            "max_margin_multiplier": dec(raw.get("max_margin_multiplier")),
            "max_options_trading_level": integer(raw.get("max_options_trading_level")),
            "ptp_no_exception_entry": raw.get("ptp_no_exception_entry"),
            "disable_overnight_trading": raw.get("disable_overnight_trading"),
        },
    )


def history(raw: Raw, env: Environment) -> list[PortfolioHistoryPoint]:
    """Alpaca's parallel arrays -> one row per timestamp. A null in the arrays
    (no equity for that window) becomes None with no_data."""
    timeframe = timeframe_from_alpaca(raw["timeframe"])
    base = dec(raw.get("base_value"))
    equity, pl, plpc = raw.get("equity") or [], raw.get("profit_loss") or [], raw.get("profit_loss_pct") or []
    rows = []
    for i, ts in enumerate(raw.get("timestamp") or []):
        rows.append(
            build_row(
                PortfolioHistoryPoint,
                {
                    "environment": env,
                    "timeframe": timeframe,
                    "t": parse_ts(ts),
                    "equity": dec(equity[i]) if i < len(equity) else None,
                    "profit_loss": dec(pl[i]) if i < len(pl) else None,
                    "profit_loss_pct": num(plpc[i]) if i < len(plpc) else None,
                    "base_value": base,
                },
                {"equity": NO_DATA, "profit_loss": NO_DATA, "profit_loss_pct": NO_DATA},
            )
        )
    return rows


def position(raw: Raw, env: Environment) -> Position:
    cls = raw["asset_class"]
    ticker, occ = instrument(raw["symbol"], cls)
    return build_row(
        Position,
        {
            "environment": env,
            "asset_class": cls,
            "ticker": ticker,
            "occ_symbol": occ,
            "exchange": text(raw.get("exchange")),
            "side": raw["side"],
            "qty": dec(raw["qty"]),
            "qty_available": dec(raw.get("qty_available")),
            "avg_entry_price": dec(raw["avg_entry_price"]),
            "cost_basis": dec(raw["cost_basis"]),
            "market_value": dec(raw.get("market_value")),
            "current_price": dec(raw.get("current_price")),
            "lastday_price": dec(raw.get("lastday_price")),
            "change_today": num(raw.get("change_today")),
            "unrealized_pl": dec(raw.get("unrealized_pl")),
            "unrealized_plpc": num(raw.get("unrealized_plpc")),
            "unrealized_intraday_pl": dec(raw.get("unrealized_intraday_pl")),
            "unrealized_intraday_plpc": num(raw.get("unrealized_intraday_plpc")),
            "asset_marginable": raw.get("asset_marginable"),
            "provider_asset_id": text(raw.get("asset_id")),
        },
        {"occ_symbol": NA} if occ is None else None,
    )


def _order_common(raw: Raw) -> dict[str, Any]:
    return {
        "order_id": raw["id"],
        "client_order_id": text(raw.get("client_order_id")),
        "side": text(raw.get("side")),
        "position_intent": text(raw.get("position_intent")),
        "order_type": text(raw.get("type")) or text(raw.get("order_type")),
        "status": raw["status"],
        "qty": dec(raw.get("qty")),
        "filled_qty": dec(raw.get("filled_qty")),
        "filled_avg_price": dec(raw.get("filled_avg_price")),
        "limit_price": dec(raw.get("limit_price")),
        "stop_price": dec(raw.get("stop_price")),
        "created_at": parse_ts(raw.get("created_at")),
        "filled_at": parse_ts(raw.get("filled_at")),
    }


def order_leg(raw: Raw) -> OrderLeg:
    cls = text(raw.get("asset_class")) or ("us_option" if is_occ(raw.get("symbol")) else "us_equity")
    ticker, occ = instrument(raw["symbol"], cls)
    data = {
        **_order_common(raw),
        "asset_class": cls,
        "ticker": ticker,
        "occ_symbol": occ,
        "ratio_qty": dec(raw.get("ratio_qty")),
    }
    return build_row(OrderLeg, data, {"occ_symbol": NA} if occ is None else None)


def order(raw: Raw, env: Environment) -> Order:
    """A multi-leg parent has no symbol, side or asset class of its own: the
    asset class and ticker come from its first leg, side is not applicable."""
    legs = [order_leg(leg) for leg in raw.get("legs") or []]
    cls = text(raw.get("asset_class"))
    symbol = text(raw.get("symbol"))
    codes: dict[str, AbsenceCode] = {}
    if symbol:
        cls = cls or ("us_option" if is_occ(symbol) else "us_equity")
        ticker, occ = instrument(symbol, cls)
    elif legs:
        cls, ticker, occ = cls or legs[0].asset_class, legs[0].ticker, None
        codes["side"] = NA
    else:
        raise ValueError("order without a symbol or legs")
    if occ is None:
        codes["occ_symbol"] = NA
    if not legs:
        codes["legs"] = NA
    trail = num(raw.get("trail_percent"))
    data = {
        **_order_common(raw),
        "environment": env,
        "asset_class": cls,
        "ticker": ticker,
        "occ_symbol": occ,
        "order_class": text(raw.get("order_class")) or "simple",
        "time_in_force": text(raw.get("time_in_force")),
        "notional": dec(raw.get("notional")),
        "trail_price": dec(raw.get("trail_price")),
        "trail_percent": None if trail is None else trail / 100,
        "hwm": dec(raw.get("hwm")),
        "extended_hours": raw.get("extended_hours"),
        "updated_at": parse_ts(raw.get("updated_at")),
        "submitted_at": parse_ts(raw.get("submitted_at")),
        "expired_at": parse_ts(raw.get("expired_at")),
        "expires_at": parse_ts(raw.get("expires_at")),
        "canceled_at": parse_ts(raw.get("canceled_at")),
        "failed_at": parse_ts(raw.get("failed_at")),
        "replaced_at": parse_ts(raw.get("replaced_at")),
        "replaced_by": text(raw.get("replaced_by")),
        "replaces": text(raw.get("replaces")),
        "legs": legs or None,
    }
    return build_row(Order, data, codes)


_TRADE_ONLY = ("side", "price", "cum_qty", "leaves_qty", "order_id", "order_status")
_NON_TRADE_ONLY = ("net_amount", "per_share_amount", "currency", "status")


def activity(raw: Raw, env: Environment) -> Activity:
    """A fill (activity_type FILL, category trade) or a non-trade activity.
    A non-trade activity dated without a time is placed at 00:00 UTC of its date."""
    kind = raw["activity_type"]
    symbol = text(raw.get("symbol"))
    ticker, occ = instrument(symbol, None) if symbol else (None, None)
    if kind == "FILL":
        t = parse_ts(raw["transaction_time"])
        data = {
            "category": "trade",
            "t": t,
            "date": t.date() if t else None,
            "side": text(raw.get("side")),
            "price": dec(raw.get("price")),
            "cum_qty": dec(raw.get("cum_qty")),
            "leaves_qty": dec(raw.get("leaves_qty")),
            "order_id": text(raw.get("order_id")),
            "order_status": text(raw.get("order_status")),
            "description": text(raw.get("type")),
        }
        codes = dict.fromkeys(_NON_TRADE_ONLY, NA)
    else:
        raw_date = str(raw.get("date") or "")
        if len(raw_date) > 10:
            t = parse_ts(raw_date)
            day = t.date() if t else None
        else:
            day = parse_date(raw_date)
            t = datetime.combine(day, time(0), UTC) if day else parse_ts(raw.get("created_at"))
        data = {
            "category": "non_trade",
            "t": t,
            "date": day,
            "net_amount": dec(raw.get("net_amount")),
            "per_share_amount": dec(raw.get("per_share_amount")),
            "currency": text(raw.get("currency")),
            "status": text(raw.get("status")),
            "description": text(raw.get("description")) or text(raw.get("activity_sub_type")),
        }
        codes = dict.fromkeys(_TRADE_ONLY, NA)
    if occ is None:
        codes["occ_symbol"] = NA
    data.update(
        {
            "environment": env,
            "activity_id": raw["id"],
            "activity_type": kind,
            "ticker": ticker,
            "occ_symbol": occ,
            "qty": dec(raw.get("qty")),
        }
    )
    return build_row(Activity, data, codes)


def watchlist(raw: Raw, env: Environment, *, with_tickers: bool) -> BrokerWatchlist:
    tickers = None
    if with_tickers:
        tickers = [to_ticker(a["symbol"], a.get("class")) for a in raw.get("assets") or []]
    return build_row(
        BrokerWatchlist,
        {
            "environment": env,
            "watchlist_id": raw["id"],
            "name": raw["name"],
            "created_at": parse_ts(raw.get("created_at")),
            "updated_at": parse_ts(raw.get("updated_at")),
            "tickers": tickers,
        },
        skip=() if with_tickers else ("tickers",),
    )


def locate(raw: Raw, env: Environment) -> Locate:
    return build_row(
        Locate,
        {
            "environment": env,
            "locate_id": raw["id"],
            "ticker": to_ticker(raw["symbol"], "us_equity"),
            "status": raw["status"],
            "requested_qty": integer(raw["requested_qty"]),
            "located_qty": integer(raw.get("located_qty")),
            "located_price": dec(raw.get("located_price")),
            "limit_price": dec(raw.get("limit_price")),
            "total_fee": dec(raw.get("total_fee")),
            "all_or_none": raw.get("all_or_none"),
            "rejection_reason": text(raw.get("rejection_reason")),
            "created_at": parse_ts(raw.get("created_at")),
            "expires_at": parse_ts(raw.get("expires_at")),
        },
    )


def locate_quote(raw: Raw, env: Environment) -> LocateQuote:
    available = integer(raw.get("available_qty"))
    return build_row(
        LocateQuote,
        {
            "environment": env,
            "ticker": to_ticker(raw["symbol"], "us_equity"),
            "available_qty": available,
            "price": dec(raw.get("price")),
            "quoted_at": parse_ts(raw.get("quoted_at")),
        },
        {"price": NO_DATA} if available == 0 else None,
    )
