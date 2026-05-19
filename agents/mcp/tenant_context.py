"""Tenant context helpers for multi-tenant MCP tool execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, Field, field_validator

from agents.mcp.config import get_settings


class TenantContext(BaseModel):
    """Identity, token, and permission data for one MCP tool request."""

    tenant_id: str
    user_id: str
    access_token: str | None = Field(default=None, repr=False)
    permissions: set[str] = Field(default_factory=set)

    @field_validator("permissions", mode="before")
    @classmethod
    def _normalise_permissions(cls, value: object) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            return {part.strip() for part in value.split(",") if part.strip()}
        if isinstance(value, (list, tuple, set)):
            return {str(part).strip() for part in value if str(part).strip()}
        raise TypeError("permissions must be a list of strings or a comma-separated string")


_current_context: ContextVar[TenantContext | None] = ContextVar(
    "mcp_tenant_context",
    default=None,
)


def build_tenant_context(
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | str | set[str] | None = None,
) -> TenantContext:
    """Create a tenant context from MCP tool arguments and defaults."""

    settings = get_settings()
    supplied_permissions = permissions
    if supplied_permissions is None:
        supplied_permissions = settings.default_permission_values

    return TenantContext(
        tenant_id=tenant_id or settings.default_tenant_id,
        user_id=user_id or settings.default_user_id,
        access_token=access_token,
        permissions=supplied_permissions,
    )


@contextmanager
def use_tenant_context(context: TenantContext) -> Iterator[TenantContext]:
    """Set the current context for the duration of one tool execution."""

    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


def current_tenant_context() -> TenantContext | None:
    """Return the context for the current tool call, if one is active."""

    return _current_context.get()
