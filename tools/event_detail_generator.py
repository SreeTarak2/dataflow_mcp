"""
Event detail generation — orchestrates the event detail pipeline (events-v1.1).

Mirrors ``tools/contest_detail_generator.py`` for the Events collection:

    Pipeline:
      1. get_priority_queue() → find events needing details, sorted by priority
      2. LLM generates content (via separate prompt/submit flow,
         prompts/event-details-v1.0.txt)
      3. validate() → check output quality
      4. save() → versioned upsert into event_details

This tool is prompt-serving and validation-only. The LLM does its own web
research and generation via the prompt in event-details-v1.0.txt.
"""

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List
from bson.objectid import ObjectId
from urllib.parse import urlparse

from config.mongodb import db

logger = logging.getLogger(__name__)

EVENT_DETAILS_COLLECTION = "event_details"
EVENTS_COLLECTION = "Events"


class EventDetailGenerator:
    """Orchestrates the event detail generation pipeline."""

    def __init__(self):
        self.details_collection = db[EVENT_DETAILS_COLLECTION]
        self.events_collection = db[EVENTS_COLLECTION]

    # ─── Priority Queue ─────────────────────────────────────────

    def get_priority_queue(self, batch_size: int = 10, skip: int = 0) -> Dict[str, Any]:
        """
        Find events needing detail generation, sorted by priority.

        Priority formula:
          - Event start date is in the future (upcoming):  +40
          - status is "published":                         +20 (draft: +10)
          - registration.status is "open":                 +10
          - Has speakers or agenda populated:              +10
          - Freshness (createdAt):                         up to +20 (linear decay over 90 days)

        Only live events (status "published" or "draft", not archived) with no
        existing details doc are queued. Date fields are stored as ISO strings
        by the events pipeline, so they are coerced with ``$dateFromString``
        before comparison.

        Returns:
            Dict with events list, total count, and metadata.
        """
        try:
            now = datetime.now(timezone.utc)

            # Get event IDs that already have completed details
            existing_ids = set()
            for doc in self.details_collection.find({}, {"eventId": 1}):
                existing_ids.add(doc["eventId"])

            queue_filter: Dict[str, Any] = {
                "archivedAt": None,
                "status": {"$in": ["published", "draft"]},
            }
            if existing_ids:
                queue_filter["_id"] = {"$nin": list(existing_ids)}

            pipeline = [
                {"$match": queue_filter},
                # Coerce ISO-string dates to real datetimes (null on parse failure)
                {
                    "$addFields": {
                        "_created_dt": {
                            "$dateFromString": {
                                "dateString": "$createdAt",
                                "onError": None,
                                "onNull": None,
                            }
                        },
                        "_start_dt": {
                            "$dateFromString": {
                                "dateString": "$eventDates.start",
                                "onError": None,
                                "onNull": None,
                            }
                        },
                    }
                },
                {
                    "$addFields": {
                        "_days_old": {
                            "$cond": {
                                "if": {"$ifNull": ["$_created_dt", False]},
                                "then": {
                                    "$divide": [
                                        {"$subtract": [now, "$_created_dt"]},
                                        86400000,
                                    ]
                                },
                                "else": 999,
                            }
                        },
                        "_is_upcoming": {
                            "$cond": {
                                "if": {"$ifNull": ["$_start_dt", False]},
                                "then": {
                                    "$cond": {
                                        "if": {"$gte": ["$_start_dt", now]},
                                        "then": 1,
                                        "else": 0,
                                    }
                                },
                                "else": 0,
                            }
                        },
                        "_is_published": {
                            "$cond": {
                                "if": {"$eq": ["$status", "published"]},
                                "then": 20,
                                "else": 10,
                            }
                        },
                        "_reg_open": {
                            "$cond": {
                                "if": {"$eq": ["$registration.status", "open"]},
                                "then": 10,
                                "else": 0,
                            }
                        },
                        "_has_lineup": {
                            "$cond": {
                                "if": {
                                    "$or": [
                                        {"$gt": [{"$size": {"$ifNull": ["$speakers", []]}}, 0]},
                                        {"$gt": [{"$size": {"$ifNull": ["$agenda", []]}}, 0]},
                                    ]
                                },
                                "then": 10,
                                "else": 0,
                            }
                        },
                    }
                },
                {
                    "$addFields": {
                        "priorityScore": {
                            "$add": [
                                {"$multiply": ["$_is_upcoming", 40]},
                                "$_is_published",
                                "$_reg_open",
                                "$_has_lineup",
                                {
                                    "$multiply": [
                                        {
                                            "$max": [
                                                0,
                                                {
                                                    "$subtract": [
                                                        1,
                                                        {"$divide": ["$_days_old", 90]},
                                                    ]
                                                },
                                            ]
                                        },
                                        20,
                                    ]
                                },
                            ]
                        }
                    }
                },
                {"$sort": {"priorityScore": -1, "_start_dt": 1}},
                {"$skip": skip},
                {"$limit": batch_size},
            ]

            events = list(self.events_collection.aggregate(pipeline))

            total_needing = self.events_collection.count_documents(queue_filter)

            return {
                "success": True,
                "events": events,
                "total_needing_generation": total_needing,
                "batch_size": batch_size,
                "skip": skip,
                "returned": len(events),
            }

        except Exception as e:
            logger.error(f"Error in EventDetailGenerator.get_priority_queue: {e}")
            return {"success": False, "error": str(e)}

    # ─── Validation ─────────────────────────────────────────────

    def validate(self, parsed: Dict[str, Any], event_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate the LLM-generated event details.

        Checks:
        - Required sections non-empty and minimum length
        - readingTime computed honestly
        - No first-person language
        - No hallucinated URLs
        - FAQ structural validity if present
        - SEO conformance (metaTitle <= 60, metaDescription <= 160)

        Returns:
            Dict with valid (bool), warnings (list), and fixed content.
        """
        warnings = []
        content = parsed.get("content", {})

        if not isinstance(content, dict):
            return {
                "valid": False,
                "warnings": ["content must be an object"],
                "warning_count": 1,
                "total_words": 0,
                "content": {},
                "seo": parsed.get("seo", {}) if isinstance(parsed.get("seo"), dict) else {},
            }

        seo = parsed.get("seo", {})
        if not isinstance(seo, dict):
            seo = {}

        # ── Required sections ──

        why_attend = content.get("whyAttend", "")
        if not isinstance(why_attend, str) or len(why_attend.strip()) < 100:
            warnings.append("whyAttend is too short or missing (need 100+ chars)")

        who_should = content.get("whoShouldAttend", "")
        if not isinstance(who_should, str) or len(who_should.strip()) < 50:
            warnings.append("whoShouldAttend is too short or missing (need 50+ chars)")

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

        source_obj = event_data.get("source")
        if isinstance(source_obj, dict) and source_obj.get("url"):
            official_link = source_obj["url"]
        else:
            official_link = event_data.get("link") or ""
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
        allowed_domains = {
            "reddit.com", "www.reddit.com",
            "github.com", "www.github.com",
            "twitter.com", "www.twitter.com",
            "x.com",
            "youtube.com", "www.youtube.com",
            "linkedin.com", "www.linkedin.com",
            "medium.com", "www.medium.com",
            "eventbrite.com", "www.eventbrite.com",
            "luma.com", "lu.ma",
            "meetup.com", "www.meetup.com",
            "behance.net", "www.behance.net",
            "dribbble.com", "www.dribbble.com",
        }
        for url in found_urls:
            url_netloc = urlparse(url).netloc
            if official_netloc and url_netloc == official_netloc:
                continue
            if url_netloc in allowed_domains:
                continue
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
        event_id: str,
        content: Dict[str, Any],
        seo: Dict[str, Any],
        warnings: List[str],
    ) -> Dict[str, Any]:
        """
        Save versioned event_details document.

        - Finds existing document for this eventId
        - Increments version (or starts at 1)
        - Sets previousVersionAt to old generatedAt
        - Upserts into event_details collection

        Args:
            event_id: The event ObjectId string
            content: The validated content dict
            seo: The SEO metadata dict
            warnings: Validation warnings

        Returns:
            Dict with success, version, is_new
        """
        try:
            event_oid = ObjectId(event_id)
            existing = self.details_collection.find_one({"eventId": event_oid})

            new_version = (existing["version"] + 1) if existing else 1
            is_new = existing is None

            now = datetime.now(timezone.utc)

            # Determine if content is truly empty (only readingTime or nothing at all)
            meaningful_keys = [k for k in content.keys() if k != "readingTime"]
            has_meaningful_content = len(meaningful_keys) > 0

            doc = {
                "eventId": event_oid,
                "version": new_version,
                "schemaVersion": 1,
                "status": "completed" if has_meaningful_content else "failed",
                "generatedBy": "event-detail-pipeline",
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

            write_result = self.details_collection.update_one(
                {"eventId": event_oid},
                {"$set": doc},
                upsert=True,
            )

            # --- Verify the write actually landed ---
            if write_result.matched_count == 0 and write_result.upserted_id is None:
                # update_one with upsert=True should ALWAYS either match or upsert.
                # If neither happened, the write was silently dropped.
                logger.error(
                    f"event_details write silently dropped for {event_id}: "
                    f"matched={write_result.matched_count}, upserted={write_result.upserted_id}"
                )
                return {
                    "success": False,
                    "error": (
                        f"Write was silently dropped (matched=0, upserted=None). "
                        f"This usually indicates a MongoDB connection issue or "
                        f"collection-level validation rejection."
                    ),
                }

            # --- Post-write verification: read back to confirm persistence ---
            verify = self.details_collection.find_one({"eventId": event_oid})
            if not verify:
                logger.error(
                    f"event_details post-write verification FAILED for {event_id}: "
                    f"document not found after successful upsert"
                )
                return {
                    "success": False,
                    "error": (
                        f"Write reported success (matched={write_result.matched_count}, "
                        f"upserted={write_result.upserted_id}) but document not found "
                        f"on immediate read-back. Possible replica set / write concern issue."
                    ),
                }

            logger.info(
                f"Saved event_details v{new_version} for event {event_id} "
                f"({'new' if is_new else 'update'}, "
                f"matched={write_result.matched_count}, "
                f"upserted={write_result.upserted_id})"
            )

            return {
                "success": True,
                "version": new_version,
                "is_new": is_new,
                "warnings": warnings,
                "quality_score": doc["metadata"]["qualityScore"],
            }

        except Exception as e:
            logger.error(f"Error saving event_details for {event_id}: {e}")
            return {"success": False, "error": str(e)}

    # ─── Rollback ───────────────────────────────────────────────

    def rollback(self, event_id: str) -> Dict[str, Any]:
        """
        Rollback event_details to the previous version.

        - Finds current document
        - If no previousVersionAt, nothing to rollback to
        - Otherwise, finds the version before current
        - Swaps current's content/seo with previous

        Args:
            event_id: The event ObjectId string

        Returns:
            Dict with success, rolled_back_to_version, or error reason
        """
        try:
            event_oid = ObjectId(event_id)
            current = self.details_collection.find_one({"eventId": event_oid})

            if not current:
                return {
                    "success": False,
                    "reason": "No event_details found for this event",
                }

            if not current.get("previousVersionAt"):
                return {
                    "success": False,
                    "reason": "No previous version available (this is the first version)",
                }

            # Find the previous version by matching generatedAt
            previous = self.details_collection.find_one(
                {
                    "eventId": event_oid,
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
                f"Rolled back event_details for {event_id}: "
                f"v{current['version']} -> v{previous['version']}"
            )

            return {
                "success": True,
                "rolled_back_to_version": previous["version"],
                "from_version": current["version"],
            }

        except Exception as e:
            logger.error(f"Error rolling back event_details for {event_id}: {e}")
            return {"success": False, "error": str(e)}

    # ─── Read ──────────────────────────────────────────────────

    def get_details(self, event_id: str) -> Dict[str, Any]:
        """
        Read the current event_details for an event.

        Args:
            event_id: The event ObjectId string

        Returns:
            Dict with the details document or null if not found.
        """
        try:
            event_oid = ObjectId(event_id)
            doc = self.details_collection.find_one(
                {"eventId": event_oid},
                sort=[("version", -1)],
            )
            if not doc:
                return {"success": True, "details": None}

            # Convert ObjectIds to strings for JSON
            doc["_id"] = str(doc["_id"])
            doc["eventId"] = str(doc["eventId"])
            return {"success": True, "details": doc}

        except Exception as e:
            logger.error(f"Error reading event_details for {event_id}: {e}")
            return {"success": False, "error": str(e)}

    # ─── Status ─────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """
        Get pipeline status overview.

        Returns:
            Dict with counts by status, total events, coverage %.
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

            total_events = self.events_collection.count_documents({"archivedAt": None})
            total_with_details = sum(
                count for status, count in status_map.items() if status != "failed"
            )

            return {
                "success": True,
                "total_events": total_events,
                "total_with_details": total_with_details,
                "total_without_details": total_events - total_with_details,
                "by_status": status_map,
                "coverage_pct": round((total_with_details / max(1, total_events)) * 100, 1),
            }

        except Exception as e:
            logger.error(f"Error getting event detail generation status: {e}")
            return {"success": False, "error": str(e)}
