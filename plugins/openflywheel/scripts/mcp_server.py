#!/usr/bin/env python3
"""Compatibility launcher for the installable OpenFlywheel MCP server."""

from ofw.mcp import main, server

__all__ = ["main", "server"]


if __name__ == "__main__":
    main()
