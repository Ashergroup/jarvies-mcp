"""One-shot re-encryption of legacy plaintext ``tenant_credentials`` rows.

Day 2 added encryption at rest (``credential_ciphertext`` + ``key_version``) and
made new/updated rows encrypted while leaving existing plaintext ``credential_key``
rows readable. This script migrates those legacy rows: it encrypts the plaintext,
writes the ciphertext, and NULLs ``credential_key`` — never dropping the column
(a later release removes it once every environment is migrated).

Standalone by design: ``asyncpg`` + ``cryptography`` only, no ``agents.*``
imports. The Fernet key is read straight from ``JARVIES_ENCRYPTION_KEY`` and used
to build ``Fernet`` directly (matching ``agents.mcp.crypto``: urlsafe-base64 key,
UTF-8 plaintext, ``key_version = 1``), so the app read path decrypts exactly what
this writes. Runnable with only ``DATABASE_URL`` + ``JARVIES_ENCRYPTION_KEY`` set.

Manual-only. Never auto-run from server startup or scripts/migrate.py.

Usage::

    python scripts/reencrypt_credentials.py --dry-run
    python scripts/reencrypt_credentials.py --execute

Exit codes: 0 success, 1 config/connection error, 2 migration failure (rolled
back), 3 verification failure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field

import asyncpg
from cryptography.fernet import Fernet

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a dev convenience only.
    pass


# Must match agents.mcp.crypto.CURRENT_KEY_VERSION so the app read path accepts
# what this script writes.
KEY_VERSION = 1

SELECT_ALL = """
    SELECT id, tenant_id, credential_type, credential_key, credential_ciphertext, key_version
    FROM tenant_credentials
"""
UPDATE_ROW = """
    UPDATE tenant_credentials
    SET credential_ciphertext = $1, key_version = $2, credential_key = NULL,
        updated_at = now()
    WHERE id = $3
