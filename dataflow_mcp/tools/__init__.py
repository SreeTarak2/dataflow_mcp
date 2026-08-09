"""Per-domain MCP tool modules.

Each module imports the shared :data:`dataflow_mcp.core.mcp` instance and
registers its tools with ``@mcp.tool()``. Importing this package (see
:mod:`dataflow_mcp.server`) registers every tool on the server.
"""

from dataflow_mcp.tools import audit  # noqa: F401
from dataflow_mcp.tools import contests  # noqa: F401
from dataflow_mcp.tools import crud  # noqa: F401
from dataflow_mcp.tools import events  # noqa: F401
from dataflow_mcp.tools import health  # noqa: F401
from dataflow_mcp.tools import images  # noqa: F401
from dataflow_mcp.tools import migration  # noqa: F401
from dataflow_mcp.tools import raw_data  # noqa: F401
from dataflow_mcp.tools import validation  # noqa: F401

__all__ = [
    "audit",
    "contests",
    "crud",
    "events",
    "health",
    "images",
    "migration",
    "raw_data",
    "validation",
]
