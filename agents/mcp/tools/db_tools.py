"""Safe PostgreSQL MCP tools.

Database tools are read-only by default, validate SQL before execution, and
support parameterized queries. Use a dedicated read-only DB user in production.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from agents.mcp.config import get_settings
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_DANGEROUS_SQL = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|"
    r"execute|merge|vacuum|analyze|refresh|reindex|listen|notify|set|reset"
    r")\b",
    re.IGNORECASE,
)
_DANGEROUS_FUNCTIONS = re.compile(r"\b(pg_sleep|dblink|lo_import|lo_export)\b", re.IGNORECASE)


class UnsafeQueryError(ValueError):
    """Raised when a SQL query violates MCP database safety rules."""


def _validate_readonly_query(query: str) -> str:
    """Return a sanitized read-only SQL query or raise UnsafeQueryError."""

    cleaned = query.strip()
    if not cleaned:
        raise UnsafeQueryError("Query cannot be empty")
    if "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
        raise UnsafeQueryError("SQL comments are not allowed in MCP queries")

    cleaned = cleaned[:-1].strip() if cleaned.endswith(";") else cleaned
    if ";" in cleaned:
        raise UnsafeQueryError("Multiple SQL statements are not allowed")

    lowered = cleaned.lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise UnsafeQueryError("Only SELECT or read-only WITH queries are allowed")
    if _DANGEROUS_SQL.search(cleaned):
        raise UnsafeQueryError("Dangerous SQL keyword detected")
    if _DANGEROUS_FUNCTIONS.search(cleaned):
        raise UnsafeQueryError("Dangerous SQL function detected")
    return cleaned


def _validate_identifier(identifier: str, kind: str) -> None:
    if not _IDENTIFIER.match(identifier):
        raise UnsafeQueryError(f"Unsafe {kind} identifier: {identifier}")


def _quote_identifier(identifier: str) -> str:
    _validate_identifier(identifier, "SQL")
    return ".".join(f'"{part}"' for part in identifier.split("."))


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [{key: _json_safe(value) for key, value in dict(row).items()} for row in rows]


async def _connect():
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured for MCP database tools")

    import asyncpg

    return await asyncpg.connect(settings.database_url)


async def db_read_query(
    query: str,
    parameters: list[Any] | None = None,
    limit: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Run a validated read-only PostgreSQL query with asyncpg parameters.

    Use asyncpg-style placeholders (`$1`, `$2`, ...). The MCP layer rejects
    writes, multiple statements, SQL comments, and dangerous administrative
    functions before execution.
    """

    context = build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )
    with use_tenant_context(context):
        check_permission(context.tenant_id, context.user_id, "db_read_query", context.permissions)
        settings = get_settings()
        safe_query = _validate_readonly_query(query)
        row_limit = max(1, min(limit or settings.db_max_rows, settings.db_max_rows))
        if not re.search(r"\blimit\b", safe_query, flags=re.IGNORECASE):
            safe_query = f"SELECT * FROM ({safe_query}) AS mcp_read_query LIMIT {row_limit}"

        conn = await _connect()
        try:
            async with conn.transaction(readonly=settings.db_readonly):
                await conn.execute(
                    f"SET LOCAL statement_timeout = {settings.db_statement_timeout_ms}"
                )
                rows = await conn.fetch(safe_query, *(parameters or []))
        finally:
            await conn.close()

        return {"row_count": len(rows), "rows": _rows_to_dicts(rows)}


async def db_select(
    table: str,
    columns: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Safely select rows from a table using parameterized equality filters."""

    context = build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )
    with use_tenant_context(context):
        check_permission(context.tenant_id, context.user_id, "db_select", context.permissions)
        settings = get_settings()
        _validate_identifier(table, "table")

        selected_columns = columns or ["*"]
        if selected_columns == ["*"]:
            column_sql = "*"
        else:
            for column in selected_columns:
                _validate_identifier(column, "column")
            column_sql = ", ".join(_quote_identifier(column) for column in selected_columns)

        where_parts: list[str] = []
        values: list[Any] = []
        for index, (column, value) in enumerate((filters or {}).items(), start=1):
            _validate_identifier(column, "filter column")
            where_parts.append(f"{_quote_identifier(column)} = ${index}")
            values.append(value)

        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        row_limit = max(1, min(limit or settings.db_max_rows, settings.db_max_rows))
        query = (
            f"SELECT {column_sql} FROM {_quote_identifier(table)}"
            f"{where_sql} LIMIT {row_limit}"
        )

        conn = await _connect()
        try:
            async with conn.transaction(readonly=settings.db_readonly):
                await conn.execute(
                    f"SET LOCAL statement_timeout = {settings.db_statement_timeout_ms}"
                )
                rows = await conn.fetch(query, *values)
        finally:
            await conn.close()

        return {"row_count": len(rows), "rows": _rows_to_dicts(rows)}


def register(mcp: Any) -> None:
    """Register PostgreSQL MCP tools."""

    mcp.tool()(db_read_query)
    mcp.tool()(db_select)
