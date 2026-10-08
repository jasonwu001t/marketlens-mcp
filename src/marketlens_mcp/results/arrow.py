"""Canonical models -> Arrow and DuckDB types (contract 2.4).

str, Literal, StrEnum -> string (VARCHAR); int -> int64 (BIGINT); float ->
float64 (DOUBLE); bool -> bool (BOOLEAN); DecimalStr -> decimal128(38, 12)
(a value with more fractional digits is refused, never rounded); UtcDatetime
-> timestamp(us, UTC); date -> date32; list[X] -> list<X>; a nested
CanonicalModel -> struct; ``absent`` -> map<string, string>.
"""

from __future__ import annotations

import datetime as dt
import enum
import functools
import math
import re
import types
import typing
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pyarrow as pa
from pydantic import BaseModel

from marketlens_schema import CanonicalModel

from ..plugin_api import ToolError
from ..results_api import ColumnInfo

DECIMAL_TYPE = pa.decimal128(38, 12)
DECIMAL_SCALE = 12
TIMESTAMP_TYPE = pa.timestamp("us", tz="UTC")
ABSENT_TYPE = pa.map_(pa.string(), pa.string())


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """(core type, nullable) with Annotated and Optional removed."""
    nullable = False
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        if origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            if len(args) != len(typing.get_args(annotation)):
                nullable = True
            if len(args) == 1:
                annotation = args[0]
                continue
            raise TypeError(f"unions are not canonical: {annotation!r}")
        return annotation, nullable


def arrow_type(annotation: Any) -> pa.DataType:
    core, _ = _unwrap(annotation)
    origin = typing.get_origin(core)
    if origin is typing.Literal:
        return pa.string()
    if origin in (list, tuple, Sequence):
        (item,) = typing.get_args(core)[:1]
        return pa.list_(arrow_type(item))
    if origin in (dict, Mapping):
        return ABSENT_TYPE
    if isinstance(core, type):
        if issubclass(core, bool):
            return pa.bool_()
        if issubclass(core, enum.Enum) or issubclass(core, str):
            return pa.string()
        if issubclass(core, int):
            return pa.int64()
        if issubclass(core, float):
            return pa.float64()
        if issubclass(core, Decimal):
            return DECIMAL_TYPE
        if issubclass(core, dt.datetime):
            return TIMESTAMP_TYPE
        if issubclass(core, dt.date):
            return pa.date32()
        if issubclass(core, BaseModel):
            return pa.struct([pa.field(f, *_field_type(core, f)) for f in field_names(core)])
    raise TypeError(f"no canonical Arrow type for {annotation!r}")


def _field_type(model: type[BaseModel], name: str) -> tuple[pa.DataType, bool]:
    info = model.model_fields[name]
    if name == "absent" and issubclass(model, CanonicalModel):
        return ABSENT_TYPE, True
    _, nullable = _unwrap(info.annotation)
    return arrow_type(info.annotation), nullable


def field_names(model: type[BaseModel]) -> list[str]:
    """The model's fields in declaration order, with ``absent`` (declared on
    the base class, so first in pydantic's order) moved last."""
    names = [n for n in model.model_fields if n != "absent"]
    return [*names, "absent"] if "absent" in model.model_fields else names


def arrow_schema(model: type[CanonicalModel]) -> pa.Schema:
    """The canonical Arrow schema of a row model (``absent`` last)."""
    return pa.schema([pa.field(name, *_field_type(model, name)) for name in field_names(model)])


def _check_decimals(value: Any) -> None:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ToolError("unrepresentable_decimal", f"The decimal value {value} cannot be stored.")
        exponent = value.normalize().as_tuple().exponent
        if isinstance(exponent, int) and exponent < -DECIMAL_SCALE:
            raise ToolError(
                "unrepresentable_decimal",
                f"The decimal value {value} has more than {DECIMAL_SCALE} fractional digits and cannot be "
                "stored exactly.",
            )
    elif isinstance(value, dict):
        for v in value.values():
            _check_decimals(v)
    elif isinstance(value, list):
        for v in value:
            _check_decimals(v)


def _python_row(row: CanonicalModel) -> dict[str, Any]:
    data = row.model_dump(mode="python")
    absent = data.get("absent")
    if absent is not None:
        data["absent"] = {str(k): str(v.value if isinstance(v, enum.Enum) else v) for k, v in absent.items()}
    _check_decimals(data)
    return data


def rows_to_table(rows: Sequence[CanonicalModel], model: type[CanonicalModel]) -> pa.Table:
    """Canonical rows -> an Arrow table in the model's canonical types."""
    schema = arrow_schema(model)
    return pa.Table.from_pylist([_python_row(r) for r in rows], schema=schema)


# --- DuckDB type names -----------------------------------------------------------------------


