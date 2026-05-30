import re
import logging
from typing import Any, Dict
from pymongo import ASCENDING, DESCENDING

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Custom validation error."""
    pass


class MongoDBValidator:
    """Security validation for MongoDB operations."""
    
    MAX_COLLECTION_NAME_LENGTH = 120
    MAX_FIELD_NAME_LENGTH = 255
    MAX_FILTER_SIZE = 10_000  # Max characters in filter
    
    # Blacklisted MongoDB operators for safety
    DANGEROUS_OPERATORS = {"$where", "$function", "$accumulator"}
    
    @staticmethod
    def validate_collection_name(collection_name: str) -> str:
        """
        Validate collection name to prevent injection.
        
        Restrictions:
        - Cannot start with 'system.' 
        - Cannot contain null bytes
        - Max 120 characters
        - Only alphanumeric, dash, underscore allowed
        """
        if not collection_name:
            raise ValidationError("Collection name cannot be empty")
        
        if len(collection_name) > MongoDBValidator.MAX_COLLECTION_NAME_LENGTH:
            raise ValidationError(
                f"Collection name exceeds max length of {MongoDBValidator.MAX_COLLECTION_NAME_LENGTH}"
            )
        
        if collection_name.startswith("system."):
            raise ValidationError("Cannot access system collections")
        
        if "\x00" in collection_name:
            raise ValidationError("Collection name cannot contain null bytes")
        
        if not re.match(r"^[a-zA-Z0-9_-]+$", collection_name):
            raise ValidationError(
                "Collection name can only contain alphanumeric characters, dash, and underscore"
            )
        
        return collection_name
    
    @staticmethod
    def validate_field_name(field_name: str) -> str:
        """Validate field names to prevent injection."""
        if not field_name:
            raise ValidationError("Field name cannot be empty")
        
        if len(field_name) > MongoDBValidator.MAX_FIELD_NAME_LENGTH:
            raise ValidationError(f"Field name exceeds max length of {MongoDBValidator.MAX_FIELD_NAME_LENGTH}")
        
        if "\x00" in field_name:
            raise ValidationError("Field name cannot contain null bytes")
        
        if field_name.startswith("$") and field_name not in ["$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$and", "$or"]:
            raise ValidationError(f"Using operator {field_name} is not allowed")
        
        return field_name
    
    @staticmethod
    def validate_filter(filter_dict: Dict[str, Any] | None) -> Dict[str, Any] | None:
        """
        Validate filter to prevent injection and dangerous operations.
        """
        if filter_dict is None:
            return {}
        
        if not isinstance(filter_dict, dict):
            raise ValidationError("Filter must be a dictionary")
        
        # Check for dangerous operators
        filter_str = str(filter_dict)
        for op in MongoDBValidator.DANGEROUS_OPERATORS:
            if op in filter_dict or op in filter_str:
                raise ValidationError(f"Operator {op} is not allowed for security reasons")
        
        if len(filter_str) > MongoDBValidator.MAX_FILTER_SIZE:
            raise ValidationError(f"Filter is too large (max {MongoDBValidator.MAX_FILTER_SIZE} characters)")
        
        return filter_dict
    
    @staticmethod
    def validate_document(document: Dict[str, Any]) -> Dict[str, Any]:
        """Validate document before insert/update."""
        if not isinstance(document, dict):
            raise ValidationError("Document must be a dictionary")
        
        if not document:
            raise ValidationError("Document cannot be empty")
        
        if "_id" in document and document["_id"] is None:
            raise ValidationError("Document _id cannot be None")
        
        # Check document size (MongoDB limit is 16MB)
        doc_str = str(document)
        if len(doc_str) > 1_000_000:  # 1MB soft limit
            raise ValidationError("Document is too large (max 1MB)")
        
        return document
    
    @staticmethod
    def validate_sort_field(field_name: str, direction: int = ASCENDING) -> tuple:
        """Validate sort field and direction."""
        MongoDBValidator.validate_field_name(field_name)
        
        if direction not in [ASCENDING, DESCENDING, -1, 1]:
            raise ValidationError(f"Invalid sort direction: {direction}")
        
        return (field_name, direction)


class RateLimiter:
    """Simple in-memory rate limiter."""
    
    def __init__(self, max_requests: int = 100, time_window: int = 60):
        """
        Initialize rate limiter.
        
        Args:
            max_requests: Maximum requests allowed
            time_window: Time window in seconds
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = {}
    
    def is_allowed(self, client_id: str) -> bool:
        """Check if request is allowed for client."""
        import time
        current_time = time.time()
        
        if client_id not in self.requests:
            self.requests[client_id] = []
        
        # Remove old requests outside time window
        self.requests[client_id] = [
            req_time for req_time in self.requests[client_id]
            if current_time - req_time < self.time_window
        ]
        
        if len(self.requests[client_id]) >= self.max_requests:
            return False
        
        self.requests[client_id].append(current_time)
        return True
