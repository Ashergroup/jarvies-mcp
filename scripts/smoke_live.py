"""Live smoke test for Cin7 and Freshsales MCP integrations.

Runs the tool functions directly against the real APIs using credentials
loaded from .env. Bypasses the MCP HTTP server so we are testing only the
integration code, not the transport.

Usage (from the jarvies-mcp project root):
    python scripts/smoke_live.py

Safety:
    - Never prints credentials.
    - Never prints full response bodies — only counts and short error snippets.
    - Read-only calls. Skips integrations that are not configured.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# Make sure the repo root is on sys.path when invoked as `python scripts/smoke_live.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.mcp.config import get_settings  # noqa: E402
from agents.mcp.tools import cin7_tools, freshsales_tools  # noqa: E402

MAX_ERROR_SNIPPET = 500


def _fmt_args(args: dict[str, Any]) -> str:
    """Render args as kwargs string, hiding any obviously-secret-looking keys."""

    safe = {k: v for k, v in args.items() if "key" not in k.lower() and "secret" not in k.lower()}
    return ", ".join(f"{k}={v!r}" for k, v in safe.items())


def _count(result: dict[str, Any]) -> int | str:
    data = result.get("data") or {}
    for key in (
        "inventory",
        "stock_levels",
        "sales_orders",
        "purchase_orders",
        "contacts",
        "accounts",
        "deals",
        "results",
    ):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    if "count" in data:
        return data["count"]
    return "?"


def _short_error(result: dict[str, Any]) -> str:
    message = result.get("error") or "(no error message)"
    snippet = str(message)
    if len(snippet) > MAX_ERROR_SNIPPET:
        snippet = snippet[: MAX_ERROR_SNIPPET - 3] + "..."
    return snippet


async def _run_one(
    integration: str,
    tool_name: str,
    fn: Callable[..., Awaitable[dict[str, Any]]],
    kwargs: dict[str, Any],
) -> tuple[bool, dict[str, Any], float]:
    started = time.perf_counter()
    try:
        result = await fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected escape
        latency_ms = (time.perf_counter() - started) * 1000
        synthetic = {
            "source": integration.lower(),
            "status": "error",
            "data": {},
            "error": f"unexpected {exc.__class__.__name__}: {exc}",
        }
        return False, synthetic, latency_ms
    latency_ms = (time.perf_counter() - started) * 1000
    return result.get("status") == "ok", result, latency_ms


def _print_call(
    integration: str,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    latency_ms: float,
) -> None:
    args_display = _fmt_args({k: v for k, v in args.items() if k != "permissions"})
    head = f"[{integration}] {tool_name}({args_display})"
    status = result.get("status", "?")
    if status == "ok":
        print(f"{head}  ->ok  ({latency_ms:.0f} ms, items={_count(result)})")
        return
    if status == "not_configured":
        print(f"{head}  ->not_configured  ({latency_ms:.0f} ms)")
        return
    err = _short_error(result)
    print(f"{head}  ->ERROR  ({latency_ms:.0f} ms)")
    for line in err.splitlines() or [err]:
        print(f"    {line}")


async def smoke_cin7(failures: list[str]) -> tuple[int, int]:
    perms = ["finance_access"]
    sequence: list[tuple[str, Callable[..., Awaitable[dict[str, Any]]], dict[str, Any]]] = [
        ("cin7_get_inventory", cin7_tools.cin7_get_inventory, {"page_size": 5}),
        ("cin7_get_sales_orders", cin7_tools.cin7_get_sales_orders, {"page_size": 5}),
        ("cin7_get_purchase_orders", cin7_tools.cin7_get_purchase_orders, {"page_size": 5}),
        ("cin7_get_stock_levels", cin7_tools.cin7_get_stock_levels, {"page_size": 5}),
    ]
    passed = 0
    for tool_name, fn, kwargs in sequence:
        call_kwargs = {**kwargs, "permissions": perms}
        ok, result, latency = await _run_one("Cin7", tool_name, fn, call_kwargs)
        _print_call("Cin7", tool_name, kwargs, result, latency)
        if ok:
            passed += 1
        else:
            failures.append(f"Cin7 / {tool_name}: {_short_error(result)}")
    return passed, len(sequence)


async def smoke_freshsales(failures: list[str]) -> tuple[int, int]:
    perms = ["freshsales_access"]
    sequence: list[tuple[str, Callable[..., Awaitable[dict[str, Any]]], dict[str, Any]]] = [
        (
            "freshsales_get_contacts",
            freshsales_tools.freshsales_get_contacts,
            {"page": 1, "page_size": 5},
        ),
        (
            "freshsales_get_accounts",
            freshsales_tools.freshsales_get_accounts,
            {"page": 1, "page_size": 5},
        ),
        (
            "freshsales_get_deals",
            freshsales_tools.freshsales_get_deals,
            {"page": 1, "page_size": 5},
        ),
        (
            "freshsales_search",
            freshsales_tools.freshsales_search,
            {"query": "test"},
        ),
    ]
    passed = 0
    for tool_name, fn, kwargs in sequence:
        call_kwargs = {**kwargs, "permissions": perms}
        ok, result, latency = await _run_one("Freshsales", tool_name, fn, call_kwargs)
        _print_call("Freshsales", tool_name, kwargs, result, latency)
        if ok:
            passed += 1
        else:
            failures.append(f"Freshsales / {tool_name}: {_short_error(result)}")
    return passed, len(sequence)


async def main() -> int:
    settings = get_settings()

    print("=" * 60)
    print("Configuration status")
    print("=" * 60)
    print(f"Cin7:        {'configured' if settings.cin7_configured else 'not configured'}")
    print(f"Freshsales:  {'configured' if settings.freshsales_configured else 'not configured'}")
    print()

    failures: list[str] = []
    cin7_passed = cin7_total = 0
    fs_passed = fs_total = 0

    if settings.cin7_configured:
        print("=" * 60)
        print("Cin7 live smoke")
        print("=" * 60)
        cin7_passed, cin7_total = await smoke_cin7(failures)
        print()
    else:
        print("Skipping Cin7 — credentials not configured.\n")

    if settings.freshsales_configured:
        print("=" * 60)
        print("Freshsales live smoke")
        print("=" * 60)
        fs_passed, fs_total = await smoke_freshsales(failures)
        print()
    else:
        print("Skipping Freshsales — credentials not configured.\n")

    print("=" * 60)
    print("Summary")
    print("=" * 60)
    if settings.cin7_configured:
        print(f"Cin7:        {cin7_passed}/{cin7_total} passed")
    else:
        print("Cin7:        skipped (not configured)")
    if settings.freshsales_configured:
        print(f"Freshsales:  {fs_passed}/{fs_total} passed")
    else:
        print("Freshsales:  skipped (not configured)")

    if failures:
        print("\nFailures:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nAll configured integrations passed.")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
