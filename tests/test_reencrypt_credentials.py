"""Tests for the one-shot credential re-encryption script.

The script (``scripts/reencrypt_credentials.py``) is standalone and not a
package, so it is loaded by path. The core row-migration logic is factored into
``migrate_credentials(conn, cipher, *, execute)``, exercised here against an
in-memory fake connection so the tests run without a live database — mirroring
the fake-connection style in tests/test_credentials.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reencrypt_credentials.py"
_spec = importlib.util.spec_from_file_location("reencrypt_credentials", _SCRIPT)
reencrypt = importlib.util.module_from_spec(_spec)
# Register before exec so the module's dataclasses can resolve their own
# namespace (required with `from __future__ import annotations`).
sys.modules["reencrypt_credentials"] = reencrypt
_spec.loader.exec_module(reencrypt)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTxn:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeTxn:
        self._conn.tx_entered = True
        return self

    async def __aexit__(self, *exc) -> bool:
        if exc[0] is not None:
            self._conn.tx_rolled_back = True
        return False


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.executed: list[tuple] = []
        self.tx_entered = False
        self.tx_rolled_back = False

    async def fetch(self, query: str, *args):
        return self._rows

    async def execute(self, query: str, *args) -> None:
        self.executed.append((query, args))

    def transaction(self) -> _FakeTxn:
        return _FakeTxn(self)


class _FakeCipher:
    """Deterministic cipher; ``bad`` names a plaintext whose encrypt raises."""

    def __init__(self, bad: str | None = None) -> None:
        self.bad = bad

    def encrypt(self, plaintext: str) -> bytes:
        if self.bad is not None and plaintext == self.bad:
            raise RuntimeError("boom")
        return b"ct:" + plaintext.encode("utf-8")

    def decrypt(self, token: bytes) -> str:
        return token[len(b"ct:") :].decode("utf-8")


def _row(
    *,
    row_id: str,
    key: str | None,
    ciphertext: bytes | None,
    credential_type: str = "xero",
) -> dict:
    return {
        "id": row_id,
        "tenant_id": "33333333-3333-3333-3333-333333333333",
        "credential_type": credential_type,
        "credential_key": key,
        "credential_ciphertext": ciphertext,
        "key_version": None if ciphertext is None else reencrypt.KEY_VERSION,
    }


# ---------------------------------------------------------------------------
# Row classification / migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plaintext_row_encrypted_and_nulled() -> None:
    conn = _FakeConn([_row(row_id="1", key="secret", ciphertext=None)])
    report = await reencrypt.migrate_credentials(conn, _FakeCipher(), execute=True)

    assert report.to_encrypt == 1
    assert report.updated == 1
    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert "credential_key = NULL" in query
    ciphertext, key_version, row_id = args
    assert ciphertext == b"ct:secret"
    assert key_version == reencrypt.KEY_VERSION
    assert row_id == "1"


@pytest.mark.asyncio
async def test_already_encrypted_row_untouched() -> None:
    conn = _FakeConn([_row(row_id="1", key=None, ciphertext=b"existing-token")])
    report = await reencrypt.migrate_credentials(conn, _FakeCipher(), execute=True)

    assert report.already_encrypted == 1
    assert report.updated == 0
    assert conn.executed == []


@pytest.mark.asyncio
async def test_anomalous_row_reencrypted_from_plaintext() -> None:
    # Both columns set: trust plaintext, overwrite the stale ciphertext, NULL key.
    conn = _FakeConn([_row(row_id="1", key="plain", ciphertext=b"stale-token")])
    report = await reencrypt.migrate_credentials(conn, _FakeCipher(), execute=True)

    assert report.anomalous == 1
    assert report.updated == 1
    query, args = conn.executed[0]
    ciphertext, _key_version, _row_id = args
    assert ciphertext == b"ct:plain"  # from the plaintext, not the stale value
    assert "credential_key = NULL" in query


@pytest.mark.asyncio
async def test_empty_row_untouched() -> None:
    conn = _FakeConn([_row(row_id="1", key=None, ciphertext=None)])
    report = await reencrypt.migrate_credentials(conn, _FakeCipher(), execute=True)

    assert report.empty == 1
    assert report.updated == 0
    assert conn.executed == []


@pytest.mark.asyncio
async def test_bad_row_aborts_batch_no_partial_writes() -> None:
    conn = _FakeConn(
        [
            _row(row_id="1", key="ok", ciphertext=None),
            _row(row_id="2", key="BAD", ciphertext=None, credential_type="cin7"),
        ]
    )
    cipher = _FakeCipher(bad="BAD")

    with pytest.raises(reencrypt.MigrationError):
        await reencrypt.migrate_credentials(conn, cipher, execute=True)

    # Encryption happens before any write, so a failure means zero UPDATEs —
    # nothing partial to roll back.
    assert conn.executed == []


@pytest.mark.asyncio
async def test_dry_run_performs_zero_updates() -> None:
    conn = _FakeConn(
        [
            _row(row_id="1", key="secret", ciphertext=None, credential_type="xero"),
            _row(row_id="2", key="plain", ciphertext=b"stale", credential_type="cin7"),
        ]
    )
    report = await reencrypt.migrate_credentials(conn, _FakeCipher(), execute=False)

    assert report.to_encrypt == 1
    assert report.anomalous == 1
    assert report.updated == 0
    assert conn.executed == []
    assert conn.tx_entered is False
    # Report still carries plaintext lengths (never the plaintext itself).
    lengths = {r.credential_type: r.plaintext_length for r in report.rows}
    assert lengths["xero"] == len("secret")
    assert lengths["cin7"] == len("plain")


# ---------------------------------------------------------------------------
# Interop: the script's Fernet output is readable by the app read path
# ---------------------------------------------------------------------------


def test_script_ciphertext_decrypts_via_app_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.mcp import config as mcp_config
    from agents.mcp import crypto

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIES_ENCRYPTION_KEY", key)
    mcp_config.get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    try:
        token = reencrypt._FernetCipher(key).encrypt("legacy-secret")
        # The app read path (crypto.get_cipher) decrypts what the script wrote,
        # using the same key and key_version=1.
        assert (
            crypto.get_cipher().decrypt(token, reencrypt.KEY_VERSION) == "legacy-secret"
        )
    finally:
        mcp_config.get_settings.cache_clear()
        crypto.get_cipher.cache_clear()
