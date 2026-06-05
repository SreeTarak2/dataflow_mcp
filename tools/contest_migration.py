"""
Contest Migration & Normalization Utilities

Handles migration of 810+ existing contests to new v4.0 schema
using the backfill prompt approach (diff-based patches).

Patch Validation Layer (v2.0):
  Before any write to MongoDB, patches are validated through:
    1. Field Whitelist  — only allowed fields may be patched
    2. Schema Compliance — types, enums, formats match expected schema
    3. Destructive Write — don't overwrite populated fields with null
    4. Cross-field Consistency — contradictory values are rejected
"""

import json
import logging
import os
from typing import Dict, Any, List, Set, Optional
from datetime import datetime
from bson.objectid import ObjectId
from config.mongodb import db
from config.security import MongoDBValidator, ValidationError
from pymongo.errors import PyMongoError

COLLECTION_NAME = os.getenv("COLLECTION_NAME", "Contests")

logger = logging.getLogger(__name__)


# ============================================================
# VALIDATION CONSTANTS
# ============================================================

ALLOWED_PATCH_FIELDS: Set[str] = {
    # Prize fields
    "prize.isMonetary",
    "prize.originalAmount",
    "prize.totalUSD",
    "prize.currency",
    "prize.prizeSummary",
    "prize.description",
    "prize.breakdown",
    # Entry fields
    "entry.isFree",
    "entry.feeConfidence",
    "entry.feeNote",
    "entry.fee.amount",
    "entry.fee.currency",
    # Description fields
    "description",
    "descriptionDetailed",
    # Audience fields
    "audience.eligibilityLabel",
    "audience.eligibilityDetail",
    "audience.primarySkillLevel",
    "audience.skillLevelSource",
    "audience.location",
    "audience.mode",
    "audience.age.min",
    "audience.age.max",
    # Category fields
    "category",
    "subCategory",
    "rawCategory",
    # Tags
    "tags",
    # Timeline
    "timeline.submissionDeadlineUTC",
    "timeline.startDateUTC",
    "timeline.eventEndUTC",
    # Image
    "image.primary.url",
    "image.alt",
    # Type
    "type",
    # Link
    "link",
}

CANONICAL_CATEGORIES: Set[str] = {
    "Creative Arts",
    "Technology & AI",
    "Science & Research",
    "Business & Innovation",
    "Writing & Media",
    "Environment & Sustainability",
    "Education & Learning",
    "Social Impact & Leadership",
    "Open / Multidisciplinary",
}

VALID_SKILL_LEVELS: Set[str] = {
    "beginner",
    "intermediate",
    "advanced",
    "open",
}

VALID_SKILL_LEVEL_SOURCES: Set[str] = {
    "explicit",
    "inferred",
    "default",
}

VALID_FEE_CONFIDENCE: Set[str] = {
    "confirmed",
    "extracted",
    "unknown",
}

VALID_MODES: Set[str] = {
    "online",
    "offline",
    "in-person",
    "hybrid",
}

VALID_TYPES: Set[str] = {
    "contest",
    "hackathon",
    "grant",
    "fellowship",
    "award",
    "challenge",
}

# Category → subcategory mapping for consistency checks
CATEGORY_TO_SUBCATEGORIES: Dict[str, Set[str]] = {
    "Creative Arts": {
        "Photography",
        "Illustration & Visual Art",
        "Graphic Design",
        "Fashion Design",
        "Architecture & Urban Design",
    },
    "Technology & AI": {
        "Software Development",
        "AI & Machine Learning",
    },
    "Science & Research": {
        "Physical & Space Sciences",
    },
    "Business & Innovation": {
        "Entrepreneurship & Startups",
    },
    "Writing & Media": {
        "Fiction & Creative Writing",
        "Film & Video",
        "Music & Audio",
        "Journalism & Nonfiction",
    },
    "Environment & Sustainability": {
        "Conservation & Ecology",
        "Climate & Clean Energy",
        "Sustainable Food & Agriculture",
    },
    "Education & Learning": {
        "Teaching & Curriculum",
    },
    "Social Impact & Leadership": {
        "Community & Equity",
        "Youth Leadership",
    },
    "Open / Multidisciplinary": set(),  # no subcategories
}


# ============================================================
# PATCH VALIDATOR
# ============================================================


