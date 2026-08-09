"""Generic MongoDB CRUD MCP tools (read / create / update / delete)."""

import json
from typing import Any, Dict, Optional

from dataflow_mcp.core import mcp, logger, check_rate_limit, update_metrics
from tools.data_manager import DataManager


# ── READ ─────────────────────────────────────────────────────────────────


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


# ── CREATE ───────────────────────────────────────────────────────────────


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


# ── UPDATE ───────────────────────────────────────────────────────────────


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


# ── DELETE ───────────────────────────────────────────────────────────────


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