"""
COUNT_REMAINING = "SELECT count(*) FROM tenant_credentials WHERE credential_key IS NOT NULL"
SELECT_CIPHERTEXTS = (
    "SELECT id, credential_ciphertext, key_version "
    "FROM tenant_credentials WHERE credential_ciphertext IS NOT NULL"
)


class MigrationError(RuntimeError):
    """Raised when a row cannot be encrypted; aborts the whole batch."""


class VerificationError(RuntimeError):
    """Raised when a post-migration check fails."""


class _FernetCipher:
    """Thin Fernet wrapper mirroring agents.mcp.crypto's key handling.

    Kept local so the script has no ``agents.*`` dependency. Uses the same
    conventions as ``TenantCredentialCipher`` (urlsafe-base64 key, UTF-8
    plaintext) so ciphertext is interchangeable with the app read path.
    """

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode("utf-8")


@dataclass
class RowResult:
    tenant_id: str
    credential_type: str
    action: str  # "encrypt" | "anomalous" | "already_encrypted" | "empty"
    plaintext_length: int | None = None


@dataclass
class MigrationReport:
    total: int = 0
    to_encrypt: int = 0
    anomalous: int = 0
    already_encrypted: int = 0
    empty: int = 0
    updated: int = 0
    rows: list[RowResult] = field(default_factory=list)


def _encrypt_or_abort(cipher, plaintext: str, row: RowResult) -> bytes:
    try:
        return cipher.encrypt(plaintext)
    except Exception as exc:  # noqa: BLE001 - any encrypt failure aborts the batch
        raise MigrationError(
            f"encrypt failed for {row.tenant_id}/{row.credential_type}: "
            f"{exc.__class__.__name__}"
        ) from exc


async def migrate_credentials(conn, cipher, *, execute: bool) -> MigrationReport:
    """Classify every ``tenant_credentials`` row and (optionally) re-encrypt.

    Reads all rows, classifies each, and encrypts the plaintext of every row that
    still carries a ``credential_key`` (normal legacy rows and anomalous rows that
    have BOTH columns populated). Encryption happens up front: if any row fails to
    encrypt, ``MigrationError`` is raised before a single write is issued, so no
    partial state is possible. When ``execute`` is true the UPDATEs run inside one
    transaction (all-or-nothing at the DB level too). When false, nothing is
    written — this is the dry-run classification pass.
    """

    records = await conn.fetch(SELECT_ALL)
    report = MigrationReport(total=len(records))
    pending: list[tuple[object, bytes]] = []  # (row id, ciphertext)

    for record in records:
        key = record["credential_key"]
        ciphertext = record["credential_ciphertext"]
        row = RowResult(
            tenant_id=str(record["tenant_id"]),
            credential_type=record["credential_type"],
            action="",
        )
        if key is not None and ciphertext is not None:
            # Both columns populated — anomalous. Trust the plaintext, re-encrypt
            # from it, and overwrite the (possibly stale) ciphertext.
            row.action = "anomalous"
            row.plaintext_length = len(key)
            report.anomalous += 1
            pending.append((record["id"], _encrypt_or_abort(cipher, key, row)))
        elif key is not None:
            row.action = "encrypt"
            row.plaintext_length = len(key)
            report.to_encrypt += 1
            pending.append((record["id"], _encrypt_or_abort(cipher, key, row)))
        elif ciphertext is not None:
            row.action = "already_encrypted"
            report.already_encrypted += 1
        else:
            row.action = "empty"
            report.empty += 1
        report.rows.append(row)

    if execute and pending:
        async with conn.transaction():
            for row_id, ciphertext in pending:
                await conn.execute(UPDATE_ROW, ciphertext, KEY_VERSION, row_id)
                report.updated += 1

    return report


async def count_remaining_plaintext(conn) -> int:
    """Return how many rows still carry a non-NULL ``credential_key``."""

    return await conn.fetchval(COUNT_REMAINING)


async def verify_round_trip(conn, cipher) -> int:
    """Decrypt every ciphertext row; return the count. Raises on any failure."""

    records = await conn.fetch(SELECT_CIPHERTEXTS)
    verified = 0
    for record in records:
        if record["key_version"] != KEY_VERSION:
            raise VerificationError(
                f"row {record['id']} has unexpected key_version {record['key_version']}"
            )
        try:
            cipher.decrypt(record["credential_ciphertext"])
        except Exception as exc:  # noqa: BLE001 - any decrypt failure is a hard fail
            raise VerificationError(
                f"row {record['id']} failed to decrypt: {exc.__class__.__name__}"
            ) from exc
        verified += 1
    return verified


def _print_report(report: MigrationReport, *, execute: bool) -> None:
    print(f"  total rows:        {report.total}")
    print(f"  rows to encrypt:   {report.to_encrypt}")
    print(f"  anomalous (both):  {report.anomalous}")
    print(f"  already encrypted: {report.already_encrypted}")
    print(f"  empty:             {report.empty}")
    for row in report.rows:
        if row.action in ("encrypt", "anomalous"):
            # Never print the plaintext — only its length.
            print(
                f"    - {row.tenant_id} / {row.credential_type} : "
                f"plaintext_length={row.plaintext_length} [{row.action}]"
            )
    if execute:
        print(f"  rows updated:      {report.updated}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-encrypt legacy plaintext tenant_credentials rows.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and report only. No writes.",
    )
    group.add_argument(
        "--execute",
        action="store_true",
        help="Perform the migration inside a single transaction.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not args.dry_run and not args.execute:
        # Never default to writing — refuse and require an explicit mode.
        print(
            "ERROR: choose a mode explicitly.\n"
            "  python scripts/reencrypt_credentials.py --dry-run    # review, no writes\n"
            "  python scripts/reencrypt_credentials.py --execute    # perform migration"
        )
        return 1

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is not set. Export it (with sslmode=require for RDS).")
        return 1
    key = os.environ.get("JARVIES_ENCRYPTION_KEY")
    if not key:
        print("ERROR: JARVIES_ENCRYPTION_KEY is not set.")
        return 1
    try:
        cipher = _FernetCipher(key)
    except (ValueError, TypeError):
        print("ERROR: JARVIES_ENCRYPTION_KEY is not a valid Fernet key.")
        return 1

    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=15)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"ERROR: could not connect to the database: {exc.__class__.__name__}: {exc}")
        return 1

    try:
        mode = "EXECUTE" if args.execute else "DRY-RUN"
        print(f"reencrypt: mode={mode}")

        try:
            report = await migrate_credentials(conn, cipher, execute=args.execute)
        except MigrationError as exc:
            print(f"MIGRATION FAILED (rolled back, nothing written): {exc}")
            return 2

        _print_report(report, execute=args.execute)

        # Verification pass.
        remaining = await count_remaining_plaintext(conn)
        if args.execute:
            status = "PASS" if remaining == 0 else "FAIL"
            print(f"verification: plaintext rows remaining = {remaining} [{status}]")
            if remaining != 0:
                return 3
            try:
                verified = await verify_round_trip(conn, cipher)
            except VerificationError as exc:
                print(f"verification: round-trip FAILED: {exc}")
                return 3
            print(f"round-trip verified: {verified} rows")
        else:
            print(
                f"verification (dry-run): plaintext rows currently = {remaining} "
                "(not migrated — re-run with --execute)"
            )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
