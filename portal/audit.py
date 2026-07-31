"""Audit-log writes for the portal.

``write_audit`` inserts one ``audit_log`` row and takes an existing connection so
the caller runs it inside the SAME transaction as the change it records — the
audit row and the state change commit or roll back together.
"""

from __future__ import annotations

import json
from typing import Any

_INSERT_AUDIT = """
    INSERT INTO audit_log
        (tenant_id, actor_type, actor_id, action, target_type, target_id, metadata)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb)
"""


async def write_audit(
    conn,
    *,
    tenant_id: str | None,
    actor_type: str,
    actor_id: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert an audit_log row on the given connection (no commit of its own)."""

    await conn.execute(
        _INSERT_AUDIT,
        tenant_id,
        actor_type,
        actor_id,
        action,
        target_type,
        target_id,
        json.dumps(metadata) if metadata is not None else None,
    )
