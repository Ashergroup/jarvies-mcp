"""Fernet encryption for tenant credentials at rest.

Wraps ``cryptography``'s Fernet with a fixed key-version envelope so the storage
layer records which key encrypted each row. Today there is a single key
(``JARVIES_ENCRYPTION_KEY``) and ``key_version`` is always ``1``; the parameter
exists so introducing a v2 key for rotation is a purely additive change (add the
new key to a lookup, bump ``CURRENT_KEY_VERSION``) without touching call sites.

The cipher is created lazily via ``get_cipher()`` (``functools.lru_cache``),
mirroring ``config.get_settings()``. Tests that change the key must call
``get_cipher.cache_clear()`` alongside ``get_settings.cache_clear()``.
"""

from __future__ import annotations

import functools

from cryptography.fernet import Fernet, InvalidToken

from agents.mcp.config import get_settings

# Single-key mode. Bump this (and add the key to a version->Fernet lookup) when
# introducing rotation; encrypt() then stamps the new version onto fresh rows
# while decrypt() keeps reading old ones.
CURRENT_KEY_VERSION = 1


class CryptoNotConfiguredError(RuntimeError):
    """Raised when encryption is requested but JARVIES_ENCRYPTION_KEY is unset."""


class CryptoDecryptError(ValueError):
    """Raised on invalid ciphertext or an unsupported key_version."""


class TenantCredentialCipher:
    """Encrypt/decrypt tenant credential secrets with a versioned Fernet key.

    The key is sourced from ``settings.jarvies_encryption_key`` at instance
    creation. An empty key raises ``CryptoNotConfiguredError`` so callers fail
    loudly rather than storing plaintext.
    """

    def __init__(self) -> None:
        key = get_settings().jarvies_encryption_key
        if not key:
            raise CryptoNotConfiguredError("JARVIES_ENCRYPTION_KEY is not configured")
        # Fernet accepts the urlsafe-base64 key as bytes.
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> tuple[bytes, int]:
        """Return ``(ciphertext, key_version)`` for a plaintext secret."""

        ciphertext = self._fernet.encrypt(plaintext.encode("utf-8"))
        return ciphertext, CURRENT_KEY_VERSION

    def decrypt(self, ciphertext: bytes, key_version: int) -> str:
        """Return the plaintext for a ciphertext produced by ``encrypt``.

        Raises ``CryptoDecryptError`` when the ciphertext is invalid/tampered or
        the ``key_version`` is not one this cipher can decrypt.
        """

        if key_version != CURRENT_KEY_VERSION:
            raise CryptoDecryptError(f"unsupported key_version: {key_version}")
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoDecryptError("invalid or tampered ciphertext") from exc


@functools.lru_cache
def get_cipher() -> TenantCredentialCipher:
    """Return the process-wide cipher, creating it on first use."""

    return TenantCredentialCipher()
