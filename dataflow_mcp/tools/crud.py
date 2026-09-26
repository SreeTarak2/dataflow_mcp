"""Generic MongoDB CRUD MCP tools (read / create / update / delete).

Write-path guidance for AI agents (spec item 6):
  When patching existing CONTEST records, prefer the pipeline tools
  (``get_records_for_structuring`` / ``get_records_for_full_generation`` +
  their submit counterparts) over raw ``read_collection``/``update_document`` —
  the pipeline tools carry the field-by-field checklist and server-side
  normalisation rules. Raw CRUD is the fallback for non-pipeline work and for
  the Events collection. When you correct a scraped field, pair the patch with
  ``flag_contest_discrepancy`` so validation reports and DB state stay
  reconciled (spec item 8).
"""

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
    validate: bool = False,
) -> Dict[str, Any]:
    """
    Create a new document in a collection.

    Args:
        collection_name: Name of the collection
        document_json: JSON string representing the document to create
        validate: When True and the collection is a contest schema collection
                  ("Contests"), run write-time enum/schema validation first
                  (spec item 1). Errors block the write; notices (e.g. legacy
                  category names, non-canonical region aliases) do not.

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

        # Write-time validation (spec item 1)
        if validate:
            from dataflow_mcp.core import DEFAULT_COLLECTION
            from tools.schema_validation import (
                split_violations,
                summarize_violations,
                validate_contest_document,
            )

            if collection_name in (DEFAULT_COLLECTION, "Contests"):
                violations = validate_contest_document(document)
                errors, _notices = split_violations(violations)
                summary = summarize_violations(violations)
                if errors:
                    update_metrics(False)
                    return {
                        "success": False,
                        "error": f"Schema validation failed ({len(errors)} error(s)) — nothing was written",
                        "validation": summary,
                    }
                if summary["notice_count"]:
                    logger.info(
                        f"create_document validation notices: {summary['notice_count']}"
                    )

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
    validate: bool = False,
) -> Dict[str, Any]:
    """
    Update an existing document in a collection.

    MERGE SEMANTICS (documented contract — spec item 2):
      - Plain object: DEEP MERGE. Nested plain objects expand to dotted $set
        paths, so {"audience": {"mode": "hybrid"}} updates only
        audience.mode and PRESERVES audience.eligibilityLabel etc. Arrays and
        nulls replace the value wholesale.
      - Operator object: {"$set", "$unset", "$push", "$pull"} pass through
        to MongoDB. $unset REMOVES a field entirely (null only empties it —
        absent and null are semantically different for filters). Unsupported
        operators are rejected with a structured error, never a generic
        "Database error occurred" (spec item 5).

    Every successful response includes the updated document plus a field-level
    diff (changes: {field: {before, after}}) so writes are self-verifying — no
    extra read round-trip needed.

    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document to update
        update_json: JSON string with the fields to update (plain object or
                     $set/$unset/$push/$pull operator object)
        validate: When True and the collection is a contest schema collection
                  ("Contests"), run write-time enum/schema validation on the
                  payload first (spec item 1). Errors block the write and the
                  response lists every violation (field, value, allowed
                  values); notices do not block.

    Returns:
        Dictionary with update result, changes diff, and the updated document
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

        # Write-time validation (spec item 1) — strict mode is opt-in.
        if validate:
            from dataflow_mcp.core import DEFAULT_COLLECTION
            from tools.schema_validation import (
                split_violations,
                summarize_violations,
                validate_update_payload,
            )

            if collection_name in (DEFAULT_COLLECTION, "Contests"):
                violations = validate_update_payload(update_data)
                errors, _notices = split_violations(violations)
                summary = summarize_violations(violations)
                if errors:
                    update_metrics(False)
                    return {
                        "success": False,
                        "error": (
                            f"Schema validation failed ({len(errors)} error(s)) — "
                            "nothing was written"
                        ),
                        "validation": summary,
                        "hint": (
                            "Fix the listed fields (each violation shows the value "
                            "and the allowed values), then resubmit. Re-run with "
                            "validate=false to skip this check."
                        ),
                    }

        result = DataManager.update_document(
            collection_name, document_id, update_data, return_document=True
        )
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in update_document: {e}")
        update_metrics(False)
        return {"success": False, "error": f"Unexpected error: {type(e).__name__}"}


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