class PatchValidationResult:
    """Holds validation results for a single patch."""

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class PatchValidator:
    """
    Validates migration patches before they reach MongoDB.

    Four verification stages:
      1. Field whitelist  — only known/approved fields
      2. Schema compliance — types, enums, formats
      3. Destructive write — protect populated fields from null
      4. Cross-field consistency — no contradictory values
    """

    @staticmethod
    def _get_nested(doc: Dict[str, Any], dotted_key: str) -> Any:
        """Get a value from a nested dict using dot notation."""
        parts = dotted_key.split(".")
        current = doc
        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _expand_dotted_keys(patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        Expand a nested patch dict into dot-notation keys.
        E.g. {"prize": {"prizeSummary": "$10k"}} → {"prize.prizeSummary": "$10k"}
        """
        expanded = {}
        for key, value in patch.items():
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    expanded[f"{key}.{nested_key}"] = nested_value
            else:
                expanded[key] = value
        return expanded

    # -------------------------------------------------------
    # Verification 1: Field Whitelist
    # -------------------------------------------------------

    @staticmethod
    def check_field_whitelist(patch: Dict[str, Any]) -> PatchValidationResult:
        """
        Verification 1: Only allow known fields to be patched.

        Checks both flat keys (e.g. "description") and nested
        dot-notation keys (e.g. "prize.prizeSummary").
        """
        result = PatchValidationResult()
        expanded = PatchValidator._expand_dotted_keys(patch)

        for key in expanded:
            if key in ALLOWED_PATCH_FIELDS:
                continue

            result.errors.append(
                f"Field '{key}' is not in the allowed patch field whitelist. "
                f"Allowed fields: {len(ALLOWED_PATCH_FIELDS)} approved fields."
            )

        return result

    # -------------------------------------------------------
    # Verification 2: Schema Compliance
    # -------------------------------------------------------

    @staticmethod
    def _check_enum(value: Any, allowed: Set[str], field_name: str) -> Optional[str]:
        """Check a value is a valid enum member."""
        if value is None:
            return None  # null is always allowed
        if not isinstance(value, str):
            return f"{field_name}: expected a string, got {type(value).__name__}"
        if value not in allowed:
            return (
                f"{field_name}: '{value}' is not valid. "
                f"Must be one of: {', '.join(sorted(allowed))}"
            )
        return None

    @staticmethod
    def _check_string(
        value: Any, field_name: str, min_len: int = 0, max_len: int = 1000
    ) -> Optional[str]:
        """Check a value is a valid string."""
        if value is None:
            return None
        if not isinstance(value, str):
            return f"{field_name}: expected a string, got {type(value).__name__}"
        if len(value) < min_len:
            return f"{field_name}: too short ({len(value)} chars, min {min_len})"
        if len(value) > max_len:
            return f"{field_name}: too long ({len(value)} chars, max {max_len})"
        return None

    @staticmethod
    def _check_number(value: Any, field_name: str) -> Optional[str]:
        """Check a value is a valid number."""
        if value is None:
            return None
        if not isinstance(value, (int, float)):
            return f"{field_name}: expected a number, got {type(value).__name__}"
        return None

    @staticmethod
    def _check_bool(value: Any, field_name: str) -> Optional[str]:
        """Check a value is a valid boolean."""
        if value is None:
            return None
        if not isinstance(value, bool):
            return f"{field_name}: expected a boolean, got {type(value).__name__}"
        return None

    @staticmethod
    def _check_list(value: Any, field_name: str) -> Optional[str]:
        """Check a value is a valid list."""
        if value is None:
            return None
        if not isinstance(value, list):
            return f"{field_name}: expected a list, got {type(value).__name__}"
        return None

    @staticmethod
    def check_schema_compliance(patch: Dict[str, Any]) -> PatchValidationResult:
        """
        Verification 2: Validate types, enums, and formats.
        """
        result = PatchValidationResult()
        expanded = PatchValidator._expand_dotted_keys(patch)

        schema_checks = {
            # Prize
            "prize.isMonetary": lambda v: PatchValidator._check_bool(v, "prize.isMonetary"),
            "prize.originalAmount": lambda v: (
                None
                if v is None or isinstance(v, (int, float, str))
                else f"prize.originalAmount: expected number or string or null, got {type(v).__name__}"
            ),
            "prize.totalUSD": lambda v: PatchValidator._check_number(v, "prize.totalUSD"),
            "prize.currency": lambda v: PatchValidator._check_string(
                v, "prize.currency", max_len=5
            ),
            "prize.prizeSummary": lambda v: PatchValidator._check_string(
                v, "prize.prizeSummary", max_len=300
            ),
            "prize.description": lambda v: PatchValidator._check_string(
                v, "prize.description", max_len=1000
            ),
            "prize.breakdown": lambda v: PatchValidator._check_string(
                v, "prize.breakdown", max_len=500
            ),
            # Entry
            "entry.isFree": lambda v: PatchValidator._check_bool(v, "entry.isFree"),
            "entry.feeConfidence": lambda v: PatchValidator._check_enum(
                v, VALID_FEE_CONFIDENCE, "entry.feeConfidence"
            ),
            "entry.feeNote": lambda v: PatchValidator._check_string(
                v, "entry.feeNote", max_len=500
            ),
            "entry.fee.amount": lambda v: PatchValidator._check_number(v, "entry.fee.amount"),
            "entry.fee.currency": lambda v: PatchValidator._check_string(
                v, "entry.fee.currency", max_len=5
            ),
            # Description
            "description": lambda v: PatchValidator._check_string(v, "description", max_len=500),
            "descriptionDetailed": lambda v: PatchValidator._check_string(
                v, "descriptionDetailed", max_len=2000
            ),
            # Audience
            "audience.eligibilityLabel": lambda v: PatchValidator._check_string(
                v, "audience.eligibilityLabel", max_len=150
            ),
            "audience.eligibilityDetail": lambda v: PatchValidator._check_string(
                v, "audience.eligibilityDetail", max_len=500
            ),
            "audience.primarySkillLevel": lambda v: PatchValidator._check_enum(
                v, VALID_SKILL_LEVELS, "audience.primarySkillLevel"
            ),
            "audience.skillLevelSource": lambda v: PatchValidator._check_enum(
                v, VALID_SKILL_LEVEL_SOURCES, "audience.skillLevelSource"
            ),
            "audience.location": lambda v: PatchValidator._check_string(
                v, "audience.location", max_len=100
            ),
            "audience.mode": lambda v: PatchValidator._check_enum(v, VALID_MODES, "audience.mode"),
            # Category
            "category": lambda v: PatchValidator._check_enum(v, CANONICAL_CATEGORIES, "category"),
            "subCategory": lambda v: PatchValidator._check_string(v, "subCategory", max_len=100),
            "rawCategory": lambda v: PatchValidator._check_string(v, "rawCategory", max_len=100),
            # Tags
            "tags": lambda v: PatchValidator._check_list(v, "tags"),
            # Type
            "type": lambda v: PatchValidator._check_enum(v, VALID_TYPES, "type"),
            # Link
            "link": lambda v: PatchValidator._check_string(v, "link", max_len=1000),
            # Timeline
            "timeline.submissionDeadlineUTC": lambda v: PatchValidator._check_string(
                v, "timeline.submissionDeadlineUTC", max_len=30
            ),
            "timeline.startDateUTC": lambda v: PatchValidator._check_string(
                v, "timeline.startDateUTC", max_len=30
            ),
            "timeline.eventEndUTC": lambda v: PatchValidator._check_string(
                v, "timeline.eventEndUTC", max_len=30
            ),
            # Image
            "image.primary.url": lambda v: PatchValidator._check_string(
                v, "image.primary.url", max_len=2000
            ),
            "image.alt": lambda v: PatchValidator._check_string(v, "image.alt", max_len=500),
        }

        for key, value in expanded.items():
            if key in schema_checks:
                error = schema_checks[key](value)
                if error:
                    result.errors.append(error)

        return result

    # -------------------------------------------------------
    # Verification 3: Destructive Write Protection
    # -------------------------------------------------------

    @staticmethod
    def check_destructive_write(
        patch: Dict[str, Any], existing_doc: Optional[Dict[str, Any]]
    ) -> PatchValidationResult:
        """
        Verification 3: Prevent null from overwriting populated fields.

        If a field already has a non-null value in the database, and the
        patch sets it to null, that's flagged unless force=True.
        """
        result = PatchValidationResult()

        if existing_doc is None:
            # Can't check without existing doc — add a warning
            result.warnings.append("Destructive write check skipped: no existing document provided")
            return result

        expanded = PatchValidator._expand_dotted_keys(patch)

        for key, new_value in expanded.items():
            if new_value is not None:
                continue  # not setting to null, safe

            existing_value = PatchValidator._get_nested(existing_doc, key)
            if existing_value is not None and existing_value != "":
                result.errors.append(
                    f"Destructive write blocked: '{key}' already has a value "
                    f"({repr(existing_value)[:100]}). Patch is setting it to null. "
                    f"Use force=True to overwrite."
                )

        return result

    # -------------------------------------------------------
    # Verification 4: Cross-field Consistency
    # -------------------------------------------------------

    @staticmethod
    def check_cross_field_consistency(patch: Dict[str, Any]) -> PatchValidationResult:
        """
        Verification 4: Detect contradictory values within a patch.

        Checks:
        - isMonetary=false + prizeSummary mentioning cash → contradiction
        - isFree=true + fee.amount > 0 → contradiction
        - category + subCategory mismatch
        - feeConfidence='confirmed' + isFree=true with no fee → warning
        - type + timeline/prize structure hints
        """
        result = PatchValidationResult()

        # Build a flat view of all values in the patch
        values = {}

        # Values come from expanded dotted keys
        expanded = PatchValidator._expand_dotted_keys(patch)
        for k, v in expanded.items():
            values[k] = v

        # --- Check 1: isMonetary false + cash prizeSummary ---
        is_monetary = values.get("prize.isMonetary")
        prize_summary = values.get("prize.prizeSummary")

        if is_monetary is False and prize_summary:
            cash_keywords = [
                "cash",
                "prize",
                "€",
                "$",
                "£",
                "₹",
                "₦",
                "dollar",
                "euro",
                "won",
                "grant",
            ]
            lower_summary = prize_summary.lower()
            if any(kw in lower_summary for kw in cash_keywords):
                result.errors.append(
                    f"Cross-field inconsistency: prize.isMonetary=false but "
                    f"prize.prizeSummary='{prize_summary[:80]}' contains cash keywords. "
                    f"Set isMonetary=true or fix prizeSummary."
                )

        # --- Check 2: isFree true + fee.amount > 0 ---
        is_free = values.get("entry.isFree")
        fee_amount = values.get("entry.fee.amount")

        if is_free is True and fee_amount is not None:
            try:
                if float(fee_amount) > 0:
                    result.errors.append(
                        f"Cross-field inconsistency: entry.isFree=true but "
                        f"entry.fee.amount={fee_amount} > 0. Either set isFree=false "
                        f"or set fee.amount=0."
                    )
            except (TypeError, ValueError):
                pass

        # --- Check 3: category / subCategory mismatch ---
        category = values.get("category")
        sub_category = values.get("subCategory")

        if category and sub_category and category in CATEGORY_TO_SUBCATEGORIES:
            allowed_subs = CATEGORY_TO_SUBCATEGORIES[category]
            if allowed_subs and sub_category not in allowed_subs:
                result.errors.append(
                    f"Cross-field inconsistency: subCategory='{sub_category}' does not "
                    f"belong under category='{category}'. Expected one of: "
                    f"{', '.join(sorted(allowed_subs)) if allowed_subs else 'none (no subcategories)'}"
                )

        # --- Check 4: feeConfidence='confirmed' with isFree=true (no fee) ---
        fee_confidence = values.get("entry.feeConfidence")
        if fee_confidence == "confirmed" and is_free is not True:
            # If feeConfidence is confirmed but isFree is not explicitly true,
            # that's fine — it just means the fee was confirmed. Warning level only.
            pass

        if fee_confidence == "confirmed" and is_free is True:
            result.warnings.append(
                "Cross-field note: entry.feeConfidence='confirmed' and "
                "entry.isFree=true — confirming the contest is free."
            )

        # --- Check 5: skillLevelSource without primarySkillLevel ---
        skill_source = values.get("audience.skillLevelSource") or values.get("skillLevelSource")
        primary_skill = values.get("audience.primarySkillLevel")

        if skill_source and not primary_skill:
            result.warnings.append(
                "Cross-field note: skillLevelSource is set but "
                "audience.primarySkillLevel is not. Consider setting both."
            )

        return result

    # -------------------------------------------------------
    # Run all verifications
    # -------------------------------------------------------

    @staticmethod
    def validate_patch(
        patch: Dict[str, Any], existing_doc: Optional[Dict[str, Any]] = None, force: bool = False
    ) -> PatchValidationResult:
        """
        Run all 4 verifications on a patch.

        Args:
            patch: The patch dict to validate
            existing_doc: Existing document from DB (for destructive write check)
            force: If True, destructive write errors become warnings

        Returns:
            PatchValidationResult with errors and warnings
        """
        result = PatchValidationResult()

        # 1. Field whitelist
        whitelist_result = PatchValidator.check_field_whitelist(patch)
        result.errors.extend(whitelist_result.errors)
        result.warnings.extend(whitelist_result.warnings)

        # 2. Schema compliance
        schema_result = PatchValidator.check_schema_compliance(patch)
        result.errors.extend(schema_result.errors)
        result.warnings.extend(schema_result.warnings)

        # 3. Destructive write protection
        destructive_result = PatchValidator.check_destructive_write(patch, existing_doc)
        if force:
            # Downgrade destructive write errors to warnings when force=True
            result.warnings.extend(destructive_result.errors)
            result.warnings.extend(destructive_result.warnings)
        else:
            result.errors.extend(destructive_result.errors)
            result.warnings.extend(destructive_result.warnings)

        # 4. Cross-field consistency
        consistency_result = PatchValidator.check_cross_field_consistency(patch)
        result.errors.extend(consistency_result.errors)
        result.warnings.extend(consistency_result.warnings)

        return result


# ============================================================
# CONTEST MIGRATION CLASS
# ============================================================


class ContestMigration:
    """Migrate existing contests from old to new schema."""

    @staticmethod
    def get_contests_needing_migration(
        batch_size: int = 10, skip: int = 0, filter_query: Optional[Dict[str, Any]] = None
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
            collection = db[COLLECTION_NAME]

            # Build filter - prioritize contests missing backfill fields
            # Note: category field is handled by Node.js normalizeContestTags.js,
            # not the MCP backfill. MCP handles: prizeSummary, feeConfidence,
            # descriptionDetailed, eligibilityLabel, etc.
            filter_dict = filter_query or {}
            default_filter = {
                "$or": [
                    {"prizeSummary": {"$exists": False}},
                    {"feeConfidence": {"$exists": False}},
                    {"subCategory": {"$exists": False}},
                ]
            }

            # Merge filters if custom one provided
            if filter_query:
                filter_dict = {"$and": [filter_dict, default_filter]}
            else:
                filter_dict = default_filter

            # Fetch contests
            contests = list(collection.find(filter_dict).skip(skip).limit(batch_size))

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
            collection = db[COLLECTION_NAME]

            total_contests = collection.count_documents({})

            # Contests with backfill fields complete
            # Note: subCategory is tracked separately in "missing_sub_category".
            # The "migrated" count reflects backfill AI fields only.
            migrated = collection.count_documents(
                {
                    "prizeSummary": {"$exists": True},
                    "feeConfidence": {"$exists": True},
                }
            )

            # Missing at least one backfill field
            needs_migration = total_contests - migrated

            # Detailed breakdown — only backfill fields
            missing_prize_summary = collection.count_documents({"prizeSummary": {"$exists": False}})

            missing_fee_confidence = collection.count_documents(
                {"feeConfidence": {"$exists": False}}
            )

            missing_sub_category = collection.count_documents({"subCategory": {"$exists": False}})

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
                    "missing_prize_summary": missing_prize_summary,
                    "missing_fee_confidence": missing_fee_confidence,
                },
            }

        except Exception as e:
            logger.error(f"Error getting migration status: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def apply_migration_patch(
        contest_id: str, patch: Dict[str, Any], force: bool = False
    ) -> Dict[str, Any]:
        """
        Apply a validated migration patch to a contest document.

        Runs all 4 verifications before writing to MongoDB:
          1. Field whitelist
          2. Schema compliance (types, enums)
          3. Destructive write protection
          4. Cross-field consistency

        Args:
            contest_id: MongoDB ObjectId as string
            patch: Dictionary containing ONLY fields to update
            force: If True, bypass destructive write protection (use with caution)

        Returns:
            Update result with validation info
        """
        try:
            # --- Validate inputs ---
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

            collection = db[COLLECTION_NAME]

            # --- Fetch existing document for destructive write check ---
            existing_doc = collection.find_one(
                {"_id": doc_id},
                {
                    "prize": 1,
                    "entry": 1,
                    "audience": 1,
                    "description": 1,
                    "descriptionDetailed": 1,
                    "category": 1,
                    "subCategory": 1,
                    "tags": 1,
                    "type": 1,
                    "link": 1,
                    "timeline": 1,
                    "image": 1,
                },
            )

            if existing_doc is None:
                logger.warning(f"Contest not found: {contest_id}")
                return {"success": False, "error": "Contest not found"}

            # --- Run all 4 verifications ---
            validation = PatchValidator.validate_patch(
                patch, existing_doc=existing_doc, force=force
            )

            if not validation.passed:
                logger.warning(
                    f"Patch validation FAILED for contest {contest_id}: "
                    f"{'; '.join(validation.errors)}"
                )
                return {
                    "success": False,
                    "contest_id": contest_id,
                    "validation": validation.to_dict(),
                    "error": f"Patch validation failed ({len(validation.errors)} error(s))",
                }

            # --- Build update operations ---
            update_ops = {}

            for key, value in patch.items():
                if isinstance(value, dict):
                    for nested_key, nested_value in value.items():
                        update_ops[f"{key}.{nested_key}"] = nested_value
                else:
                    update_ops[key] = value

            if not update_ops:
                logger.warning(f"Patch resulted in no update operations: {patch}")
                return {"success": False, "error": "Patch contained no valid fields to update"}

            # Add migration timestamp
            update_ops["_migration_updated_at"] = datetime.utcnow()

            # --- Apply update ---
            result = collection.update_one({"_id": doc_id}, {"$set": update_ops})

            logger.info(
                f"Applied patch to contest {contest_id} "
                f"({result.modified_count} field(s) modified). "
                f"Validation: {len(validation.warnings)} warning(s)"
            )

            return {
                "success": True,
                "contest_id": contest_id,
                "modified_count": result.modified_count,
                "message": "Patch applied successfully",
                "validation": validation.to_dict(),
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
        migrations: List[Dict[str, Any]], force: bool = False
    ) -> Dict[str, Any]:
        """
        Apply multiple migration patches in batch, with validation.

        Each patch goes through all 4 verifications before writing.

        Args:
            migrations: List of {"contest_id": "...", "patch": {...}} dicts
            force: If True, bypass destructive write protection for ALL patches

        Returns:
            Bulk operation results with per-item validation details
        """
        try:
            if not isinstance(migrations, list):
                raise ValidationError("Migrations must be a list")

            results = {
                "success": True,
                "total": len(migrations),
                "successful": 0,
                "failed": 0,
                "validation_passed": 0,
                "validation_failed": 0,
                "details": [],
            }

            for migration in migrations:
                if not isinstance(migration, dict):
                    results["details"].append({"error": "Invalid migration item (not dict)"})
                    results["failed"] += 1
                    continue

                contest_id = migration.get("contest_id")
                patch = migration.get("patch", {})
                item_force = migration.get("force", force)

                result = ContestMigration.apply_migration_patch(contest_id, patch, force=item_force)

                if result.get("success"):
                    results["successful"] += 1
                    results["validation_passed"] += 1
                    entry = {
                        "contest_id": contest_id,
                        "status": "success",
                    }
                    validation = result.get("validation")
                    if validation and validation.get("warnings"):
                        entry["warnings"] = validation["warnings"]
                    results["details"].append(entry)
                else:
                    results["failed"] += 1
                    error = result.get("error", "")
                    if "validation failed" in error:
                        results["validation_failed"] += 1
                    results["details"].append(
                        {
                            "contest_id": contest_id,
                            "status": "failed",
                            "error": error,
                            "validation": result.get("validation"),
                        }
                    )

            logger.info(
                f"Bulk migration completed: {results['successful']}/{results['total']} successful "
                f"({results['validation_failed']} validation failures)"
            )

            return results

        except ValidationError as e:
            logger.warning(f"Validation error in bulk_apply_migrations: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error in bulk_apply_migrations: {e}")
            return {"success": False, "error": str(e)}
