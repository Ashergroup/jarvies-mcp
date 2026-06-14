"""Async PostgreSQL connection pool for the MCP server.

asyncpg only — no ORM. A single process-wide pool is created lazily on first
use and (in the server) eagerly on startup via the wrapped lifespan in
``agents.mcp.server``. ``DATABASE_URL`` carries libpq-style options such as
``sslmode=require``, which asyncpg parses from the DSN.

The pool is optional: if ``DATABASE_URL`` is unset the multi-tenant features
are simply unavailable and callers fall back to env-var credentials. Nothing
here is imported for its side effects, so importing this module never opens a
connection.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from agents.mcp.config import get_settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a pool is requested but ``DATABASE_URL`` is not set."""


async def init_pool() -> asyncpg.Pool:
    """Create the process-wide connection pool if it does not exist.

    Idempotent: returns the existing pool on repeat calls. Raises
    ``DatabaseNotConfiguredError`` when ``DATABASE_URL`` is empty.
    """

    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    dsn = settings.database_url
    if not dsn:
        raise DatabaseNotConfiguredError("DATABASE_URL is not configured")

    # asyncpg parses sslmode (e.g. ?sslmode=require) directly from the DSN.
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    log.info("db_pool_initialised", extra={"min_size": 1, "max_size": 10})
    return _pool


async def get_pool() -> asyncpg.Pool:
    """Return the connection pool, initialising it lazily on first use."""

    if _pool is None:
        return await init_pool()
    return _pool


async def close_pool() -> None:
    """Close the connection pool if open. Safe to call when never initialised."""

    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


@asynccontextmanager
async def get_conn() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a pooled connection for the duration of the context."""

    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
