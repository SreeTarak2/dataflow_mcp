"""Health & monitoring MCP tools."""

from typing import Any, Dict

from dataflow_mcp.core import mcp, logger, metrics


@mcp.tool()
def health_check() -> Dict[str, Any]:
    """
    Check the health of the MCP server process AND its MongoDB connectivity
    (merges the former database_status tool).

    Returns:
        Dictionary with server health, request metrics, and mongodb status
    """
    try:
        import time

        logger.info("Health check requested")

        uptime = time.time() - metrics["start_time"]

        health_data: Dict[str, Any] = {
            "status": "healthy",
            "uptime_seconds": round(uptime, 2),
            "metrics": {
                "total_requests": metrics["total_requests"],
                "successful_requests": metrics["successful_requests"],
                "failed_requests": metrics["failed_requests"],
                "success_rate": round(
                    (metrics["successful_requests"] / max(metrics["total_requests"], 1)) * 100,
                    2,
                ),
            },
        }

        # MongoDB connectivity (from the former database_status tool)
        try:
            from config.mongodb import ping_database

            mongo_ready, mongo_error = ping_database()
            health_data["mongodb"] = {
                "connected": mongo_ready,
                "error": mongo_error,
            }
            if not mongo_ready:
                health_data["status"] = "degraded"
        except Exception as mongo_exc:
            health_data["mongodb"] = {"connected": False, "error": str(mongo_exc)}
            health_data["status"] = "degraded"

        return health_data

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}
