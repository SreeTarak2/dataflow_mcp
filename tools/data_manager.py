import logging
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

            # Convert ObjectId to string for JSON serialization
            for doc in data:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])

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
    def update_document(
        collection_name: str,
        document_id: str,
        update_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Update an existing document.

        Args:
            collection_name: Collection containing the document
            document_id: ID of the document to update
            update_data: Fields to update

        Returns:
            Update result
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
            result = collection.update_one({"_id": doc_id}, {"$set": update_data})

            if result.matched_count == 0:
                logger.warning(f"Document not found: {document_id}")
                return {"success": False, "error": "Document not found"}

            logger.info(f"Updated document in {collection_name}: {document_id}")

            return {
                "success": True,
                "message": "Document updated successfully",
                "modified_count": result.modified_count,
            }

        except ValidationError as e:
            logger.warning(f"Validation error in update_document: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in update_document: {e}")
            return {"success": False, "error": "Database error occurred"}
        except Exception as e:
            logger.error(f"Unexpected error in update_document: {e}")
            return {"success": False, "error": "An error occurred"}

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

            # Convert ObjectId to string
            document["_id"] = str(document["_id"])

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
