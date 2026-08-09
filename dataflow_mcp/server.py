"""
Server entry point for the DataFlow MCP server.

Importing :mod:`dataflow_mcp.tools` triggers the ``@mcp.tool()`` registrations
in every per-domain module (side-effect registration), then ``run()`` starts
the FastMCP server.
"""

from dataflow_mcp.core import mcp, logger

# Import all tool modules so their @mcp.tool() decorators register.
# The noqa comment keeps linters happy about the "unused" import — the
# import IS the registration mechanism.
import dataflow_mcp.tools  # noqa: F401


def run() -> None:
    """Start the DataFlow MCP server."""
    logger.info("=" * 60)
    logger.info("Starting DataFlow MCP Server (dataflow_mcp package)")
    logger.info("=" * 60)
    mcp.run()


def main() -> None:
    """Console-script entry point (see pyproject [project.scripts])."""
    run()


if __name__ == "__main__":
    run()
