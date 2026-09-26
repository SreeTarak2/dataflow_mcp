import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from bson.objectid import ObjectId
from pymongo.errors import PyMongoError
from config.mongodb import db, get_raw_db
from config.security import MongoDBValidator, ValidationError
from .tag_normalizer import normalize_tags_array

logger = logging.getLogger(__name__)


class DataManager:
    """Secure data management with MongoDB."""

    DEFAULT_LIMIT = 100
    MAX_LIMIT = 1000

    # Update operators accepted by update_document (spec item 5).
    # Anything else in operator position is rejected with a structured error
    # instead of surfacing as a generic "Database error occurred".
    ALLOWED_UPDATE_OPERATORS = {"$set", "$unset", "$push", "$pull"}

    @staticmethod
    def _coerce_objectid_strings(obj: Any) -> Any:
        """Recursively convert 24-hex-char strings to ObjectId in filter values.

        This bridges the gap between:
        - ``save()`` which stores ``contestId`` as ``ObjectId``
        - AI callers which pass ``contestId`` as a JSON string via
          ``read_collection`` (JSON has no ObjectId type).

        Only converts strings that are exactly 24 hex characters and are
        not nested inside ``$regex``-style operator dicts.
        """
        if isinstance(obj, str) and len(obj) == 24:
            try:
                return ObjectId(obj)
            except Exception:
                return obj
        if isinstance(obj, dict):
            return {k: DataManager._coerce_objectid_strings(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [DataManager._coerce_objectid_strings(item) for item in obj]
        return obj

    @staticmethod
    def _make_json_safe(obj: Any) -> Any:
        """Recursively convert MongoDB types into JSON-serializable values.

        ObjectId -> str, datetime -> ISO string, Decimal128 -> str.
        Required because MCP tools return structured output, and any
        non-JSON-native value in the payload (e.g. an ObjectId stored in a
        ``contestId`` field) makes serialization fail with an
        "outputSchema defined but no structured output returned" error.
        """
        if isinstance(obj, dict):
            return {key: DataManager._make_json_safe(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [DataManager._make_json_safe(item) for item in obj]
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        try:
            from bson.decimal128 import Decimal128

            if isinstance(obj, Decimal128):
                return str(obj)
        except Exception:  # pragma: no cover - bson always available here
            pass
        return obj

    @staticmethod
    def read_data(
        collection_name: str,
        filter_query: Optional[Dict[str, Any]] = None,
        limit: int = DEFAULT_LIMIT,
        skip: int = 0,
        sort_by: Optional[str] = None,
        sort_direction: int = 1,
    ) -> Dict[str, Any]:
        """
        Read data from MongoDB collection with security validation.

        Args:
            collection_name: Collection to read from
            filter_query: MongoDB filter query
            limit: Maximum documents to return
            skip: Number of documents to skip
            sort_by: Field to sort by
            sort_direction: 1 for ascending, -1 for descending

        Returns:
            Dictionary with data and metadata
        """
        try:
            # Validate inputs
            collection_name = MongoDBValidator.validate_collection_name(collection_name)
            filter_query = MongoDBValidator.validate_filter(filter_query or {})

            # Auto-convert ObjectId-looking strings so that queries from AI
            # callers (which pass JSON strings) match documents stored with
            # native ObjectId values.
            filter_query = DataManager._coerce_objectid_strings(filter_query)

            # Enforce limits
            limit = min(int(limit), DataManager.MAX_LIMIT)
            skip = int(skip)

            if skip < 0:
                skip = 0
            if limit < 1:
                limit = 1

            collection = db[collection_name]

            # Build query
            query = collection.find(filter_query).skip(skip).limit(limit)

            # Add sorting if specified
            if sort_by:
                sort_by = MongoDBValidator.validate_field_name(sort_by)
                query = query.sort(sort_by, sort_direction)

            # Execute query
            data = list(query)
            total_count = collection.count_documents(filter_query)

            # Convert MongoDB types (ObjectId, datetime, ...) to JSON-safe values
            data = DataManager._make_json_safe(data)

            logger.info(f"Read {len(data)} documents from {collection_name}")

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "total": total_count,
                "skip": skip,
                "limit": limit,
            }

        except ValidationError as e:
            logger.warning(f"Validation error in read_documents: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in read_documents: {e}")
            return {"success": False, "error": "Database error occurred"}
        except Exception as e:
            logger.error(f"Unexpected error in read_documents: {e}")
            return {"success": False, "error": "An error occurred"}

    @staticmethod
    def read_raw_data(
        collection_name: str,
        filter_query: Optional[Dict[str, Any]] = None,
        limit: int = DEFAULT_LIMIT,
        skip: int = 0,
        sort_by: Optional[str] = None,
        sort_direction: int = 1,
    ) -> Dict[str, Any]:
        """
        Read data from the raw data database (CHrawdata) with security validation.

        Args:
            collection_name: Collection to read from
            filter_query: MongoDB filter query
            limit: Maximum documents to return
            skip: Number of documents to skip
            sort_by: Field to sort by
            sort_direction: 1 for ascending, -1 for descending

        Returns:
            Dictionary with data and metadata
        """
        try:
            collection_name = MongoDBValidator.validate_collection_name(collection_name)
            filter_query = MongoDBValidator.validate_filter(filter_query or {})

            limit = min(int(limit), DataManager.MAX_LIMIT)
            skip = int(skip)

            if skip < 0:
                skip = 0
            if limit < 1:
                limit = 1

            raw_db = get_raw_db()
            collection = raw_db[collection_name]

            query = collection.find(filter_query).skip(skip).limit(limit)

            if sort_by:
                sort_by = MongoDBValidator.validate_field_name(sort_by)
                query = query.sort(sort_by, sort_direction)

            data = list(query)
            total_count = collection.count_documents(filter_query)

            for doc in data:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])

            logger.info(f"Read {len(data)} documents from raw db {collection_name}")

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "total": total_count,
                "skip": skip,
                "limit": limit,
            }

        except ValidationError as e:
            logger.warning(f"Validation error in read_raw_data: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in read_raw_data: {e}")
            return {"success": False, "error": "Database error occurred"}
        except Exception as e:
            logger.error(f"Unexpected error in read_raw_data: {e}")
            return {"success": False, "error": "An error occurred"}

    @staticmethod
    def create_document(
        collection_name: str,
        document: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a new document in the collection.

        Args:
            collection_name: Collection to insert into
            document: Document to insert

        Returns:
            Result with inserted document ID
        """
        try:
            collection_name = MongoDBValidator.validate_collection_name(collection_name)
            document = MongoDBValidator.validate_document(document)

            # Operators are for updates, never for inserts — reject early with
            # a clear message instead of storing a literal "$set" field.
            if isinstance(document, dict):
                ops = [k for k in document if isinstance(k, str) and k.startswith("$")]
                if ops:
                    return {
                        "success": False,
                        "error": f"Update operator(s) {', '.join(ops)} are not valid in create_document",
                        "hint": (
                            "Send the plain document object. Operators like $set "
                            "belong in update_document only."
                        ),
                    }

            # Normalize tags for new documents to prevent noisy tags
            if isinstance(document, dict) and "tags" in document:
                try:
                    document["tags"] = normalize_tags_array(document.get("tags") or [])
                except Exception:
                    # Fail-safe: if normalization fails, keep original tags
                    pass

            collection = db[collection_name]
            result = collection.insert_one(document)

            logger.info(f"Created document in {collection_name}: {result.inserted_id}")

            return {
                "success": True,
                "id": str(result.inserted_id),
                "message": "Document created successfully",
            }

        except ValidationError as e:
            logger.warning(f"Validation error in create_document: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in create_document: {e}")
            return {"success": False, "error": "Database error occurred"}
        except Exception as e:
            logger.error(f"Unexpected error in create_document: {e}")
            return {"success": False, "error": "An error occurred"}

    @staticmethod
    def _expand_set_paths(obj: Any, prefix: str = "") -> Dict[str, Any]:
        """Flatten nested plain-object updates into dotted $set paths.

        This is what gives update_document DEEP-MERGE semantics:
          {"audience": {"mode": "hybrid"}}  →  {"audience.mode": "hybrid"}

        Only the named sub-fields change; sibling fields (e.g.
        audience.eligibilityLabel) are preserved. Arrays and empty dicts are
        treated as leaf values (replaced wholesale).
        """
        flat: Dict[str, Any] = {}
        if not isinstance(obj, dict):
            flat[prefix] = obj
            return flat
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value and not any(
                isinstance(k, str) and k.startswith("$") for k in value
            ):
                flat.update(DataManager._expand_set_paths(value, path))
            else:
                flat[path] = value
        return flat

    @staticmethod
    def update_document(
        collection_name: str,
        document_id: str,
        update_data: Dict[str, Any],
        return_document: bool = True,
    ) -> Dict[str, Any]:
        """
        Update an existing document with self-verifying semantics (spec items 2 & 5).

        Update shapes:
          - Plain object ``{"audience": {"mode": "hybrid"}}`` → DEEP MERGE.
            Nested objects are expanded to dotted paths, so only the named
            sub-fields change; sibling sub-fields are preserved. Arrays and
            nulls replace the value wholesale.
          - Operator object ``{"$set": {...}, "$unset": {...}, "$push":
            {...}, "$pull": {...}}`` → passed through to MongoDB. Unsupported
            operators are rejected with a structured error listing the
            operator and supported alternatives — never a generic
            "Database error occurred".

        Merge semantics summary (documented contract):
          - nested plain objects: deep-merged (dotted-path $set)
          - arrays: replaced wholesale unless edited via $push/$pull
          - ``null``: sets the field to null (use ``$unset`` to REMOVE a field
            entirely — absent and null are semantically different)

        Returns the updated document plus a field-level diff of every touched
        top-level field, so the caller can verify the write without a second
        read round-trip.

        Args:
            collection_name: Collection containing the document
            document_id: ID of the document to update
            update_data: Fields to update (plain object) or MongoDB update
                operators ($set/$unset/$push/$pull)
            return_document: When True (default), the response includes the
                full updated document and a per-field diff

        Returns:
            Update result with document, diff, and validation info
        """
        try:
            collection_name = MongoDBValidator.validate_collection_name(collection_name)
            update_data = MongoDBValidator.validate_document(update_data)

            # Normalize tags when documents are updated
            if isinstance(update_data, dict) and "tags" in update_data:
                try:
                    update_data["tags"] = normalize_tags_array(update_data.get("tags") or [])
                except Exception:
                    pass

            # Validate and convert document ID
            try:
                doc_id = ObjectId(document_id)
            except Exception:
                raise ValidationError("Invalid document ID format")

            collection = db[collection_name]

            # ── Determine the update shape ──
            is_operator_form = bool(update_data) and all(
                isinstance(k, str) and k.startswith("$") for k in update_data
            )

            if is_operator_form:
                unsupported = [k for k in update_data if k not in DataManager.ALLOWED_UPDATE_OPERATORS]
                if unsupported:
                    logger.warning(
                        f"Rejected unsupported update operator(s) {unsupported} "
                        f"for {document_id} in {collection_name}"
                    )
                    return {
                        "success": False,
                        "error": (
                            f"Unsupported update operator(s): {', '.join(unsupported)}"
                        ),
                        "operator_attempted": unsupported,
                        "supported_operators": sorted(DataManager.ALLOWED_UPDATE_OPERATORS),
                        "hint": (
                            "Use $set for field updates, $unset to remove fields, "
                            "$push/$pull for array edits — or send a plain "
                            "{field: value} object, which is treated as a deep-merge patch."
                        ),
                    }

                update_op = update_data
                # Normalize tags inside $set too
                set_stage = update_op.get("$set")
                if isinstance(set_stage, dict) and "tags" in set_stage:
                    try:
                        set_stage["tags"] = normalize_tags_array(set_stage.get("tags") or [])
                    except Exception:
                        pass
            else:
                # Plain object → deep merge via dotted-path expansion
                update_op = {"$set": DataManager._expand_set_paths(update_data)}

            # ── Touched top-level fields (for the projection + diff) ──
            touched_top_fields: set = set()
            for op_stage in update_op.values():
                if isinstance(op_stage, dict):
                    for key in op_stage:
                        touched_top_fields.add(key.split(".")[0])
            touched_top_fields.discard("_id")

            projection = {"_id": 1}
            for field in touched_top_fields:
                projection[field] = 1

            before_doc = collection.find_one({"_id": doc_id}, projection) if return_document else None

            if before_doc is None:
                logger.warning(f"Document not found: {document_id}")
                return {"success": False, "error": "Document not found"}

            # ── Apply the update ──
            try:
                result = collection.update_one({"_id": doc_id}, update_op)
            except PyMongoError as db_err:
                details = getattr(db_err, "details", None) or {}
                logger.error(
                    f"MongoDB error in update_document ({type(db_err).__name__}): {db_err}"
                )
                return {
                    "success": False,
                    "error": f"MongoDB error ({type(db_err).__name__}): {str(db_err)[:300]}",
                    "error_code": getattr(db_err, "code", None),
                    "details": details if isinstance(details, dict) else {},
                    "operator_attempted": sorted(update_op.keys()),
                }

            if result.matched_count == 0:
                logger.warning(f"Document not found: {document_id}")
                return {"success": False, "error": "Document not found"}

            logger.info(f"Updated document in {collection_name}: {document_id}")

            response: Dict[str, Any] = {
                "success": True,
                "message": "Document updated successfully",
                "modified_count": result.modified_count,
                "matched_count": result.matched_count,
                "update_semantics": (
                    "operator passthrough ($set/$unset/$push/$pull)"
                    if is_operator_form
                    else "deep merge (plain object expanded to dotted $set paths)"
                ),
            }

            if return_document:
                after_doc = collection.find_one({"_id": doc_id}, projection)
                changes: Dict[str, Any] = {}
                for field in sorted(touched_top_fields):
                    before_value = DataManager._make_json_safe(before_doc.get(field))
                    after_value = DataManager._make_json_safe((after_doc or {}).get(field))
                    if before_value != after_value:
                        changes[field] = {"before": before_value, "after": after_value}
                response["changes"] = changes
                if after_doc is not None:
                    response["document"] = DataManager._make_json_safe(
                        collection.find_one({"_id": doc_id})
                    )

            return response

        except ValidationError as e:
            logger.warning(f"Validation error in update_document: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in update_document: {e}")
            return {
                "success": False,
                "error": f"MongoDB error ({type(e).__name__}): {str(e)[:300]}",
                "error_code": getattr(e, "code", None),
            }
        except Exception as e:
            logger.error(f"Unexpected error in update_document: {e}")
            return {"success": False, "error": f"Unexpected error: {type(e).__name__}"}

    @staticmethod
    def delete_document(
        collection_name: str,
        document_id: str,
    ) -> Dict[str, Any]:
        """
        Delete a document from the collection.

        Args:
            collection_name: Collection containing the document
            document_id: ID of the document to delete

        Returns:
            Deletion result
        """
        try:
            collection_name = MongoDBValidator.validate_collection_name(collection_name)

            # Validate and convert document ID
            try:
                doc_id = ObjectId(document_id)
            except Exception:
                raise ValidationError("Invalid document ID format")

            collection = db[collection_name]
            result = collection.delete_one({"_id": doc_id})

            if result.deleted_count == 0:
                logger.warning(f"Document not found for deletion: {document_id}")
                return {"success": False, "error": "Document not found"}

            logger.info(f"Deleted document from {collection_name}: {document_id}")

            return {
                "success": True,
                "message": "Document deleted successfully",
            }

        except ValidationError as e:
            logger.warning(f"Validation error in delete_document: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in delete_document: {e}")
            return {"success": False, "error": "Database error occurred"}
        except Exception as e:
            logger.error(f"Unexpected error in delete_document: {e}")
            return {"success": False, "error": "An error occurred"}

    @staticmethod
    def get_document(
        collection_name: str,
        document_id: str,
    ) -> Dict[str, Any]:
        """
        Get a single document by ID.

        Args:
            collection_name: Collection containing the document
            document_id: ID of the document to retrieve

        Returns:
            Document data or error
        """
        try:
            collection_name = MongoDBValidator.validate_collection_name(collection_name)

            # Validate and convert document ID
            try:
                doc_id = ObjectId(document_id)
            except Exception:
                raise ValidationError("Invalid document ID format")

            collection = db[collection_name]
            document = collection.find_one({"_id": doc_id})

            if not document:
                logger.warning(f"Document not found: {document_id}")
                return {"success": False, "error": "Document not found"}

            # Convert MongoDB types (ObjectId, datetime, ...) to JSON-safe values
            document = DataManager._make_json_safe(document)

            logger.info(f"Retrieved document from {collection_name}: {document_id}")

            return {
                "success": True,
                "data": document,
            }

        except ValidationError as e:
            logger.warning(f"Validation error in get_document: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in get_document: {e}")
            return {"success": False, "error": "Database error occurred"}
        except Exception as e:
            logger.error(f"Unexpected error in get_document: {e}")
            return {"success": False, "error": "An error occurred"}


# Keep backward compatibility
def read_data(collection_name):
    """Read data from a MongoDB collection (legacy function)."""
    result = DataManager.read_data(collection_name)
    return result.get("data", [])


def read_documents(collection_name):
    """Read documents from a MongoDB collection."""
    return DataManager.read_data(collection_name)
