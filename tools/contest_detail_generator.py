import json
import logging
import math
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from bson.objectid import ObjectId
from urllib.parse import urlparse

from config.mongodb import db
from config.security import ValidationError

logger = logging.getLogger(__name__)

CONTEST_DETAILS_COLLECTION = "contest_details"
CONTESTS_COLLECTION = "Contests"
VIEWS_COLLECTION = "contest_views"
TRENDING_COLLECTION = "contest_trending_scores"


class ContestDetailGenerator:
    """
    Orchestrates the contest detail generation pipeline.

    Pipeline:
      1. priority_queue() → find contests needing details, sorted by priority
      2. Mistral generates content (via separate prompt/submit flow)
      3. validate() → check output quality
      4. save() → versioned upsert into contest_details

    This tool is prompt-serving and validation-only. The LLM does its own
    web research and generation via the prompt in Prompts-contest-details.txt.
    """

    def __init__(self):
        self.details_collection = db[CONTEST_DETAILS_COLLECTION]
        self.contests_collection = db[CONTESTS_COLLECTION]
        self.views_collection = db[VIEWS_COLLECTION]
        self.trending_collection = db[TRENDING_COLLECTION]

    # ─── Priority Queue ─────────────────────────────────────────

    def get_priority_queue(self, batch_size: int = 11, skip: int = 0) -> Dict[str, Any]:
        """
        Find contests needing detail generation, sorted by priority.

        Priority formula:
          - Has active trendingUntil:   +40
          - Status is "open":           +20
          - View velocity (7-day):      up to +20 (percentile-based)
          - Freshness (createdAt):      up to +10 (linear decay over 90 days)
          - Prize value (log scale):    up to +10

        Returns:
            Dict with contests list, total count, and metadata.
        """
        try:
            now = datetime.now(timezone.utc)

            # Get contest IDs that already have completed details
            existing_ids = set()
            for doc in self.details_collection.find({}, {"contestId": 1}):
                existing_ids.add(doc["contestId"])

            pipeline = [
                {
                    "$match": {
                        "archivedAt": None,
                        "status": {"$in": ["open", "scheduled"]},
                        **({"_id": {"$nin": list(existing_ids)}} if existing_ids else {}),
                    }
                },
                {
                    "$lookup": {
                        "from": "contest_trending_scores",
                        "let": {"contestId": "$_id"},
                        "pipeline": [
                            {"$match": {"$expr": {"$eq": ["$contestId", "$$contestId"]}}},
                            {"$limit": 1},
                        ],
                        "as": "trending",
                    }
                },
                {
                    "$lookup": {
                        "from": "contest_views",
                        "let": {"contestId": "$_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {"$eq": ["$contestId", "$$contestId"]},
                                    "viewedAt": {"$gte": now - timedelta(days=7)},
                                }
                            },
                            {"$count": "views"},
                        ],
                        "as": "views",
                    }
                },
                {
                    "$addFields": {
                        "_trending_score": {
                            "$ifNull": [
                                {"$first": "$trending.finalScore"},
                                0,
                            ]
                        },
                        "_view_count": {
                            "$ifNull": [
                                {"$first": "$views.views"},
                                0,
                            ]
                        },
                        "_days_old": {
                            "$cond": {
                                "if": {"$ifNull": ["$createdAt", False]},
                                "then": {
                                    "$divide": [
                                        {"$subtract": [now, "$createdAt"]},
                                        86400000,
                                    ]
                                },
                                "else": 999,
                            }
                        },
                        "_is_trending": {
                            "$cond": {
                                "if": {
                                    "$and": [
                                        {"$ifNull": ["$trendingUntil", False]},
                                        {"$gt": ["$trendingUntil", now]},
                                    ]
                                },
                                "then": 1,
                                "else": 0,
                            }
                        },
                        "_is_open": {
                            "$cond": {
                                "if": {"$eq": ["$status", "open"]},
                                "then": 1,
                                "else": 0,
                            }
                        },
                        "_prize_log": {
                            "$cond": {
                                "if": {
                                    "$and": [
                                        {"$ifNull": ["$prize.totalUSD", False]},
                                        {"$gt": ["$prize.totalUSD", 0]},
                                    ]
                                },
                                "then": {
                                    "$min": [
                                        {
                                            "$divide": [
                                                {"$log": "$prize.totalUSD"},
                                                6.0,
                                            ]
                                        },
                                        1,
                                    ]
                                },
                                "else": 0,
                            }
                        },
                    }
                },
                {
                    "$addFields": {
                        "priorityScore": {
                            "$add": [
                                {"$multiply": ["$_is_trending", 40]},
                                {"$multiply": ["$_is_open", 20]},
                                {
                                    "$min": [
                                        {"$multiply": ["$_view_count", 2]},
                                        20,
                                    ]
                                },
                                {
                                    "$multiply": [
                                        {
                                            "$max": [
                                                0,
                                                {
                                                    "$subtract": [
                                                        1,
                                                        {
                                                            "$divide": [
                                                                "$_days_old",
                                                                90,
                                                            ]
                                                        },
                                                    ]
                                                },
                                            ]
                                        },
                                        10,
                                    ]
                                },
                                {"$multiply": ["$_prize_log", 10]},
                            ]
                        }
                    }
                },
                {"$sort": {"priorityScore": -1}},
                {"$skip": skip},
                {"$limit": batch_size},
            ]

            contests = list(self.contests_collection.aggregate(pipeline))

            # Get total count of contests needing details
            total_needing = self.contests_collection.count_documents(
                {
                    "archivedAt": None,
                    "status": {"$in": ["open", "scheduled"]},
                    **({"_id": {"$nin": list(existing_ids)}} if existing_ids else {}),
                }
            )

            return {
                "success": True,
                "contests": contests,
                "total_needing_generation": total_needing,
                "batch_size": batch_size,
                "skip": skip,
                "returned": len(contests),
            }

        except Exception as e:
            logger.error(f"Error in get_priority_queue: {e}")
            return {"success": False, "error": str(e)}

    # ─── Validation ─────────────────────────────────────────────

    def validate(self, parsed: Dict[str, Any], contest_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate the LLM-generated contest details.

        Checks:
        - Required sections non-empty and minimum length
        - readingTime computed honestly
        - No first-person language
        - No hallucinated URLs
        - FAQ/guide/timeline structural validity if present
        - Schema conformance

        Returns:
            Dict with valid (bool), warnings (list), and fixed content.
        """
        warnings = []
        content = parsed.get("content", {})
        seo = parsed.get("seo", {})

        # ── Required sections ──

        why_join = content.get("whyJoin", "")
        if not isinstance(why_join, str) or len(why_join.strip()) < 100:
            warnings.append("whyJoin is too short or missing (need 100+ chars)")

        who_should = content.get("whoShouldApply", "")
        if not isinstance(who_should, str) or len(who_should.strip()) < 50:
            warnings.append("whoShouldApply is too short or missing (need 50+ chars)")

        benefits = content.get("benefits", [])
        if not isinstance(benefits, list) or len(benefits) < 3:
            warnings.append(
                f"Only {len(benefits) if isinstance(benefits, list) else 0} benefits (need 3+)"
            )
        elif not all(isinstance(b, str) and b.strip() for b in benefits):
            warnings.append("Some benefits are empty or non-string")

        tips = content.get("tips", [])
        if not isinstance(tips, list) or len(tips) < 2:
            warnings.append(f"Only {len(tips) if isinstance(tips, list) else 0} tips (need 2+)")

        # ── readingTime ──

        total_words = 0
        for key, val in content.items():
            if isinstance(val, str):
                total_words += len(val.split())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        total_words += len(item.split())
                    elif isinstance(item, dict):
                        for sub_val in item.values():
                            if isinstance(sub_val, str):
                                total_words += len(sub_val.split())

        expected_reading_time = max(1, math.ceil(total_words / 200))
        provided_reading_time = content.get("readingTime", 0)

        if not isinstance(provided_reading_time, int) or provided_reading_time < 1:
            content["readingTime"] = expected_reading_time
            warnings.append(f"readingTime was invalid; computed as {expected_reading_time}")
        elif provided_reading_time != expected_reading_time:
            warnings.append(
                f"readingTime mismatch: provided={provided_reading_time}, "
                f"computed={expected_reading_time} ({total_words} words)"
            )
            content["readingTime"] = expected_reading_time

        # ── First-person check ──

        first_person_patterns = re.compile(r"\b(I\s|we\s|our\s|my\s|us\b)", re.IGNORECASE)

        for section_key, section_val in content.items():
            if isinstance(section_val, str):
                matches = first_person_patterns.findall(section_val)
                if matches:
                    warnings.append(f"First-person language in '{section_key}': {matches[:3]}")
            elif isinstance(section_val, list):
                for i, item in enumerate(section_val):
                    if isinstance(item, str):
                        matches = first_person_patterns.findall(item)
                        if matches:
                            warnings.append(
                                f"First-person language in '{section_key}[{i}]': {matches[:3]}"
                            )

        # ── Hallucinated URL check ──

        official_link = contest_data.get("link", "")
        official_netloc = urlparse(official_link).netloc if official_link else ""

        all_text = ""
        for key, val in content.items():
            if isinstance(val, str):
                all_text += val + " "
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        all_text += item + " "
                    elif isinstance(item, dict):
                        for sub_val in item.values():
                            if isinstance(sub_val, str):
                                all_text += sub_val + " "

        found_urls = re.findall(r'https?://[^\s\)"\'<>,]+', all_text)
        for url in found_urls:
            url_netloc = urlparse(url).netloc
            # Allow the official domain and research-related domains
            if official_netloc and url_netloc == official_netloc:
                continue
            # Allow common domains that are typically legitimate
            allowed_domains = {
                "reddit.com",
                "www.reddit.com",
                "github.com",
                "www.github.com",
                "twitter.com",
                "www.twitter.com",
                "x.com",
                "youtube.com",
                "www.youtube.com",
                "linkedin.com",
                "www.linkedin.com",
                "medium.com",
                "www.medium.com",
                "devpost.com",
                "www.devpost.com",
                "itch.io",
                "behance.net",
                "www.behance.net",
                "dribbble.com",
                "www.dribbble.com",
            }
            if url_netloc in allowed_domains:
                continue
            # URLs not in allowed list and not the official domain are suspicious
            warnings.append(f"Unverified URL in content: {url} (domain: {url_netloc})")

        # ── FAQ structural check ──

        faq = content.get("faq", [])
        if faq and isinstance(faq, list):
            for i, item in enumerate(faq):
                if not isinstance(item, dict):
                    warnings.append(f"faq[{i}] is not an object")
                    continue
                if not item.get("question") or not item.get("answer"):
                    warnings.append(f"faq[{i}] missing question or answer")

        # ── Submission guide structural check ──

        guide = content.get("submissionGuide", [])
        if guide and isinstance(guide, list):
            for i, item in enumerate(guide):
                if not isinstance(item, dict):
                    warnings.append(f"submissionGuide[{i}] is not an object")
                    continue
                if not item.get("step"):
                    warnings.append(f"submissionGuide[{i}] missing step")

        # ── SEO checks ──

        meta_title = seo.get("metaTitle", "")
        if meta_title and len(meta_title) > 60:
            warnings.append(f"metaTitle exceeds 60 chars ({len(meta_title)})")

        meta_desc = seo.get("metaDescription", "")
        if meta_desc and len(meta_desc) > 160:
            warnings.append(f"metaDescription exceeds 160 chars ({len(meta_desc)})")

        is_valid = len(warnings) < 5

        return {
            "valid": is_valid,
            "warnings": warnings,
            "warning_count": len(warnings),
            "total_words": total_words,
            "content": content,
            "seo": seo,
        }

    # ─── Save ───────────────────────────────────────────────────

    def save(
        self,
        contest_id: str,
        content: Dict[str, Any],
        seo: Dict[str, Any],
        warnings: List[str],
    ) -> Dict[str, Any]:
        """
        Save versioned contest_details document.

        - Finds existing document for this contestId
        - Increments version (or starts at 1)
        - Sets previousVersionAt to old generatedAt
        - Upserts into contest_details collection

        Args:
            contest_id: The contest ObjectId string
            content: The validated content dict
            seo: The SEO metadata dict
            warnings: Validation warnings

        Returns:
            Dict with success, version, is_new
        """
        try:
            contest_oid = ObjectId(contest_id)
            existing = self.details_collection.find_one({"contestId": contest_oid})

            new_version = (existing["version"] + 1) if existing else 1
            is_new = existing is None

            now = datetime.now(timezone.utc)

            # Determine if content is truly empty (only readingTime or nothing at all)
            meaningful_keys = [k for k in content.keys() if k != "readingTime"]
            has_meaningful_content = len(meaningful_keys) > 0

            doc = {
                "contestId": contest_oid,
                "version": new_version,
                "schemaVersion": 1,
                "status": "completed" if has_meaningful_content else "failed",
                "generatedBy": "mistral-vibe-detail-pipeline",
                "generatedAt": now,
                "previousVersionAt": existing.get("generatedAt") if existing else None,
                "changeLog": (
                    f"Initial generation (v{new_version})"
                    if is_new
                    else f"Regeneration (v{existing['version']} -> v{new_version})"
                ),
                "content": content,
                "seo": seo,
                "metadata": {
                    "pipelineSteps": ["prompt", "generate", "validate", "save"],
                    "qualityScore": max(0, 100 - len(warnings) * 15),
                    "warnings": warnings if warnings else [],
                },
            }

            self.details_collection.update_one(
                {"contestId": contest_oid},
                {"$set": doc},
                upsert=True,
            )

            logger.info(
                f"Saved contest_details v{new_version} for contest {contest_id} "
                f"({'new' if is_new else 'update'})"
            )

            return {
                "success": True,
                "version": new_version,
                "is_new": is_new,
                "warnings": warnings,
                "quality_score": doc["metadata"]["qualityScore"],
            }

        except Exception as e:
            logger.error(f"Error saving contest_details for {contest_id}: {e}")
            return {"success": False, "error": str(e)}

    # ─── Rollback ───────────────────────────────────────────────

    def rollback(self, contest_id: str) -> Dict[str, Any]:
        """
        Rollback contest_details to the previous version.

        - Finds current document
        - If no previousVersionAt, nothing to rollback to
        - Otherwise, finds the version before current
        - Swaps current's content/seo with previous

        Args:
            contest_id: The contest ObjectId string

        Returns:
            Dict with success, rolled_back_to_version, or error reason
        """
        try:
            contest_oid = ObjectId(contest_id)
            current = self.details_collection.find_one({"contestId": contest_oid})

            if not current:
                return {
                    "success": False,
                    "reason": "No contest_details found for this contest",
                }

            if not current.get("previousVersionAt"):
                return {
                    "success": False,
                    "reason": "No previous version available (this is the first version)",
                }

            # Find the previous version by matching generatedAt
            previous = self.details_collection.find_one(
                {
                    "contestId": contest_oid,
                    "generatedAt": current["previousVersionAt"],
                }
            )

            if not previous:
                return {
                    "success": False,
                    "reason": "Previous version document not found in database",
                }

            now = datetime.now(timezone.utc)

            self.details_collection.update_one(
                {"_id": current["_id"]},
                {
                    "$set": {
                        "version": previous["version"],
                        "generatedBy": "rollback",
                        "generatedAt": now,
                        "previousVersionAt": current["generatedAt"],
                        "changeLog": (
                            f"Rolled back from v{current['version']} to v{previous['version']}"
                        ),
                        "status": "completed",
                        "content": previous["content"],
                        "seo": previous.get("seo", {}),
                    }
                },
            )

            logger.info(
                f"Rolled back contest_details for {contest_id}: "
                f"v{current['version']} -> v{previous['version']}"
            )

            return {
                "success": True,
                "rolled_back_to_version": previous["version"],
                "from_version": current["version"],
            }

        except Exception as e:
            logger.error(f"Error rolling back contest_details for {contest_id}: {e}")
            return {"success": False, "error": str(e)}

    # ─── Read ──────────────────────────────────────────────────

    def get_details(self, contest_id: str) -> Dict[str, Any]:
        """
        Read the current contest_details for a contest.

        Args:
            contest_id: The contest ObjectId string

        Returns:
            Dict with the details document or null if not found.
        """
        try:
            contest_oid = ObjectId(contest_id)
            doc = self.details_collection.find_one(
                {"contestId": contest_oid},
                sort=[("version", -1)],
            )
            if not doc:
                return {"success": True, "details": None}

            # Convert ObjectIds to strings for JSON
            doc["_id"] = str(doc["_id"])
            doc["contestId"] = str(doc["contestId"])
            return {"success": True, "details": doc}

        except Exception as e:
            logger.error(f"Error reading contest_details for {contest_id}: {e}")
            return {"success": False, "error": str(e)}

    # ─── Status ─────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """
        Get pipeline status overview.

        Returns:
            Dict with counts by status, total contests, coverage %.
        """
        try:
            pipeline = [
                {
                    "$group": {
                        "_id": "$status",
                        "count": {"$sum": 1},
                    }
                }
            ]
            by_status = list(self.details_collection.aggregate(pipeline))
            status_map = {item["_id"]: item["count"] for item in by_status}

            total_contests = self.contests_collection.count_documents({"archivedAt": None})
            total_with_details = sum(
                count for status, count in status_map.items() if status != "failed"
            )

            return {
                "success": True,
                "total_contests": total_contests,
                "total_with_details": total_with_details,
                "total_without_details": total_contests - total_with_details,
                "by_status": status_map,
                "coverage_pct": round((total_with_details / max(1, total_contests)) * 100, 1),
            }

        except Exception as e:
            logger.error(f"Error getting detail generation status: {e}")
            return {"success": False, "error": str(e)}
