"""Tests for the DNS-rebinding host/origin allowlist derivation.

Regression cover for the live ECS bug: POST /mcp returned 421 "Invalid Host"
because FastMCP's allowed_hosts list was empty, so the on.aws domain was
rejected.
"""

from __future__ import annotations

import pytest

from agents.mcp import config as mcp_config

ECS_HOST = "ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> mcp_config.MCPSettings:
    for key in (
        "JARVIES_PUBLIC_URL",
        "AZURE_REDIRECT_URI",
        "MCP_ALLOWED_HOSTS",
        "MCP_DISABLE_DNS_REBINDING_PROTECTION",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return mcp_config.MCPSettings()


def test_localhost_allowed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = _settings(monkeypatch).allowed_host_values
    assert "localhost" in hosts
    assert "127.0.0.1:*" in hosts


def test_ecs_host_derived_from_azure_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = _settings(
        monkeypatch, AZURE_REDIRECT_URI=f"https://{ECS_HOST}/auth/callback"
    ).allowed_host_values
    assert ECS_HOST in hosts


def test_host_derived_from_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = _settings(monkeypatch, JARVIES_PUBLIC_URL=f"https://{ECS_HOST}").allowed_host_values
    assert ECS_HOST in hosts


def test_explicit_allowed_hosts_csv_added(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = _settings(
        monkeypatch, MCP_ALLOWED_HOSTS="a.example.com, b.example.com:*"
    ).allowed_host_values
    assert "a.example.com" in hosts
    assert "b.example.com:*" in hosts


def test_host_values_are_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = _settings(
        monkeypatch,
        JARVIES_PUBLIC_URL=f"https://{ECS_HOST}",
        AZURE_REDIRECT_URI=f"https://{ECS_HOST}/auth/callback",
    ).allowed_host_values
    assert hosts.count(ECS_HOST) == 1


def test_origins_mirror_hosts_with_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    origins = _settings(
        monkeypatch, JARVIES_PUBLIC_URL=f"https://{ECS_HOST}"
    ).allowed_origin_values
    assert f"https://{ECS_HOST}" in origins
    assert "http://localhost" in origins


def test_disable_flag_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).disable_dns_rebinding_protection is False


def test_disable_flag_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        _settings(monkeypatch, MCP_DISABLE_DNS_REBINDING_PROTECTION="true").disable_dns_rebinding_protection
        is True
    )
