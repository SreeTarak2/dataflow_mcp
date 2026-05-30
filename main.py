import json
import logging
import time
from typing import Optional, Any, Dict, List
from fastmcp import FastMCP
from config.logging_config import get_logger
from config.security import RateLimiter, ValidationError
from tools.data_manager import DataManager
from tools.contest_migration import ContestMigration

logger = get_logger(__name__)

# Initialize MCP server
mcp = FastMCP("DataFlow MCP Server")

# Rate limiter (100 requests per 60 seconds)
rate_limiter = RateLimiter(max_requests=100, time_window=60)

# Metrics
metrics = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "start_time": time.time(),
}


def check_rate_limit(client_id: str = "default") -> bool:
    """Check if request is allowed by rate limiter."""
    if not rate_limiter.is_allowed(client_id):
        logger.warning(f"Rate limit exceeded for client: {client_id}")
        return False
    return True


def update_metrics(success: bool):
    """Update request metrics."""
    metrics["total_requests"] += 1
    if success:
        metrics["successful_requests"] += 1
    else:
        metrics["failed_requests"] += 1


# ==================== HEALTH CHECK ====================

@mcp.tool()
def health_check() -> Dict[str, Any]:
    """
    Check the health status of the MCP server and MongoDB connection.
    
    Returns:
        Dictionary with health status and metrics
    """
    try:
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
                    (
                        metrics["successful_requests"]
                        / max(metrics["total_requests"], 1)
                    )
                    * 100,
                    2,
                ),
            },
        }
        
        return health_data
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


# ==================== READ OPERATIONS ====================

