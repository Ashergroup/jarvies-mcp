"""Tests for the idempotent portal schema (agents.mcp.portal_schema).

``ensure_portal_schema`` goes through ``get_conn`` for DB access; it is
monkeypatched with an in-memory fake connection so the tests run without a live
database, mirroring the fake-connection pattern in tests/test_credentials.py and
the best-effort-swallow expectations of tests/test_admin_consent.py.
"""

from __future__ import annotations

import pytest

from agents.mcp import portal_schema


class _FakeConn:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    async def execute(self, statement: str, *args) -> None:
        self._recorder.append(statement)


class _FakeConnCtx:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._recorder)

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.mark.asyncio
async def test_ensure_portal_schema_runs_all_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder: list = []
    monkeypatch.setattr(portal_schema, "get_conn", lambda: _FakeConnCtx(recorder))

    await portal_schema.ensure_portal_schema()

    assert recorder == portal_schema.PORTAL_DDL_STATEMENTS


@pytest.mark.asyncio
async def test_ensure_portal_schema_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: list = []
    monkeypatch.setattr(portal_schema, "get_conn", lambda: _FakeConnCtx(recorder))

    await portal_schema.ensure_portal_schema()
    # A second run against the same (mocked) pool must not raise.
    await portal_schema.ensure_portal_schema()

    assert len(recorder) == 2 * len(portal_schema.PORTAL_DDL_STATEMENTS)


@pytest.mark.asyncio
async def test_ensure_portal_schema_swallows_pool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(portal_schema, "get_conn", boom)
    # Best-effort: a pool/DB failure must not propagate.
    await portal_schema.ensure_portal_schema()


@pytest.mark.asyncio
async def test_ensure_portal_schema_swallows_execute_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomConn:
        async def execute(self, statement: str, *args) -> None:
            raise RuntimeError("statement failed")

    class _BoomCtx:
        async def __aenter__(self) -> _BoomConn:
            return _BoomConn()

        async def __aexit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(portal_schema, "get_conn", lambda: _BoomCtx())
    # A failing statement mid-run is swallowed too.
    await portal_schema.ensure_portal_schema()
