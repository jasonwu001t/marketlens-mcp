"""Canonical outputs of the core analytics tools (schema 1.x).

Owner: ml-core (seeded verbatim from the contract). Built by ml-analytics.
``series`` is the value of the input result's group column (a ticker, an OCC
symbol, ...) or "_all" when the input has no group column. Every row names the
parameters it was computed with, so a stored result explains itself.
The as-of align output has no fixed model: it is the left result's columns
plus the right result's chosen columns, under the schema name
"marketlens.Aligned" (dynamic; columns typed from the data).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .base import CanonicalModel, Timeframe, UtcDatetime, unit

ALIGNED_SCHEMA_NAME = "marketlens.Aligned"
#: The schema name of results_query / results_sample rows (dynamic columns).
QUERY_ROW_SCHEMA_NAME = "marketlens.QueryRow"


class ReturnPoint(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.ReturnPoint"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("ret",)

    series: str
    t: UtcDatetime = unit(
        "UTC", description="End of the return's interval (the later observation, or the period bucket start)"
    )
    ret: float = unit("fraction")
    kind: Literal["simple", "log"]
    period: Timeframe | None = Field(
        default=None,
        description="Bucket the prices were sampled to (last price per bucket); None = consecutive rows",
    )
    price_column: str


class VolatilityPoint(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.VolatilityPoint"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("volatility",)

    series: str
    t: UtcDatetime = unit("UTC", description="Last observation in the window")
    volatility: float = unit(
        "fraction_per_year", description="stddev_samp of the window's returns x sqrt(periods_per_year)"
    )
    window: int = Field(ge=2, description="Observations (returns) per window")
    periods_per_year: float
    return_kind: Literal["simple", "log"]


class CorrelationCell(CanonicalModel):
    """One cell of a correlation matrix in long form (diagonal included)."""

    schema_name: ClassVar[str] = "marketlens.CorrelationCell"
    value_columns: ClassVar[tuple[str, ...]] = ("correlation",)

    a: str
    b: str
    correlation: float | None = unit(
        "fraction",
        default=None,
        description="Pearson correlation; None with insufficient_data when n_obs < min_overlap",
    )
    n_obs: int = unit("count")
    value_column: str
    start: UtcDatetime | None = unit("UTC", default=None)
    end: UtcDatetime | None = unit("UTC", default=None)


class DrawdownPoint(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.DrawdownPoint"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("drawdown",)

    series: str
    t: UtcDatetime = unit("UTC")
    value: float = unit("price")
    running_peak: float = unit("price")
    drawdown: float = unit(
        "fraction", description="value / running_peak - 1 (0 at a new peak, negative below it)"
    )


class DrawdownSummary(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.DrawdownSummary"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("max_drawdown",)

    series: str
    max_drawdown: float = unit(
        "fraction", description="The most negative drawdown (0 when the series never fell below a peak)"
    )
    peak_t: UtcDatetime = unit("UTC")
    trough_t: UtcDatetime = unit("UTC")
    recovery_t: UtcDatetime | None = unit(
        "UTC",
        default=None,
        description="First time at or above the peak after the trough; None with not_recovered",
    )
    peak_to_trough_days: float = unit("days")
    n_obs: int = unit("count")
    value_column: str


class BetaResult(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.BetaResult"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("beta", "alpha", "r_squared")

    series: str
    benchmark: str
    beta: float | None = unit("fraction", default=None, description="cov_samp(r_a, r_b) / var_samp(r_b)")
    alpha: float | None = unit(
        "fraction", default=None, description="Per period: mean(r_a) - beta x mean(r_b)"
    )
    r_squared: float | None = unit("fraction", default=None)
    n_obs: int = unit("count")
    start: UtcDatetime | None = unit("UTC", default=None)
    end: UtcDatetime | None = unit("UTC", default=None)
    return_kind: Literal["simple", "log"]
    period: Timeframe | None = None


class BetaPoint(CanonicalModel):
    """Rolling beta: one row per window end."""

    schema_name: ClassVar[str] = "marketlens.BetaPoint"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("beta",)

    series: str
    benchmark: str
    t: UtcDatetime = unit("UTC")
    beta: float | None = unit("fraction", default=None)
    window: int = Field(ge=2)
    return_kind: Literal["simple", "log"]
    period: Timeframe | None = None


ANALYTICS_MODELS: tuple[type[CanonicalModel], ...] = (
    ReturnPoint,
    VolatilityPoint,
    CorrelationCell,
    DrawdownPoint,
    DrawdownSummary,
    BetaResult,
    BetaPoint,
)
