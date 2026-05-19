"""Configuration for the MCP platform.

All production-sensitive settings are sourced from environment variables so
the same code can run locally, in Docker, and on AWS App Runner.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_M365_AGENT_PATH = Path("C:/Users/DELL/Documents/agents/m365_agent_v2")


class MCPSettings(BaseSettings):
    """Runtime settings for the MCP server.

    Secrets must be supplied by environment variables or a local `.env` file.
    The defaults are development-friendly but production keeps authentication
    closed unless API key or JWT verification is configured.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        validation_alias="ENVIRONMENT",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        validation_alias="MCP_LOG_LEVEL",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8080, validation_alias="PORT")

    api_keys: str = Field(default="", validation_alias="MCP_API_KEYS")
    allow_unauthenticated: bool = Field(
        default=False,
        validation_alias="MCP_ALLOW_UNAUTHENTICATED",
    )
    jwt_secret: str = Field(default="", validation_alias="MCP_JWT_SECRET")
    jwt_audience: str = Field(default="", validation_alias="MCP_JWT_AUDIENCE")
    jwt_issuer: str = Field(default="", validation_alias="MCP_JWT_ISSUER")

    default_tenant_id: str = Field(default="local", validation_alias="MCP_DEFAULT_TENANT_ID")
    default_user_id: str = Field(default="local-user", validation_alias="MCP_DEFAULT_USER_ID")
    default_permissions: str = Field(default="", validation_alias="MCP_DEFAULT_PERMISSIONS")

    m365_agent_path: Path | None = Field(
        default=DEFAULT_M365_AGENT_PATH,
        validation_alias="M365_AGENT_PATH",
    )
    m365_draft_unattended: bool = Field(
        default=True,
        validation_alias="M365_DRAFT_UNATTENDED",
    )

    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    db_readonly: bool = Field(default=True, validation_alias="MCP_DB_READONLY")
    db_statement_timeout_ms: int = Field(
        default=5_000,
        ge=100,
        le=60_000,
        validation_alias="MCP_DB_STATEMENT_TIMEOUT_MS",
    )
    db_max_rows: int = Field(default=100, ge=1, le=1_000, validation_alias="MCP_DB_MAX_ROWS")

    tool_result_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        validation_alias="MCP_TOOL_RESULT_LIMIT",
    )

    integration_http_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        validation_alias="MCP_INTEGRATION_HTTP_TIMEOUT_SECONDS",
    )

    xero_client_id: str = Field(default="", validation_alias="XERO_CLIENT_ID")
    xero_client_secret: str = Field(default="", validation_alias="XERO_CLIENT_SECRET")
    xero_tenant_id: str = Field(default="", validation_alias="XERO_TENANT_ID")
    xero_scopes: str = Field(
        default=(
            "accounting.transactions.read accounting.contacts.read "
            "accounting.reports.read accounting.settings.read"
        ),
        validation_alias="XERO_SCOPES",
    )
    xero_identity_url: str = Field(
        default="https://identity.xero.com/connect/token",
        validation_alias="XERO_IDENTITY_URL",
    )
    xero_base_url: str = Field(
        default="https://api.xero.com/api.xro/2.0",
        validation_alias="XERO_BASE_URL",
    )

    cin7_api_key: str = Field(default="", validation_alias="CIN7_API_KEY")
    cin7_account_id: str = Field(default="", validation_alias="CIN7_ACCOUNT_ID")
    cin7_base_url: str = Field(
        default="https://inventory.dearsystems.com/ExternalApi/v2",
        validation_alias="CIN7_BASE_URL",
    )

    freshsales_domain: str = Field(default="", validation_alias="FRESHSALES_DOMAIN")
    freshsales_api_key: str = Field(default="", validation_alias="FRESHSALES_API_KEY")

    @field_validator("api_keys", "default_permissions")
    @classmethod
    def _strip_csv(cls, value: str) -> str:
        return ",".join(part.strip() for part in value.split(",") if part.strip())

    @property
    def is_production(self) -> bool:
        """Return True when the server is running in production mode."""

        return self.environment == "production"

    @property
    def api_key_values(self) -> set[str]:
        """Configured API keys for testing and internal MCP clients."""

        return {part.strip() for part in self.api_keys.split(",") if part.strip()}

    @property
    def default_permission_values(self) -> set[str]:
        """Default permissions used only when a tool call omits permissions."""

        return {
            part.strip()
            for part in self.default_permissions.split(",")
            if part.strip()
        }

    @property
    def xero_configured(self) -> bool:
        """Return True when client-credentials Xero auth can be attempted."""

        return bool(self.xero_client_id and self.xero_client_secret and self.xero_tenant_id)

    @property
    def cin7_configured(self) -> bool:
        """Return True when Cin7 API credentials are present."""

        return bool(self.cin7_api_key and self.cin7_account_id)

    @property
    def freshsales_configured(self) -> bool:
        """Return True when Freshsales API credentials are present."""

        return bool(self.freshsales_domain and self.freshsales_api_key)


@lru_cache
def get_settings() -> MCPSettings:
    """Return cached MCP settings."""

    return MCPSettings()