@mcp.tool()
def read_collection(
    collection_name: str,
    filter_query: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    sort_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read documents from a MongoDB collection with filtering and pagination.
    
    Args:
        collection_name: Name of the collection to read from
        filter_query: JSON string with MongoDB filter query (optional)
        limit: Maximum number of documents to return (max 1000)
        skip: Number of documents to skip for pagination
        sort_by: Field name to sort by (optional)
    
    Returns:
        Dictionary containing the documents and metadata
    """
    client_id = "read_collection"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)  # Assume failure, update if success
        
        logger.info(f"Reading from collection: {collection_name}")
        
        # Parse JSON filter query if provided
        filter_dict = None
        if filter_query:
            try:
                filter_dict = json.loads(filter_query)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON filter: {filter_query}")
                return {"success": False, "error": "Invalid JSON in filter_query"}
        
        # Call data manager
        result = DataManager.read_data(
            collection_name=collection_name,
            filter_query=filter_dict,
            limit=limit,
            skip=skip,
            sort_by=sort_by,
        )
        
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in read_collection: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_document(
    collection_name: str,
    document_id: str,
) -> Dict[str, Any]:
    """
    Get a single document by ID.
    
    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document
    
    Returns:
        Dictionary containing the document data
    """
    client_id = "get_document"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info(f"Getting document {document_id} from {collection_name}")
        
        result = DataManager.get_document(collection_name, document_id)
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in get_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== CREATE OPERATIONS ====================

@mcp.tool()
def create_document(
    collection_name: str,
    document_json: str,
) -> Dict[str, Any]:
    """
    Create a new document in a collection.
    
    Args:
        collection_name: Name of the collection
        document_json: JSON string representing the document to create
    
    Returns:
        Dictionary with the ID of the created document
    """
    client_id = "create_document"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info(f"Creating document in {collection_name}")
        
        # Parse JSON document
        try:
            document = json.loads(document_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON document: {document_json}")
            return {"success": False, "error": "Invalid JSON in document_json"}
        
        # Remove _id if present (let MongoDB generate it)
        document.pop("_id", None)
        
        result = DataManager.create_document(collection_name, document)
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in create_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== UPDATE OPERATIONS ====================

@mcp.tool()
def update_document(
    collection_name: str,
    document_id: str,
    update_json: str,
) -> Dict[str, Any]:
    """
    Update an existing document in a collection.
    
    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document to update
        update_json: JSON string with the fields to update
    
    Returns:
        Dictionary with update result
    """
    client_id = "update_document"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info(f"Updating document {document_id} in {collection_name}")
        
        # Parse JSON update data
        try:
            update_data = json.loads(update_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON update: {update_json}")
            return {"success": False, "error": "Invalid JSON in update_json"}
        
        result = DataManager.update_document(collection_name, document_id, update_data)
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in update_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== DELETE OPERATIONS ====================

@mcp.tool()
def delete_document(
    collection_name: str,
    document_id: str,
) -> Dict[str, Any]:
    """
    Delete a document from a collection.
    
    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document to delete
    
    Returns:
        Dictionary with deletion result
    """
    client_id = "delete_document"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info(f"Deleting document {document_id} from {collection_name}")
        
        result = DataManager.delete_document(collection_name, document_id)
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in delete_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}
# ==================== MIGRATION OPERATIONS ====================

@mcp.tool()
def get_migration_status() -> Dict[str, Any]:
    """
    Get overall migration progress statistics for the 810 contests.
    
    Shows how many contests have been migrated to v4.0 schema,
    how many are pending, and what fields are missing.
    
    Returns:
        Dictionary with migration progress and breakdown by field
    """
    client_id = "get_migration_status"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info("Fetching migration status")
        
        result = ContestMigration.get_migration_status()
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in get_migration_status: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_contests_for_migration(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Get a batch of existing contests that need migration to v4.0 schema.
    
    Returns contests missing key fields like canonicalCategory, prizeSummary,
    or feeConfidence. Use pagination to process in batches.
    
    Args:
        batch_size: Number of contests to fetch (max 100)
        skip: Number of documents to skip for pagination
    
    Returns:
        Dictionary containing contest documents and pagination info
    """
    client_id = "get_contests_for_migration"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info(f"Fetching {batch_size} contests for migration")
        
        # Enforce limits
        batch_size = min(int(batch_size), 100)
        skip = int(skip)
        
        if batch_size < 1:
            batch_size = 10
        if skip < 0:
            skip = 0
        
        result = ContestMigration.get_contests_needing_migration(
            batch_size=batch_size,
            skip=skip
        )
        
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in get_contests_for_migration: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def apply_migration_patch(
    contest_id: str,
    patch_json: str,
) -> Dict[str, Any]:
    """
    Apply a normalized patch to update a single contest.
    
    This tool receives a JSON patch (from the backfill prompt) containing
    ONLY the fields to update. Use this after normalizing with Claude using
    the Prompts-backfill.txt prompt.
    
    Args:
        contest_id: MongoDB ObjectId of the contest (as string)
        patch_json: JSON string with fields to update (e.g. {"canonicalCategory": "Technology & AI", "prizes": {"prizeSummary": "..."}})
    
    Returns:
        Update result with modified count
    """
    client_id = "apply_migration_patch"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info(f"Applying patch to contest {contest_id}")
        
        # Parse JSON patch
        try:
            patch = json.loads(patch_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON patch: {patch_json}")
            return {"success": False, "error": "Invalid JSON in patch_json"}
        
        result = ContestMigration.apply_migration_patch(contest_id, patch)
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in apply_migration_patch: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def bulk_apply_migrations(
    migrations_json: str,
) -> Dict[str, Any]:
    """
    Apply multiple migration patches in one batch operation.
    
    Use this to process multiple contests' patches together for efficiency.
    
    Args:
        migrations_json: JSON string containing array of migrations:
            [
              {"contest_id": "...", "patch": {...}},
              {"contest_id": "...", "patch": {...}},
              ...
            ]
    
    Returns:
        Bulk operation results with successful/failed counts
    """
    client_id = "bulk_apply_migrations"
    
    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}
    
    try:
        update_metrics(False)
        
        logger.info("Starting bulk migration")
        
        # Parse JSON migrations
        try:
            migrations = json.loads(migrations_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON migrations: {migrations_json[:100]}")
            return {"success": False, "error": "Invalid JSON in migrations_json"}
        
        if not isinstance(migrations, list):
            return {"success": False, "error": "migrations_json must be an array"}
        
        result = ContestMigration.bulk_apply_migrations(migrations)
        update_metrics(result.get("success", False))
        return result
        
    except Exception as e:
        logger.error(f"Error in bulk_apply_migrations: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Starting DataFlow MCP Server")
    logger.info("=" * 60)
    mcp.run()


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Starting DataFlow MCP Server")
    logger.info("=" * 60)
    mcp.run()

