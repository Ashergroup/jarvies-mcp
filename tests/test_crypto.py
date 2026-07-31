"""Tests for the Fernet credential cipher (agents.mcp.crypto).

The key is supplied via JARVIES_ENCRYPTION_KEY and read through the cached
``get_settings()``; each test clears both the settings cache and the cipher
cache, mirroring the cache-clearing style in tests/test_credentials.py.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from agents.mcp import config as mcp_config
from agents.mcp import crypto


@pytest.fixture(autouse=True)
def _clear_caches():
    mcp_config.get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


@pytest.fixture
def _with_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIES_ENCRYPTION_KEY", key)
    mcp_config.get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    return key


def test_encrypt_decrypt_round_trip(_with_key: str) -> None:
    cipher = crypto.TenantCredentialCipher()
    ciphertext, key_version = cipher.encrypt("super-secret-refresh-token")
    assert cipher.decrypt(ciphertext, key_version) == "super-secret-refresh-token"


def test_key_version_is_one(_with_key: str) -> None:
    cipher = crypto.TenantCredentialCipher()
    _, key_version = cipher.encrypt("anything")
    assert key_version == 1


def test_missing_key_raises_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIES_ENCRYPTION_KEY", "")
    mcp_config.get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    with pytest.raises(crypto.CryptoNotConfiguredError):
        crypto.TenantCredentialCipher()


def test_tampered_ciphertext_raises_decrypt_error(_with_key: str) -> None:
    cipher = crypto.TenantCredentialCipher()
    ciphertext, key_version = cipher.encrypt("secret")
    # Replace the trailing base64 chars: still valid base64, but the HMAC fails.
    tampered = ciphertext[:-4] + b"AAAA"
    with pytest.raises(crypto.CryptoDecryptError):
        cipher.decrypt(tampered, key_version)


def test_unknown_key_version_raises_decrypt_error(_with_key: str) -> None:
    cipher = crypto.TenantCredentialCipher()
    ciphertext, _ = cipher.encrypt("secret")
    with pytest.raises(crypto.CryptoDecryptError):
        cipher.decrypt(ciphertext, 2)


def test_get_cipher_round_trip(_with_key: str) -> None:
    cipher = crypto.get_cipher()
    ciphertext, key_version = cipher.encrypt("via-get-cipher")
    assert cipher.decrypt(ciphertext, key_version) == "via-get-cipher"
