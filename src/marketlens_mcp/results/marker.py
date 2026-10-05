"""The stored-result marker (contract 5.3): a typed summary, a
time-series-shaped preview and exactly three ready-made queries that pass
the query guard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from marketlens_schema import BUILTIN_MODELS, CanonicalModel

from ..results_api import ColumnInfo, Preview, ReadyQuery, ResultInfo, ResultMarker
from .arrow import sql_ident

HOW_TO = (
    "This result is stored, not shown. Query it with results_query using its result_id as the table name "
    "(SELECT ... FROM {rid}), inspect it with results_describe or results_sample, or pass the result_id to an "
    "analytics_* tool."
)

PREVIEW_ROWS = 3
PREVIEW_STRING_CHARS = 200
GROUPS_SAMPLE = 20
NUMERIC_PREFIXES = (
    "DOUBLE",
    "BIGINT",
    "INTEGER",
    "DECIMAL",
    "FLOAT",
    "SMALLINT",
    "TINYINT",
    "HUGEINT",
    "UBIGINT",
)


def truncate_strings(value: Any, limit: int = PREVIEW_STRING_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [truncate_strings(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: truncate_strings(v, limit) for k, v in value.items()}
    return value


def _value_column(info: ResultInfo, models: Mapping[str, type[CanonicalModel]]) -> str | None:
    model = models.get(info.model) or BUILTIN_MODELS.get(info.model)
    numeric = {c.name for c in info.columns if c.type.startswith(NUMERIC_PREFIXES)}
    if model is not None:
        for name in model.value_columns:
            if name in numeric:
                return name
    return None


def ready_queries(
    info: ResultInfo, models: Mapping[str, type[CanonicalModel]] | None = None
) -> list[ReadyQuery]:
    """Exactly three queries for this result's shape."""
    rid = info.result_id
    names = [c.name for c in info.columns]
    star = "* EXCLUDE (absent)" if "absent" in names else "*"
    t = sql_ident(info.time_column) if info.time_column in names else None
    g = sql_ident(info.group_column) if info.group_column in names else None
    v_name = _value_column(info, models or {})
    v = sql_ident(v_name) if v_name else None

    def daily(prefix: str, order: str) -> ReadyQuery:
        if v:
            stats = f"min({v}) AS min_{v_name}, max({v}) AS max_{v_name}, avg({v}) AS avg_{v_name}, count(*) AS rows"
            purpose = f"Daily summary of {v_name}"
        else:
            stats = "count(*) AS rows"
            purpose = "Daily row count" + (" per series" if g else "")
        return ReadyQuery(
            purpose=purpose,
            sql=f"SELECT {prefix}time_bucket(INTERVAL '1 day', {t}) AS day, {stats} FROM {rid} GROUP BY ALL "
            f"ORDER BY {order} LIMIT 50",
        )

    if t and g:
        return [
            ReadyQuery(
                purpose="Coverage per series",
                sql=f"SELECT {g}, count(*) AS rows, min({t}) AS first_t, max({t}) AS last_t FROM {rid} "
                f"GROUP BY {g} ORDER BY {g}",
            ),
            ReadyQuery(
                purpose="Latest row per series",
                sql=f"SELECT {star} FROM {rid} QUALIFY row_number() OVER (PARTITION BY {g} ORDER BY {t} DESC) = 1 "
                f"ORDER BY {g}",
            ),
            daily(f"{g}, ", f"day DESC, {g}"),
        ]
    if t:
        return [
            ReadyQuery(
                purpose="Time span and row count",
                sql=f"SELECT min({t}) AS first_t, max({t}) AS last_t, count(*) AS rows FROM {rid}",
            ),
            ReadyQuery(
                purpose="Last 20 rows by time", sql=f"SELECT {star} FROM {rid} ORDER BY {t} DESC LIMIT 20"
            ),
            daily("", "day DESC"),
        ]
    by = g or next((sql_ident(c.name) for c in info.columns if c.type == "VARCHAR"), None)
    by_name = info.group_column if g else next((c.name for c in info.columns if c.type == "VARCHAR"), None)
    third = (
        ReadyQuery(
            purpose=f"Rows per {by_name}",
            sql=f"SELECT {by}, count(*) AS rows FROM {rid} GROUP BY {by} ORDER BY rows DESC LIMIT 50",
        )
        if by
        else ReadyQuery(
            purpose="Random sample of 10 rows",
            sql=f"SELECT {star} FROM {rid} USING SAMPLE 10 ROWS (reservoir, 42)",
        )
    )
    return [
        ReadyQuery(purpose="Row count", sql=f"SELECT count(*) AS rows FROM {rid}"),
        ReadyQuery(purpose="First 20 rows", sql=f"SELECT {star} FROM {rid} LIMIT 20"),
        third,
    ]


def build_marker(
    info: ResultInfo,
    preview: Preview,
    *,
    notes: Sequence[str] = (),
    models: Mapping[str, type[CanonicalModel]] | None = None,
) -> ResultMarker:
    return ResultMarker(
        result_id=info.result_id,
        tool=info.tool,
        model=info.model,
        schema_version=info.schema_version,
        row_count=info.row_count,
        bytes=info.bytes,
        columns=list(info.columns),
        preview=preview,
        provenance=info.provenance,
        absent=dict(info.absent),
        pagination=info.pagination,
        expires_at=info.expires_at,
        risk=info.risk,
        parents=list(info.parents),
        queries=ready_queries(info, models),
        notes=list(notes),
        how_to=HOW_TO.format(rid=info.result_id),
    )


def with_null_reasons(columns: Sequence[ColumnInfo], absent: Mapping[str, Any]) -> list[ColumnInfo]:
    return [c.model_copy(update={"null_reason": absent[c.name]}) if c.name in absent else c for c in columns]
