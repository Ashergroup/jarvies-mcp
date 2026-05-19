"""Agent package root for the MCP platform.

This package intentionally supports namespace-style extension so the MCP
platform can live beside, or outside, the existing `agents.m365` codebase.
"""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
