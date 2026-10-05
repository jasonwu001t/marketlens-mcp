"""The query guard for model-written SQL (contract 5.4, steps 1-5 and 7).

One statement, a SELECT (or a set operation of SELECTs, optionally WITH),
no DML/DDL/PRAGMA/SET/COPY/ATTACH/INSTALL/LOAD/CALL, no parameters, no
function that reads files or settings, and tables only by this session's
result ids or the names of CTEs DuckDB binds at that point (resolved per
scope, so a CTE cannot hide a catalog view such as pg_settings elsewhere in
the statement). The store then runs it in a connection that cannot touch files
at all (steps 6 and 8, in store.py): the guard is the belt, the connection
settings are the wall, and the path scrub here is the last line for error
texts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from ..results_api import RESULT_ID_RE, QueryRefused

MAX_SQL_CHARS = 20_000
MAX_RESULTS_PER_QUERY = 8

#: Functions refused by name (lower case). The belt; the connection's
#: disabled external access is the wall.
DENIED_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "parquet_scan",
        "parquet_metadata",
        "parquet_schema",
        "read_json",
        "read_json_auto",
        "read_json_objects",
        "read_ndjson",
        "read_ndjson_objects",
        "read_text",
        "read_blob",
        "glob",
        "sniff_csv",
        "csv_scan",
        "delta_scan",
        "iceberg_scan",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "query",
        "query_table",
        "getenv",
        "load_extension",
        "duckdb_secrets",
        "duckdb_extensions",
        "duckdb_settings",
        "duckdb_databases",
        "current_setting",
        "which_secret",
    }
)

#: Whole families refused by prefix or suffix, so a member missing from the
#: list above is refused too (DuckDB grows these families between releases).
DENIED_PREFIXES = (
    "read_",
    "duckdb_",
    "pragma_",
    "parquet_",
    "sqlite_",
    "postgres_",
    "mysql_",
    "iceberg_",
    "delta_",
    "json_execute",
)
DENIED_SUFFIXES = ("_scan",)

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Into",
        "Insert",
        "Update",
        "Delete",
        "Create",
        "Drop",
        "Alter",
        "Merge",
        "Command",
        "Pragma",
        "Set",
        "Copy",
        "Attach",
        "Detach",
        "Install",
        "Use",
        "Describe",
        "Show",
        "Transaction",
        "Commit",
        "Rollback",
        "Placeholder",
        "Parameter",
        "Summarize",
        "Pivot",
        "Export",
        "Load",
        "LoadData",
        "Refresh",
        "Cache",
        "Uncache",
        "Grant",
        "Revoke",
        "Comment",
        "Analyze",
        "TruncateTable",
    )
    if hasattr(exp, name)
)

_SET_OPERATIONS = tuple(getattr(exp, n) for n in ("Union", "Intersect", "Except") if hasattr(exp, n))

_PATH_RE = re.compile(r"""(?<![\w.\-/<>~:])(?:[A-Za-z]:\\|/)[^\s"'`,;)]+""")

logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass(frozen=True)
class Checked:
    ast: exp.Expression
    result_ids: tuple[str, ...]
    cte_names: frozenset[str]


def scrub(text: str, *known_paths: str) -> str:
    """Replace every absolute-path-like substring (and the given paths) with
    ``<path>``. Applied to every error text before it leaves the server."""
    for p in sorted((k for k in known_paths if k), key=len, reverse=True):
        text = text.replace(p, "<path>")
    text = _PATH_RE.sub("<path>", text)
    return re.sub(r"<path>[^\s\"']*", "<path>", text)


def _first_line(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(text)).strip()
    return text.splitlines()[0] if text else ""


def _function_name(node: exp.Func) -> str:
    """The name DuckDB resolves: quotes dropped and case folded, because DuckDB
    treats "current_setting", "CURRENT_SETTING" and main.current_setting as one
    function (a quoted name kept its quotes here once and passed the deny-list)."""
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    return node.sql_name().lower()


def _denied(name: str) -> bool:
    return name in DENIED_FUNCTIONS or name.startswith(DENIED_PREFIXES) or name.endswith(DENIED_SUFFIXES)


def _is_select_tree(node: exp.Expression) -> bool:
    if isinstance(node, exp.Select):
        return True
    if isinstance(node, _SET_OPERATIONS):
        return _is_select_tree(node.this) and _is_select_tree(node.expression)
    if isinstance(node, exp.Subquery):
        return _is_select_tree(node.this)
    return False


