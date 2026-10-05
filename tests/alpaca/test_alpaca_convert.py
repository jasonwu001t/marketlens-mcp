"""Raw Alpaca values -> canonical values (marketlens_mcp.providers.alpaca.convert)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from marketlens_mcp.providers.alpaca import convert as c
from marketlens_schema.base import AbsenceCode
from marketlens_schema.market import Bar, Quote


def test_timestamps_with_nanoseconds_are_truncated_to_microseconds_in_utc():
    assert c.parse_ts("2021-02-06T13:35:08.946977536Z") == datetime(2021, 2, 6, 13, 35, 8, 946977, tzinfo=UTC)
    assert c.parse_ts("2025-06-24T14:15:22-04:00") == datetime(2025, 6, 24, 18, 15, 22, tzinfo=UTC)
    assert c.parse_ts("2022-01-03T09:00:00Z") == datetime(2022, 1, 3, 9, tzinfo=UTC)
    assert c.parse_ts("2022-05-18T12:00:05.2Z") == datetime(2022, 5, 18, 12, 0, 5, 200000, tzinfo=UTC)
    assert c.parse_ts(1697722200) == datetime(2023, 10, 19, 13, 30, tzinfo=UTC)
    assert c.parse_ts(None) is None
    assert c.parse_ts("") is None


def test_naive_or_garbage_timestamps_are_refused():
    with pytest.raises(ValueError):
        c.parse_ts("2022-01-03T09:00:00")
    with pytest.raises(ValueError):
        c.parse_ts("yesterday")


def test_dates():
    assert c.parse_date("2023-05-04") == date(2023, 5, 4)
    assert c.parse_date("") is None
    assert c.parse_date(None) is None


def test_equity_symbols_translate_both_ways():
    assert c.to_ticker("BRK.B", "us_equity") == "BRK-B"
    assert c.to_ticker("AAPL", "us_equity") == "AAPL"
    assert c.alpaca_symbol("BRK-B") == "BRK.B"
    assert c.alpaca_symbol("BTC/USD") == "BTC/USD"
    assert c.path_symbol("BTC/USD") == "BTCUSD"
    assert c.path_symbol("BRK-B") == "BRK.B"


def test_crypto_symbols_become_pairs():
    assert c.to_ticker("BTC/USD", "crypto") == "BTC/USD"
    assert c.to_ticker("BTCUSD", "crypto") == "BTC/USD"
    assert c.to_ticker("ETHUSDT", "crypto") == "ETH/USDT"
    assert c.to_ticker("SOLUSDC", "crypto") == "SOL/USDC"


def test_ticker_input_is_normalised_and_refused_by_name():
    assert c.equity_tickers(["aapl", " brk.b ", "BRK/B"]) == ["AAPL", "BRK-B", "BRK-B"]
    with pytest.raises(ValueError, match="'BTC/USD' is a crypto pair"):
        c.equity_tickers(["BTC/USD"])
    with pytest.raises(ValueError, match="'AA PL!'"):
        c.equity_tickers(["AA PL!"])
    assert c.crypto_pairs(["btc/usd", "ETH/USDT"]) == ["BTC/USD", "ETH/USDT"]
    with pytest.raises(ValueError, match="'AAPL' is not a crypto pair"):
        c.crypto_pairs(["AAPL"])


def test_occ_symbols_are_parsed():
    p = c.parse_occ("AAPL250117C00150000")
    assert (p.root, p.expiration, p.option_type, p.strike) == ("AAPL", date(2025, 1, 17), "call", 150.0)
    p = c.parse_occ("SPY241213P00512500")
    assert (p.option_type, p.strike) == ("put", 512.5)
    assert c.occ_symbols(["aapl250117c00150000"]) == ["AAPL250117C00150000"]
    with pytest.raises(ValueError, match="'AAPL'"):
        c.occ_symbols(["AAPL"])


def test_decimals_never_come_from_binary_floats():
    assert c.dec("103820.56") == Decimal("103820.56")
    assert c.dec(Decimal("1.10")) == Decimal("1.10")
    assert c.dec(0.1) == Decimal("0.1")  # repr, not the binary expansion
    assert c.dec(4) == Decimal("4")
    assert c.dec("") is None
    assert c.dec(None) is None


def test_percent_to_fraction():
    assert c.percent(145.56) == pytest.approx(1.4556)
    assert c.percent("30") == pytest.approx(0.30)
    assert c.percent(None) is None


def test_iso_durations():
    assert c.parse_duration("P1Y") == timedelta(days=365)
    assert c.parse_duration("P5D") == timedelta(days=5)
    assert c.parse_duration("PT20M") == timedelta(minutes=20)
    assert c.parse_duration("P1W") == timedelta(days=7)
    assert c.parse_duration("P1DT12H") == timedelta(days=1, hours=12)
    assert c.parse_duration("P2M") == timedelta(days=60)
    for bad in ("", "P", "5D", "PT", "P-1D", "P1.5D"):
        with pytest.raises(ValueError):
            c.parse_duration(bad)


@pytest.mark.parametrize("huge", ["P99999999999999D", "PT99999999999999999999H", "P9999999999Y"])
def test_a_duration_too_long_for_a_timedelta_is_a_value_error(huge):
    with pytest.raises(ValueError, match="too long"):
        c.parse_duration(huge)


def test_instants_from_user_input():
    assert c.parse_instant("2026-01-02") == datetime(2026, 1, 2, tzinfo=UTC)
    assert c.parse_instant("2026-01-02T14:30:00Z") == datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    assert c.parse_instant("2026-01-02T09:30:00-05:00") == datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="time zone"):
        c.parse_instant("2026-01-02T14:30:00")


def test_timeframes_translate_both_ways():
    pairs = {
        "1min": "1Min",
        "15min": "15Min",
        "1h": "1Hour",
        "4h": "4Hour",
        "1d": "1Day",
        "1w": "1Week",
        "3mo": "3Month",
    }
    for canonical, alpaca in pairs.items():
        assert c.timeframe_to_alpaca(canonical) == alpaca
        assert c.timeframe_from_alpaca(alpaca) == canonical
    assert c.timeframe_from_alpaca("1H") == "1h"
    assert c.timeframe_from_alpaca("1D") == "1d"
    assert c.timeframe_from_alpaca("5Min") == "5min"
    with pytest.raises(ValueError):
        c.timeframe_to_alpaca("7mo")


def test_exchange_local_times_become_utc():
    # EDT (UTC-4) in June, EST (UTC-5) in December; HH:MM and HHMM both appear.
    assert c.et_to_utc(date(2025, 6, 24), "09:30") == datetime(2025, 6, 24, 13, 30, tzinfo=UTC)
    assert c.et_to_utc(date(2025, 12, 24), "1300") == datetime(2025, 12, 24, 18, 0, tzinfo=UTC)
    assert c.et_to_utc(date(2025, 12, 24), "") is None


def test_build_row_explains_every_none():
    row = c.build_row(
        Quote,
        {
            "ticker": "AAPL",
            "asset_class": "us_equity",
            "t": c.parse_ts("2026-01-02T15:00:00Z"),
            "bid_price": None,
            "ask_price": 10.0,
            "ask_size": 100.0,
            "conditions": ["R"],
            "tape": "C",
            "bid_size": None,
            "bid_exchange": None,
            "ask_exchange": "N",
        },
        codes={"bid_price": AbsenceCode.NO_DATA, "bid_size": AbsenceCode.NO_DATA},
    )
    assert row.absent == {
        "bid_price": "no_data",
        "bid_size": "no_data",
        "bid_exchange": "not_provided_by_source",
    }


def test_build_row_without_none_has_no_absent_map():
    row = c.build_row(
        Bar,
        {
            "ticker": "AAPL",
            "asset_class": "us_equity",
            "timeframe": "1d",
            "t": c.parse_ts("2026-01-02T05:00:00Z"),
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
            "trade_count": 3,
            "vwap": 1.2,
        },
    )
    assert row.absent is None
