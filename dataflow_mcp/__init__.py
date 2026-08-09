"""DataFlow MCP Server — modular FastMCP package.

The MCP server was previously a single ~3,300-line ``main.py``. It is now
split into per-domain tool modules under :mod:`dataflow_mcp.tools` so AI
chatbots can discover, read, and reason about each tool more easily.

Run the server with::

    python main.py            # thin entry point
    python -m dataflow_mcp.server
    dataflow-mcp              # installed console script
"""

__version__ = "1.0.0"
