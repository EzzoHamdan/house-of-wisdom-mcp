"""
AI Council MCP Server

An MCP server that consults multiple AI model families in parallel and returns
each one's complete, independent analysis. There is no synthesizer — the
orchestrator (the MCP client) weighs the perspectives itself.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("house-of-wisdom-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+source"

__author__ = "Ezzaldeen Hamdan"

from .main import main

__all__ = ["main"] 