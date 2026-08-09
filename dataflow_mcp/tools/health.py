"""Health & monitoring MCP tools."""

from typing import Any, Dict

from dataflow_mcp.core import mcp, logger, metrics


@mcp.tool()
def health_check() -> Dict[str, Any]:
    """
    Check the health status of the MCP server process.

    Returns:
        Dictionary with health status and metrics
    """
    try:
        import time

        logger.info("Health check requested")

        uptime = time.time() - metrics["start_time"]

        health_data = {
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

        return health_data

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


@mcp.tool()
def database_status() -> Dict[str, Any]:
    """
    Check MongoDB connectivity separately from server health.

    Returns:
        Database connection status and any connection error message
    """
    try:
        logger.info("Database status requested")

        from config.mongodb import ping_database

        mongo_ready, mongo_error = ping_database()
        return {
            "status": "healthy" if mongo_ready else "degraded",
            "mongodb": {
                "connected": mongo_ready,
                "error": mongo_error,
            },
        }

    except Exception as e:
        logger.error(f"Database status failed: {e}")
        return {"status": "unhealthy", "error": str(e)}
