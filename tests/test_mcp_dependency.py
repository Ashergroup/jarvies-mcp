"""Regression cover for the mcp runtime dependency.

Task def 35 crash-looped on 2026-07-31: the image built cleanly and the whole
suite passed, but the container could not start because mcp had resolved to
2.0.0, which removed ``mcp.server.fastmcp``. Nothing asserted that the module
the server imports at startup is actually importable, so the failure only
surfaced in ECS.

These tests fail in the environment the server runs in, not just in CI, so a
future unpinned upgrade is caught at test time instead of at rollout.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

MIN_VERSION = (1, 9, 0)
MAX_MAJOR = 2


def _release(raw: str) -> tuple[int, int, int]:
    """Return the (major, minor, patch) release tuple, ignoring any suffix."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if match is None:
        pytest.fail(f"cannot parse mcp version {raw!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def test_fastmcp_module_importable() -> None:
    """The exact import agents.mcp.server performs at startup."""
    from mcp.server.fastmcp import FastMCP

    assert FastMCP is not None


def test_installed_mcp_version_within_supported_range() -> None:
    raw = version("mcp")
    release = _release(raw)
    assert release >= MIN_VERSION, f"mcp {raw} is older than 1.9.0"
    assert release[0] < MAX_MAJOR, (
        f"mcp {raw} is 2.x, which removed mcp.server.fastmcp; "
        "the server cannot start against it"
    )


def test_pyproject_pins_mcp_below_2() -> None:
    """Guard the declared constraint, not just the resolved one.

    A passing version assertion means little if the pin that produced it can be
    loosened without anything noticing.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = declared["project"]["dependencies"]
    specs = [dep for dep in deps if re.match(r"^mcp(\[|>|=|<|!|~|$)", dep)]
    assert len(specs) == 1, f"expected exactly one mcp requirement, found {specs}"
    assert "<2" in specs[0], f"mcp requirement {specs[0]!r} is missing an upper bound"
