"""
AI Council MCP Server

An MCP server that consults multiple AI models in parallel and synthesizes 
their responses into comprehensive answers.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("house-of-wisdom-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+source"

__author__ = "Ezzaldeen Hamdan"

from .main import main

__all__ = ["main"] 