def _inside(node: exp.Expression | None, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _with_clause(node: exp.Expression) -> exp.With | None:
    clause = node.args.get("with_", node.args.get("with"))
    return clause if isinstance(clause, exp.With) else None


def _binds_to_cte(table: exp.Table) -> bool:
    """Whether DuckDB binds this table name to a CTE (else: the catalog).

    Walking up from the table: in the main body of a query every CTE of its
    WITH is visible; inside the body of a CTE only the CTEs defined before it
    in the same WITH are. The CTE's own name binds only in DuckDB's recursive
    form: WITH RECURSIVE, the body a top-level UNION [ALL] (not BY NAME,
    EXCEPT or INTERSECT), the table in its right branch. Any other use of the
    own name is refused, even where an outer CTE of that name would bind.
    This is structural on purpose: sqlglot's scope sources are looser than
    DuckDB (both branches of any set operation; an alias such as
    ``c AS pg_settings`` overwrites a real ``pg_settings`` source)."""
    name = table.name
    child, node = table, table.parent
    while node is not None:
        if isinstance(node, exp.With):
            if isinstance(child, exp.CTE):
                if child.alias == name:
                    body = child.this
                    return (
                        bool(node.args.get("recursive"))
                        and isinstance(body, exp.Union)
                        and not body.args.get("by_name")
                        and _inside(table, body.expression)
                    )
                position = next(i for i, cte in enumerate(node.expressions) if cte is child)
                if any(cte.alias == name for cte in node.expressions[:position]):
                    return True
        else:
            clause = _with_clause(node)
            if (
                clause is not None
                and child is not clause
                and any(cte.alias == name for cte in clause.expressions)
            ):
                return True
        child, node = node, node.parent
    return False


def _cte_references(root: exp.Expression) -> set[int]:
    """The ids of the ``exp.Table`` nodes that DuckDB binds to a CTE."""
    return {id(table) for table in root.find_all(exp.Table) if _binds_to_cte(table)}


def check(sql: str, *, live_ids: Collection[str]) -> Checked:
    """Steps 1-5. Raises QueryRefused with a readable reason."""
    if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
        raise QueryRefused("sql_empty", "The SQL is empty (or contains a NUL character).")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryRefused("sql_too_long", f"The SQL is longer than {MAX_SQL_CHARS:,} characters.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except (ParseError, TokenError) as exc:
        raise QueryRefused("sql_parse", f"The SQL does not parse: {scrub(_first_line(exc))}") from None
    if not statements:
        raise QueryRefused("sql_empty", "The SQL is empty.")
    if len(statements) > 1:
        raise QueryRefused(
            "sql_multiple", f"Send exactly one statement; this SQL has {len(statements)} (separated by ';')."
        )
    root = statements[0]
    if not _is_select_tree(root):
        raise QueryRefused(
            "sql_not_select",
            f"Only one SELECT (or WITH ... SELECT) statement is accepted; this is a {type(root).__name__} statement.",
        )
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            kind = "a parameter placeholder" if isinstance(node, exp.Placeholder) else type(node).__name__
            raise QueryRefused(
                "sql_forbidden", f"The SQL contains {kind}, which results_query does not allow."
            )
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if _denied(name):
                raise QueryRefused(
                    "sql_function",
                    f"The function {name}() is not allowed: results_query reads only this session's stored results.",
                )
    ctes = frozenset(cte.alias_or_name for cte in root.find_all(exp.CTE))
    try:
        cte_refs = _cte_references(root)
    except Exception:  # fail closed: no scope analysis, no query
        raise QueryRefused(
            "sql_parse", "The SQL's table references could not be analysed; simplify the statement."
        ) from None
    live = set(live_ids)
    referenced: list[str] = []
    for table in root.find_all(exp.Table):
        ident = table.this
        name = ident.name if isinstance(ident, exp.Identifier) else ""
        bare = (
            isinstance(ident, exp.Identifier) and not table.args.get("db") and not table.args.get("catalog")
        )
        if bare and name in ctes and id(table) in cte_refs:
            continue
        if bare and RESULT_ID_RE.match(name) and name in live:
            if name not in referenced:
                referenced.append(name)
            continue
        shown = (
            table.sql(dialect="duckdb")
            if not isinstance(ident, exp.Identifier)
            else ".".join(
                p
                for p in (
                    table.args.get("catalog") and table.catalog,
                    table.args.get("db") and table.db,
                    name,
                )
                if p
            )
        )
        listing = ", ".join(sorted(live)) or "none"
        scope_note = (
            " A WITH name counts only inside the WITH that defines it, after its definition"
            " (inside its own body only in the second branch of WITH RECURSIVE ... UNION [ALL])."
            if bare and name in ctes
            else ""
        )
        raise QueryRefused(
            "sql_table",
            f"Table {scrub(shown)[:80]!s} is not one of this session's results.{scope_note} "
            f"Use a result_id as the table name. Live results in this session: {listing}.",
        )
    if len(referenced) > MAX_RESULTS_PER_QUERY:
        raise QueryRefused(
            "sql_too_many_results",
            f"One query may read at most {MAX_RESULTS_PER_QUERY} results; this one reads {len(referenced)}.",
        )
    return Checked(ast=root, result_ids=tuple(referenced), cte_names=ctes)


def force_limit(ast: exp.Expression, cap: int) -> exp.Expression:
    """Step 7: at most ``cap + 1`` rows (the extra row tells truncation)."""
    limit = cap + 1
    tree = ast.copy()
    if isinstance(tree, exp.Select) and not tree.args.get("fetch"):
        current = tree.args.get("limit")
        if current is None:
            return tree.limit(limit, copy=False)
        value = current.args.get("expression")
        if isinstance(value, exp.Literal) and not value.is_string and str(value.this).isdigit():
            current.set("expression", exp.Literal.number(min(int(value.this), limit)))
            return tree
    wrapped = exp.select("*").from_(
        exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier("q")))
    )
    return wrapped.limit(limit, copy=False)


def render(ast: exp.Expression) -> str:
    return ast.sql(dialect="duckdb")
