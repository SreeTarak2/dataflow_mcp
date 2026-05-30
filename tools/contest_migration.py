"""
Contest Migration & Normalization Utilities

Handles migration of 810+ existing contests to new v4.0 schema
using the backfill prompt approach (diff-based patches).
"""

import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from config.mongodb import db
from config.security import MongoDBValidator, ValidationError
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)


class ContestMigration:
    """Migrate existing contests from old to new schema."""
    
    @staticmethod
    def get_contests_needing_migration(
        batch_size: int = 10,
        skip: int = 0,
        filter_query: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Get batch of contests ready for migration/normalization.
        
        Args:
            batch_size: Number of contests to fetch
            skip: Offset for pagination
            filter_query: Optional MongoDB filter (e.g., {"_id": {"$in": [id1, id2]}})
            
        Returns:
            Dictionary with contests and metadata
        """
        try:
            collection = db['Contests']
            
            # Build filter - prioritize contests missing key v4.0 fields
            filter_dict = filter_query or {}
            default_filter = {
                "$or": [
                    {"canonicalCategory": {"$exists": False}},
                    {"canonicalCategory": None},
                    {"prizeSummary": {"$exists": False}},
                    {"feeConfidence": {"$exists": False}},
                ]
            }
            
            # Merge filters if custom one provided
            if filter_query:
                filter_dict = {"$and": [filter_dict, default_filter]}
            else:
                filter_dict = default_filter
            
            # Fetch contests
            contests = list(
                collection.find(filter_dict)
                .skip(skip)
                .limit(batch_size)
            )
            
            # Convert ObjectIds to strings
            for contest in contests:
                contest["_id"] = str(contest["_id"])
            
            total_needing_migration = collection.count_documents(default_filter)
            
            logger.info(
                f"Fetched {len(contests)} contests needing migration "
                f"(total: {total_needing_migration})"
            )
            
            return {
                "success": True,
                "contests": contests,
                "count": len(contests),
                "total_needing_migration": total_needing_migration,
                "skip": skip,
                "batch_size": batch_size,
            }
            
        except Exception as e:
            logger.error(f"Error fetching contests for migration: {e}")
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def get_migration_status() -> Dict[str, Any]:
        """
        Get overall migration progress statistics.
        
        Returns:
            Statistics on migrated vs remaining contests
        """
        try:
            collection = db['Contests']
            
            total_contests = collection.count_documents({})
            
            # Contests with v4.0 fields
            migrated = collection.count_documents({
                "canonicalCategory": {"$exists": True, "$ne": None},
                "prizeSummary": {"$exists": True},
                "feeConfidence": {"$exists": True},
            })
            
            # Missing at least one v4.0 field
            needs_migration = total_contests - migrated
            
            # Detailed breakdown
            missing_canonical = collection.count_documents({
                "$or": [
                    {"canonicalCategory": {"$exists": False}},
                    {"canonicalCategory": None}
                ]
            })
            
            missing_prize_summary = collection.count_documents({
                "prizeSummary": {"$exists": False}
            })
            
            missing_fee_confidence = collection.count_documents({
                "feeConfidence": {"$exists": False}
            })
            
            logger.info(
                f"Migration status: {migrated}/{total_contests} migrated "
                f"({100 * migrated / total_contests:.1f}%)"
            )
            
            return {
                "success": True,
                "total_contests": total_contests,
                "migrated_count": migrated,
                "migration_percentage": round(100 * migrated / total_contests, 2),
                "needs_migration": needs_migration,
                "breakdown": {
                    "missing_canonical_category": missing_canonical,
                    "missing_prize_summary": missing_prize_summary,
                    "missing_fee_confidence": missing_fee_confidence,
                },
            }
            
        except Exception as e:
            logger.error(f"Error getting migration status: {e}")
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def apply_migration_patch(
        contest_id: str,
        patch: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Apply a normalized patch to a contest document.
        
        This performs a deep merge of only the fields provided in the patch,
        preserving all other contest data.
        
        Args:
            contest_id: MongoDB ObjectId as string
            patch: Dictionary containing ONLY fields to update (as per backfill prompt)
            
        Returns:
            Update result
        """
        try:
            from bson.objectid import ObjectId
            
            # Validate inputs
            try:
                doc_id = ObjectId(contest_id)
            except Exception:
                raise ValidationError("Invalid contest ID format")
            
            if not isinstance(patch, dict):
                raise ValidationError("Patch must be a dictionary")
            
            if not patch:
                logger.info(f"No changes needed for contest {contest_id}")
                return {
                    "success": True,
                    "message": "No changes needed",
                    "modified": False,
                }
            
            collection = db['Contests']
            
            # Build update operation with deep merge logic
            # Use $set for root level fields, but merge nested objects
            update_ops = {}
            
            for key, value in patch.items():
                if isinstance(value, dict):
                    # For nested objects, use dot notation to merge
                    for nested_key, nested_value in value.items():
                        update_ops[f"{key}.{nested_key}"] = nested_value
                else:
                    update_ops[key] = value
            
            if not update_ops:
                logger.warning(f"Patch resulted in no update operations: {patch}")
                return {
                    "success": False,
                    "error": "Patch contained no valid fields to update"
                }
            
            # Add migration timestamp
            update_ops["_migration_updated_at"] = datetime.utcnow()
            
            # Apply update
            result = collection.update_one(
                {"_id": doc_id},
                {"$set": update_ops}
            )
            
            if result.matched_count == 0:
                logger.warning(f"Contest not found: {contest_id}")
                return {"success": False, "error": "Contest not found"}
            
            logger.info(
                f"Applied patch to contest {contest_id} "
                f"({result.modified_count} field(s) modified)"
            )
            
            return {
                "success": True,
                "contest_id": contest_id,
                "modified_count": result.modified_count,
                "message": "Patch applied successfully",
            }
            
        except ValidationError as e:
            logger.warning(f"Validation error in apply_migration_patch: {e}")
            return {"success": False, "error": str(e)}
        except PyMongoError as e:
            logger.error(f"MongoDB error in apply_migration_patch: {e}")
            return {"success": False, "error": "Database error"}
        except Exception as e:
            logger.error(f"Unexpected error in apply_migration_patch: {e}")
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def bulk_apply_migrations(
        migrations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Apply multiple migration patches in batch.
        
        Args:
            migrations: List of {"contest_id": "...", "patch": {...}} dicts
            
        Returns:
            Bulk operation results
        """
        try:
            if not isinstance(migrations, list):
                raise ValidationError("Migrations must be a list")
            
            results = {
                "success": True,
                "total": len(migrations),
                "successful": 0,
                "failed": 0,
                "details": []
            }
            
            for migration in migrations:
                if not isinstance(migration, dict):
                    results["details"].append({
                        "error": "Invalid migration item (not dict)"
                    })
                    results["failed"] += 1
                    continue
                
                contest_id = migration.get("contest_id")
                patch = migration.get("patch", {})
                
                result = ContestMigration.apply_migration_patch(contest_id, patch)
                
                if result.get("success"):
                    results["successful"] += 1
                    results["details"].append({
                        "contest_id": contest_id,
                        "status": "success"
                    })
                else:
                    results["failed"] += 1
                    results["details"].append({
                        "contest_id": contest_id,
                        "status": "failed",
                        "error": result.get("error")
                    })
            
            logger.info(
                f"Bulk migration completed: {results['successful']}/{results['total']} successful"
            )
            
            return results
            
        except ValidationError as e:
            logger.warning(f"Validation error in bulk_apply_migrations: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error in bulk_apply_migrations: {e}")
            return {"success": False, "error": str(e)}
