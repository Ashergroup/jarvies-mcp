"""Shared response envelope for third-party integration MCP tools.

Adopted from finpilot-agent (`agents/finpilot/models.py`). Used only by the
new read-only integration tool families: `xero_*`, `cin7_*`, `freshsales_*`.
M365 and DB tools keep their existing return shapes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

IntegrationSource = Literal["xero", "cin7", "freshsales"]
IntegrationStatus = Literal["ok", "not_configured", "error", "skipped"]


class IntegrationResult(BaseModel):
    """Normalized integration response returned by Xero/Cin7/Freshsales tools."""

    source: IntegrationSource
    status: IntegrationStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def ok(source: IntegrationSource, data: dict[str, Any]) -> dict[str, Any]:
    """Build a success IntegrationResult as a serializable dict."""

    return IntegrationResult(source=source, status="ok", data=data).model_dump()


def not_configured(source: IntegrationSource, message: str) -> dict[str, Any]:
    """Build a not_configured IntegrationResult as a serializable dict."""

    return IntegrationResult(
        source=source,
        status="not_configured",
        error=message,
    ).model_dump()


def error(source: IntegrationSource, message: str) -> dict[str, Any]:
    """Build an error IntegrationResult as a serializable dict."""

    return IntegrationResult(source=source, status="error", error=message).model_dump()