def duckdb_type(t: pa.DataType) -> str:
    """The DuckDB name of an Arrow type, as DuckDB's DESCRIBE spells it."""
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "VARCHAR"
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_int64(t):
        return "BIGINT"
    if pa.types.is_int32(t):
        return "INTEGER"
    if pa.types.is_int16(t):
        return "SMALLINT"
    if pa.types.is_int8(t):
        return "TINYINT"
    if pa.types.is_uint64(t):
        return "UBIGINT"
    if pa.types.is_uint32(t):
        return "UINTEGER"
    if pa.types.is_float64(t):
        return "DOUBLE"
    if pa.types.is_float32(t):
        return "FLOAT"
    if pa.types.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP WITH TIME ZONE" if t.tz else "TIMESTAMP"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_time(t):
        return "TIME"
    if pa.types.is_duration(t):
        return "INTERVAL"
    if pa.types.is_null(t):
        return '"NULL"'
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "BLOB"
    if pa.types.is_map(t):
        return f"MAP({duckdb_type(t.key_type)}, {duckdb_type(t.item_type)})"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return f"{duckdb_type(t.value_type)}[]"
    if pa.types.is_struct(t):
        inner = ", ".join(
            f"{_ident(t.field(i).name)} {duckdb_type(t.field(i).type)}" for i in range(t.num_fields)
        )
        return f"STRUCT({inner})"
    return str(t).upper()


@functools.cache
def duckdb_keywords() -> dict[str, str]:
    """DuckDB's keywords and their categories (reserved, unreserved, ...)."""
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        rows = (
            con.execute("SELECT keyword_name, keyword_category FROM duckdb_keywords()")
            .to_arrow_table()
            .to_pylist()
        )
    finally:
        con.close()
    return {r["keyword_name"]: r["keyword_category"] for r in rows}


def _ident(name: str) -> str:
    """A struct field name as DuckDB prints it: quoted when it is a keyword or
    not a plain lower-case identifier."""
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and name not in duckdb_keywords():
        return name
    return '"' + name.replace('"', '""') + '"'


def sql_ident(name: str) -> str:
    """A column name for SQL the server writes: quoted only when needed
    (a reserved keyword, or not a plain lower-case identifier)."""
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and duckdb_keywords().get(name) != "reserved":
        return name
    return '"' + name.replace('"', '""') + '"'


def _unit(model: type[BaseModel], name: str) -> str | None:
    extra = model.model_fields[name].json_schema_extra
    if isinstance(extra, dict):
        u = extra.get("x-unit")
        return str(u) if u else None
    return None


def column_infos(model: type[CanonicalModel]) -> list[ColumnInfo]:
    """ColumnInfo for every column of a canonical model (no summary values)."""
    schema = arrow_schema(model)
    out = []
    for field in schema:
        if field.name == "absent":
            desc = "Per-row reasons for None fields: field name -> AbsenceCode"
        else:
            desc = model.model_fields[field.name].description
        out.append(
            ColumnInfo(
                name=field.name,
                type=duckdb_type(field.type),
                unit=_unit(model, field.name) if field.name != "absent" else None,
                nullable=field.nullable,
                description=desc,
            )
        )
    return out


def column_infos_for(
    schema: pa.Schema, model: type[CanonicalModel] | None, units: Mapping[str, str] | None = None
) -> list[ColumnInfo]:
    """ColumnInfo for a table's actual columns: types from the table, units
    and descriptions from ``model`` where a column of the same name and type
    exists there (``units`` overrides)."""
    infos = column_infos_from_schema(schema, units)
    if model is None:
        return infos
    known = {c.name: c for c in column_infos(model)}
    out = []
    for c in infos:
        k = known.get(c.name)
        if k is not None and k.type == c.type:
            c = c.model_copy(
                update={"unit": c.unit or k.unit, "description": k.description, "nullable": k.nullable}
            )
        out.append(c)
    return out


def column_infos_from_schema(schema: pa.Schema, units: Mapping[str, str] | None = None) -> list[ColumnInfo]:
    """ColumnInfo for a dynamic result (types from the Arrow schema). A column's
    unit is ``units``' entry, else the one its Arrow field carries in metadata
    (``{b"unit": b"percent"}``): how a handler labels a dynamic table, which has
    no model to state ``x-unit``."""
    units = units or {}
    return [
        ColumnInfo(
            name=f.name,
            type=duckdb_type(f.type),
            unit=units.get(f.name) or _field_unit(f),
            nullable=f.nullable,
        )
        for f in schema
    ]


def _field_unit(field: pa.Field) -> str | None:
    raw = (field.metadata or {}).get(b"unit")
    return raw.decode("utf-8") if raw else None


# --- JSON-safe values ------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    """A value from Arrow or pydantic, made JSON-safe: instants as ISO-8601
    with Z, dates ISO, decimals as plain strings, NaN/inf as None, Arrow map
    pairs as objects."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        text = format(value.normalize(), "f")
        return text
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, tuple) and len(v) == 2 for v in value):
            return {str(k): json_safe(v) for k, v in value}
        return [json_safe(v) for v in value]
    return str(value)


def table_rows(table: pa.Table) -> list[dict[str, Any]]:
    """JSON-safe dicts of every row of ``table``; a null ``absent`` is omitted."""
    out = []
    for row in table.to_pylist():
        d = {k: json_safe(v) for k, v in row.items()}
        if "absent" in d and d["absent"] is None:
            del d["absent"]
        out.append(d)
    return out
