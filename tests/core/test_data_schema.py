"""Schema 1.1.0: the canonical models of the official-data tools (the
``data`` extra) and the four units they add. The models are built in, so
they are registered and exported whether or not the extra is installed."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from marketlens_schema import BUILTIN_MODELS, SCHEMA_VERSION, UNITS, unexplained_absences
from marketlens_schema import data as d

NAMES = [
    "EconomicObservation",
    "EconomicSeriesInfo",
    "ReleaseScheduleEntry",
    "Filing",
    "XbrlFact",
    "FundamentalValue",
    "EarningsRelease",
    "EarningsFigure",
    "InsiderTransaction",
    "InstitutionalHolding",
    "FundReport",
    "FundHolding",
    "FomcMeeting",
    "FomcStatement",
    "ReferenceRate",
    "YieldCurvePoint",
    "TreasuryAuction",
    "TreasuryDebt",
    "TreasuryCashBalance",
    "EconomicEvent",
    "EarningsCalendarEntry",
    "EarningsSurprise",
    "MarketHoliday",
]
KT = datetime(2024, 2, 13, tzinfo=UTC)


def test_schema_version_is_1_1_0():
    assert SCHEMA_VERSION == "1.1.0"


def test_units_added():
    for name in ("as_published", "USD_per_share", "times", "ordinal"):
        assert name in UNITS, name
    # the 1.0 vocabulary is untouched
    for name in ("price", "USD", "fraction", "fraction_per_year", "percent_of_par", "count", "UTC"):
        assert name in UNITS


def test_the_23_models_are_built_in():
    assert [m.__name__ for m in d.DATA_MODELS] == NAMES
    for model in d.DATA_MODELS:
        assert model.schema_name == f"marketlens.{model.__name__}"
        assert BUILTIN_MODELS[model.schema_name] is model
        assert model.model_config["extra"] == "forbid" and model.model_config["frozen"] is True
        # every observation says when it became knowable; a catalogue entry is no observation
        assert ("knowledge_time" in model.model_fields) is (model is not d.EconomicSeriesInfo)


def _unit(model, field):
    return model.model_json_schema()["properties"][field].get("x-unit") or next(
        (
            s.get("x-unit")
            for s in model.model_json_schema()["properties"][field].get("anyOf", [])
            if s.get("x-unit")
        ),
        None,
    )


@pytest.mark.parametrize(
    ("model", "field", "expected"),
    [
        (d.EconomicObservation, "value", "as_published"),
        (d.EconomicObservation, "line_number", "ordinal"),
        (d.EconomicObservation, "knowledge_time", "UTC"),
        (d.XbrlFact, "value", "as_published"),
        (d.FundamentalValue, "value", "as_published"),
        (d.EarningsFigure, "value", "USD_per_share"),
        (d.InsiderTransaction, "shares", "shares"),
        (d.InsiderTransaction, "price_per_share", "price"),
        (d.InsiderTransaction, "value_usd", "USD"),
        (d.InsiderTransaction, "line", "ordinal"),
        (d.InstitutionalHolding, "value_usd", "USD"),
        (d.InstitutionalHolding, "shares", "as_published"),
        (d.InstitutionalHolding, "sole_voting", "shares"),
        (d.FundReport, "net_assets", "USD"),
        (d.FundReport, "holdings_count", "count"),
        (d.FundHolding, "pct_value", "fraction"),
        (d.FundHolding, "balance", "as_published"),
        (d.FomcStatement, "n_paragraphs", "count"),
        (d.ReferenceRate, "rate", "fraction_per_year"),
        (d.ReferenceRate, "volume_usd", "USD"),
        (d.YieldCurvePoint, "rate", "fraction_per_year"),
        (d.TreasuryAuction, "bid_to_cover", "times"),
        (d.TreasuryAuction, "price_per_100", "percent_of_par"),
        (d.TreasuryAuction, "high_yield", "fraction_per_year"),
        (d.TreasuryAuction, "offering_amount", "USD"),
        (d.TreasuryDebt, "total_debt", "USD"),
        (d.TreasuryCashBalance, "closing_balance", "USD"),
        (d.EconomicEvent, "actual", "as_published"),
        (d.EarningsCalendarEntry, "market_cap", "USD"),
        (d.EarningsCalendarEntry, "eps_forecast", "USD_per_share"),
        (d.EarningsCalendarEntry, "surprise_pct", "fraction"),
        (d.EarningsCalendarEntry, "estimates", "count"),
        (d.EarningsSurprise, "surprise_pct", "fraction"),
        (d.MarketHoliday, "close_at", "UTC"),
    ],
)
def test_field_units(model, field, expected):
    assert _unit(model, field) == expected


@pytest.mark.parametrize(
    ("model", "time", "group", "values"),
    [
        (d.EconomicObservation, "date", "series_id", ("value",)),
        (d.EconomicSeriesInfo, None, "series_id", ()),
        (d.ReleaseScheduleEntry, "scheduled_at", "release", ()),
        (d.Filing, "accepted_at", "ticker", ()),
        (d.XbrlFact, "period_end", "metric", ("value",)),
        (d.FundamentalValue, "period_end", "metric", ("value",)),
        (d.EarningsRelease, "accepted_at", "ticker", ()),
        (d.EarningsFigure, "accepted_at", "ticker", ("value",)),
        (d.InsiderTransaction, "transaction_date", "ticker", ("value_usd",)),
        (d.InstitutionalHolding, "period_end", "cusip", ("value_usd",)),
        (d.FundReport, "report_date", "series_id", ("net_assets",)),
        (d.FundHolding, "report_date", "series_id", ("value_usd",)),
        (d.FomcMeeting, "start_date", None, ()),
        (d.FomcStatement, "meeting_date", None, ()),
        (d.ReferenceRate, "date", "rate_type", ("rate",)),
        (d.YieldCurvePoint, "date", "tenor", ("rate",)),
        (d.TreasuryAuction, "auction_date", "security_term", ("high_yield",)),
        (d.TreasuryDebt, "date", None, ("total_debt",)),
        (d.TreasuryCashBalance, "date", "account_type", ("closing_balance",)),
        (d.EconomicEvent, "release_at", "event_name", ("actual",)),
        (d.EarningsCalendarEntry, "report_date", "ticker", ("eps_actual",)),
        (d.EarningsSurprise, "report_date", "ticker", ("eps_actual",)),
        (d.MarketHoliday, "holiday_date", "market", ()),
    ],
)
def test_classvars(model, time, group, values):
    assert (model.time_column, model.group_column, model.value_columns) == (time, group, values)


def test_an_observation_with_its_absences_explained():
    row = d.EconomicObservation(
        source="fred",
        series_id="CPIAUCSL",
        title=None,
        date=date(2024, 1, 1),
        period=None,
        frequency=None,
        value=None,
        units=None,
        table_name=None,
        line_number=None,
        knowledge_time=KT,
        pit="vintage",
        absent={"value": "no_data", "title": "not_provided_by_source", "units": "not_provided_by_source"},
    )
    response = {
        "period": {"code": "not_applicable"},
        "frequency": {"code": "not_provided_by_source"},
        "table_name": {"code": "not_applicable"},
        "line_number": {"code": "not_applicable"},
    }
    from marketlens_schema import AbsenceReason

    assert unexplained_absences([row], {k: AbsenceReason(**v) for k, v in response.items()}) == []
    assert unexplained_absences([row], {}) == [
        (0, "period"),
        (0, "frequency"),
        (0, "table_name"),
        (0, "line_number"),
    ]


def test_models_refuse_naive_instants_and_extra_fields():
    with pytest.raises(ValidationError):
        d.FomcMeeting(
            start_date=date(2026, 1, 27),
            end_date=date(2026, 1, 28),
            year=2026,
            has_projection=False,
            knowledge_time=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        d.TreasuryDebt(
            date=date(2026, 1, 2),
            total_debt=1.0,
            debt_held_public=1.0,
            intragovernmental=1.0,
            knowledge_time=KT,
            surplus=0.0,
        )
