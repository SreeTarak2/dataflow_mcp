"""
Thin entry point for the DataFlow MCP server.

The server has been refactored into the :mod:`dataflow_mcp` package (see
``dataflow_mcp/server.py``). This file is kept so ``python main.py`` keeps
working exactly as before.

Equivalent ways to run:
    python main.py
    python -m dataflow_mcp.server
    dataflow-mcp          (installed console script)
"""

from dataflow_mcp.server import run

if __name__ == "__main__":
    run()
