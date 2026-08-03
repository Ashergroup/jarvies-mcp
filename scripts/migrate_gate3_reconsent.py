"""Gate 3 (stages 1-3): force M365 re-consent by clearing stored user tokens.

WHY THIS EXISTS
---------------
Before Gate 3, the authorize request consented to ``User.Read`` only
(``oauth.MS_REDIRECT_SCOPE``) while the refresh asked for seven more scopes
(``m365_write_tools._REFRESH_SCOPE``). Widening the authorize request fixes NEW
grants but does nothing for rows already in ``user_tokens``: their refresh tokens
were minted against the narrow grant, so every refresh returns AADSTS65001. The
old blanket ``except (httpx.HTTPError, ValueError)`` swallowed that into a warning
and reused the narrower access token, which then 403'd against Graph with no
usable diagnostic. That is precisely how the mismatch stayed invisible.

Clearing the rows makes the next call resolve no token, return
``_NO_TOKEN_MESSAGE``, and drive the user back through ``/authorize`` — where they
consent to the widened set. The alternative (leaving them) is an indefinite loop
of doomed refreshes.

DELETE, not UPDATE ... SET NULL: the row carries no history worth keeping, and
``users``/``tenants`` are unaffected (``user_tokens.user_id`` references
``users(id)``; deleting the token row does not touch the user). ``oauth
._persist_identity`` DELETEs and re-INSERTs this row on every sign-in anyway, so a
missing row is a state the code already handles.

Idempotent: running it twice deletes nothing the second time. Safe to re-run.

NOT RUN AUTOMATICALLY. Run it once, immediately after deploying the widened
scopes — not before, or users will re-consent to the old narrow set.

Usage::

    # DATABASE_URL must be set (never hardcode it). For RDS include sslmode:
    #   postgresql://USER:PASS@HOST:5432/jarvies?sslmode=require
    python scripts/migrate_gate3_reconsent.py

    # Preview the row count without deleting anything:
    python scripts/migrate_gate3_reconsent.py --dry-run

asyncpg only — no ORM. Same shape as scripts/migrate.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a dev convenience only.
    pass


# Kept as a module constant so the statement under review is the statement that
# runs. There is no WHERE clause on purpose: every pre-Gate-3 grant is narrow, and
# a row written post-deploy is re-created on the user's next sign-in regardless.
DELETE_USER_TOKENS = "DELETE FROM user_tokens"

COUNT_USER_TOKENS = "SELECT COUNT(*) FROM user_tokens"


async def _count(conn: asyncpg.Connection) -> int:
    return int(await conn.fetchval(COUNT_USER_TOKENS) or 0)


async def _clear_user_tokens(conn: asyncpg.Connection) -> int:
    """Delete every stored M365 token. Returns the number of rows removed."""

    before = await _count(conn)
    await conn.execute(DELETE_USER_TOKENS)
    after = await _count(conn)
    removed = before - after
    print(
        f"reconsent: cleared {removed} user_tokens row(s) "
        f"({before} before, {after} after)"
    )
    if removed:
        print(
            "reconsent: affected users will get "
            '"No M365 access token available — please reconnect via OAuth" '
            "on their next M365 tool call, then re-consent via /authorize."
        )
    return removed


async def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: DATABASE_URL is not set. Export it (with sslmode=require "
            "for RDS) and re-run."
        )
        return 1

    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=15)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"ERROR: could not connect to the database: {exc.__class__.__name__}: {exc}")
        return 1

    try:
        if dry_run:
            count = await _count(conn)
            print(f"reconsent: DRY RUN — {count} user_tokens row(s) would be deleted.")
            print("reconsent: no changes made.")
            return 0
        # One transaction so a partial failure leaves nothing half-applied.
        async with conn.transaction():
            await _clear_user_tokens(conn)
    except asyncpg.PostgresError as exc:
        print(f"ERROR: delete failed: {exc.__class__.__name__}: {exc}")
        return 1
    finally:
        await conn.close()

    print("reconsent: done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
