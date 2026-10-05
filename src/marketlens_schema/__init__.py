"""marketlens_schema: the vendor-neutral canonical models (pydantic only).

Owner: ml-core. Seeded verbatim from the contract; later edits are additive
and follow the versioning rule in base.py.
"""

from __future__ import annotations

from .analytics import ALIGNED_SCHEMA_NAME, ANALYTICS_MODELS, QUERY_ROW_SCHEMA_NAME
from .base import (
    SCHEMA_NAMESPACE,
    SCHEMA_VERSION,
    UNITS,
    AbsenceCode,
    AbsenceReason,
    AssetClass,
    CanonicalModel,
    DecimalStr,
    Delay,
    Environment,
    Isin,
    OccSymbol,
    OptionStyle,
    OptionType,
    OrderClass,
    OrderStatus,
    OrderType,
    PaginationState,
    PositionSide,
    Provenance,
    Side,
    Ticker,
    Timeframe,
    TimeInForce,
    UtcDatetime,
    normalize_ticker,
    unexplained_absences,
    unit,
)
from .market import MARKET_MODELS
from .portfolio import PORTFOLIO_MODELS

#: schema name -> model class, for every built-in model. Plugins add theirs
#: through the server's Registry, never by editing this mapping.
BUILTIN_MODELS: dict[str, type[CanonicalModel]] = {
    m.schema_name: m for m in (*MARKET_MODELS, *PORTFOLIO_MODELS, *ANALYTICS_MODELS)
}

__all__ = [
    "ALIGNED_SCHEMA_NAME",
    "BUILTIN_MODELS",
    "QUERY_ROW_SCHEMA_NAME",
    "SCHEMA_NAMESPACE",
    "SCHEMA_VERSION",
    "UNITS",
    "AbsenceCode",
    "AbsenceReason",
    "AssetClass",
    "CanonicalModel",
    "DecimalStr",
    "Delay",
    "Environment",
    "Isin",
    "OccSymbol",
    "OptionStyle",
    "OptionType",
    "OrderClass",
    "OrderStatus",
    "OrderType",
    "PaginationState",
    "PositionSide",
    "Provenance",
    "Side",
    "Ticker",
    "TimeInForce",
    "Timeframe",
    "UtcDatetime",
    "normalize_ticker",
    "unexplained_absences",
    "unit",
]
