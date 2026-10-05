"""Canonical models -> Arrow and DuckDB types (contract 2.4)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest
from coresupport import bar_rows

from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.results import arrow
from marketlens_schema import BUILTIN_MODELS
from marketlens_schema.market import Bar, OptionContract, OptionDeliverable
from marketlens_schema.portfolio import Account


def test_scalar_mapping_for_bar():
    s = arrow.arrow_schema(Bar)
    assert s.field("ticker").type == pa.string()
    assert s.field("asset_class").type == pa.string()
    assert s.field("t").type == pa.timestamp("us", tz="UTC")
    assert s.field("open").type == pa.float64()
    assert s.field("trade_count").type == pa.int64()
    assert s.field("absent").type == pa.map_(pa.string(), pa.string())
    assert s.names[-1] == "absent"
    assert not s.field("ticker").nullable
    assert s.field("vwap").nullable


def test_decimal_date_list_and_nested_mapping():
    a = arrow.arrow_schema(Account)
    assert a.field("cash").type == pa.decimal128(38, 12)
    assert a.field("balance_as_of").type == pa.date32()
    assert a.field("trading_blocked").type == pa.bool_()
    oc = arrow.arrow_schema(OptionContract)
    deliverable = oc.field("deliverables").type
    assert pa.types.is_list(deliverable)
    assert pa.types.is_struct(deliverable.value_type)
    assert deliverable.value_type.field("amount").type == pa.decimal128(38, 12)


def test_rows_to_table_round_trip():
    rows = bar_rows(n=8)
    table = arrow.rows_to_table(rows, Bar)
    assert table.num_rows == 24
    assert table.schema == arrow.arrow_schema(Bar)
    first = table.slice(0, 1).to_pylist()[0]
    assert first["t"] == rows[0].t
    assert first["absent"] == [("vwap", "not_provided_by_source")]
    assert table.column("absent")[1].as_py() is None


def test_nested_rows_convert():
    row = OptionContract(
        occ_symbol="AAPL250117C00150000",
        underlying="AAPL",
        root_symbol="AAPL",
        status="active",
        tradable=True,
        expiration_date=dt.date(2025, 1, 17),
        strike=150.0,
        option_type="call",
        style="american",
        multiplier=100.0,
        deliverables=[OptionDeliverable(type="equity", ticker="AAPL", amount=Decimal("100"))],
    )
    t = arrow.rows_to_table([row], OptionContract)
    assert t.column("deliverables")[0].as_py()[0]["amount"] == Decimal("100")


def test_unrepresentable_decimal_is_refused_not_rounded():
    acct = Account(
        environment="paper",
        account_id="x",
        status="ACTIVE",
        cash=Decimal("1.0000000000001"),
        equity=Decimal("1"),
        buying_power=Decimal("1"),
    )
    with pytest.raises(ToolError) as info:
        arrow.rows_to_table([acct], Account)
    assert info.value.code == "unrepresentable_decimal"
    ok = Account(
        environment="paper",
        account_id="x",
        status="ACTIVE",
        cash=Decimal("1.500000000000000"),
        equity=Decimal("1"),
        buying_power=Decimal("1"),
    )
    assert arrow.rows_to_table([ok], Account).column("cash")[0].as_py() == Decimal("1.5")


@pytest.mark.parametrize("name", sorted(BUILTIN_MODELS))
def test_duckdb_type_names_match_duckdb(name):
    model = BUILTIN_MODELS[name]
    table = arrow.arrow_schema(model).empty_table()
    con = duckdb.connect(":memory:")
    con.register("x", table)
    described = {
        r["column_name"]: r["column_type"] for r in con.execute("DESCRIBE x").to_arrow_table().to_pylist()
    }
    infos = arrow.column_infos(model)
    assert [c.name for c in infos] == list(described)
    for c in infos:
        assert c.type == described[c.name], c.name


def test_column_infos_carry_units_and_descriptions():
    infos = {c.name: c for c in arrow.column_infos(Bar)}
    assert infos["close"].unit == "price"
    assert infos["t"].unit == "UTC"
    assert infos["t"].description == "Bar start"
    assert infos["t"].nullable is False
    assert infos["vwap"].nullable is True
    assert infos["ticker"].unit is None
    assert infos["close"].min is None and infos["close"].nulls is None


def test_column_infos_for_dynamic_tables():
    t = pa.table(
        {
            "a": [1],
            "b": ["x"],
            "c": pa.array([dt.datetime(2026, 1, 1, tzinfo=dt.UTC)], pa.timestamp("us", tz="UTC")),
        }
    )
    infos = arrow.column_infos_from_schema(t.schema, units={"a": "count"})
    assert [(c.name, c.type, c.unit) for c in infos] == [
        ("a", "BIGINT", "count"),
        ("b", "VARCHAR", None),
        ("c", "TIMESTAMP WITH TIME ZONE", None),
    ]


def test_json_safe_values():
    assert arrow.json_safe(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)) == "2026-01-02T03:04:05Z"
    assert arrow.json_safe(dt.date(2026, 1, 2)) == "2026-01-02"
    assert arrow.json_safe(Decimal("1.500000000000")) == "1.5"
    assert arrow.json_safe(Decimal("100.000000000000")) == "100"
    assert arrow.json_safe(float("nan")) is None
    assert arrow.json_safe([("a", "b")]) == {"a": "b"}  # Arrow maps arrive as pairs
    assert arrow.json_safe({"x": dt.date(2026, 1, 1)}) == {"x": "2026-01-01"}
