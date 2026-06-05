"""
Raw Data Processor — MCP tool for reading scraped raw data from CHRawdata.rawdata
(cluster A), normalizing it, and upserting into the primary ContestHopperDb.Contests
(cluster B).

Tools exposed:
  get_raw_data_status(source=None)  — summary of raw data by source
  process_raw_data(source, limit)   — full normalize-and-upsert pipeline
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from pymongo import UpdateOne

from config.mongodb import db, get_raw_db, RAW_COLLECTION
from tools.tag_normalizer import normalize_tags_array
import json
import os
import subprocess

logger = logging.getLogger(__name__)

# Target collection in the primary ContestHopperDb
TARGET_COLLECTION = os.getenv("COLLECTION_NAME", "Contests")

# Path to the Node.js scrapping project for image processing
SCRAPING_PROJECT = os.getenv(
    "SCRAPING_PROJECT_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "scrapping"
    ),
)
BRIDGE_SCRIPT = os.path.join(SCRAPING_PROJECT, "scripts", "processSingleImage.js")

# Fields considered critical for a usable contest record
REQUIRED_FIELDS = {"title", "url"}


def _parse_deadline(raw_deadline: any) -> tuple[Optional[str], Optional[str]]:
    """
    Attempt to parse a deadline value into an ISO 8601 string and derive
    the contest status ("Open" | "Closed").

    Returns (iso_deadline, status).
    """
    if not raw_deadline:
        return None, None

    # Already a datetime object
    if isinstance(raw_deadline, datetime):
        iso = raw_deadline.isoformat()
        status = "Closed" if raw_deadline < datetime.now(timezone.utc) else "Open"
        return iso, status

    # String: try common formats
    if isinstance(raw_deadline, str):
        raw = raw_deadline.strip()
        if not raw:
            return None, None

        # Try ISO format first (most common from scrapers)
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%B %d, %Y",
            "%d %B %Y",
        ]:
            try:
                dt = datetime.strptime(raw, fmt)
                # Naive datetime — assume UTC
                dt = dt.replace(tzinfo=timezone.utc)
                iso = dt.isoformat()
                status = "Closed" if dt < datetime.now(timezone.utc) else "Open"
                return iso, status
            except ValueError:
                continue

        logger.debug(f"Could not parse deadline string: {raw}")
        return raw_deadline, None

    return str(raw_deadline), None


def _validate_record(record: dict) -> tuple[bool, str]:
    """Check that a record has all required fields. Returns (valid, reason)."""
    missing = REQUIRED_FIELDS - set(record.keys())
    if missing:
        title = record.get("title", "<untitled>")
        return False, f"Missing fields {missing} in record '{title}'"
    return True, ""


def _normalize_record(record: dict, source: str) -> dict:
    """
    Clean and enrich a single raw record so it is ready for the Contests
    collection.

    - Converts ObjectId to string
    - Normalises deadline and derives status
    - Cleans tags via tag_normalizer
    - Strips internal scrapedAt / source tracking
    """
    normalized = {}

    # Convert _id to string if present (keep for traceability but don't overwrite)
    if "_id" in record:
        if isinstance(record["_id"], ObjectId):
            normalized["_raw_id"] = str(record["_id"])
        else:
            normalized["_raw_id"] = record["_id"]

    # Copy safe fields
    for field in [
        "title",
        "description",
        "url",
        "imageUrl",
        "prize",
        "location",
        "fee",
        "organizer",
        "category",
    ]:
        if field in record and record[field] is not None:
            normalized[field] = record[field]

    # type — infer from record or default to "contest"
    raw_type = record.get("type")
    if raw_type in ("contest", "hackathon", "grant", "fellowship", "award", "challenge"):
        normalized["type"] = raw_type
    else:
        normalized["type"] = "contest"

    # flags — ["women"] for women-exclusive contests
    raw_flags = record.get("flags", [])
    if isinstance(raw_flags, list):
        valid_flags = [f for f in raw_flags if f in ("women",)]
        if valid_flags:
            normalized["flags"] = valid_flags
    elif isinstance(raw_flags, str) and raw_flags == "women":
        normalized["flags"] = ["women"]

    # Normalize deadline
    deadline, status = _parse_deadline(record.get("deadline"))
    if deadline:
        normalized["deadline"] = deadline
    if status:
        normalized["status"] = status

    # Clean tags
    raw_tags = record.get("tags", [])
    if isinstance(raw_tags, list) and raw_tags:
        normalized["tags"] = normalize_tags_array(raw_tags, max_tags=6)
    elif isinstance(raw_tags, str) and raw_tags.strip():
        normalized["tags"] = normalize_tags_array(
            [t.strip() for t in raw_tags.split(",")], max_tags=6
        )

    # Enrich with metadata
    normalized["source"] = source
    normalized["processedAt"] = datetime.now(timezone.utc).isoformat()

    return normalized


# ---- Exposed MCP tools ----


def get_raw_data_status(source: Optional[str] = None) -> dict:
    """
    Return a summary of what raw data is available in CHRawdata.rawdata.

    If `source` is provided, only records from that scraper source are
    considered.
    """
    try:
        raw_collection = get_raw_db()[RAW_COLLECTION]
    except Exception as e:
        logger.error(f"Failed to connect to raw database: {e}")
        return {"success": False, "error": str(e)}

    match = {"source": source} if source else {}

    try:
        total = raw_collection.count_documents(match)

        # Group by source for overview
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": "$source",
                    "count": {"$sum": 1},
                    "lastScraped": {"$max": "$scrapedAt"},
                }
            },
            {"$sort": {"count": -1}},
        ]
        by_source = list(raw_collection.aggregate(pipeline))

        # Records with missing critical fields
        missing_pipeline = [
            {
                "$match": {
                    **match,
                    "$or": [
                        {"title": {"$in": [None, ""]}},
                        {"url": {"$in": [None, ""]}},
                    ],
                }
            },
            {"$count": "count"},
        ]
        missing_result = list(raw_collection.aggregate(missing_pipeline))
        missing_count = missing_result[0]["count"] if missing_result else 0

        return {
            "success": True,
            "database": "CHRawdata",
            "collection": RAW_COLLECTION,
            "filter": source or "all sources",
            "totalRecords": total,
            "recordsWithMissingFields": missing_count,
            "bySource": [
                {
                    "source": s["_id"],
                    "count": s["count"],
                    "lastScraped": s.get("lastScraped"),
                }
                for s in by_source
            ],
        }
    except Exception as e:
        logger.error(f"Error querying raw data status: {e}")
        return {"success": False, "error": str(e)}


def process_raw_data(
    source: str,
    limit: int = 100,
    auto_image: bool = False,
    require_validation: bool = True,
) -> dict:
    """
    Read raw records from CHRawdata.rawdata for a given scraper `source`,
    validate, normalize, and upsert them into the primary Contests collection.

    Deduplication key: `source` + `title`.

    Args:
        source: Scraper source name (e.g. "contestwatchers", "opportunityDesk")
        limit: Maximum number of raw records to process (default 100)
        auto_image: If True, automatically download, convert (WebP+AVIF),
                    and upload images to R2 after upserting contest data
        require_validation: If True, only process records with
                            validationStatus="validated" (default True)

    Returns:
        dict with inserted / updated / skipped / error counts
    """
    if not source:
        return {"success": False, "error": "'source' parameter is required"}

    # 1. Connect to raw database
    try:
        raw_collection = get_raw_db()[RAW_COLLECTION]
    except Exception as e:
        logger.error(f"Failed to connect to raw database: {e}")
        return {"success": False, "error": str(e)}

    # 2. Build filter — only process validated records when require_validation=True
    db_filter: dict = {"source": source}
    if require_validation:
        db_filter["validationStatus"] = "validated"
        logger.info(
            f"Filtering raw records with validationStatus='validated' for source '{source}'"
        )

    # 3. Fetch raw records
    try:
        raw_records = list(raw_collection.find(db_filter).sort("scrapedAt", -1).limit(limit))
    except Exception as e:
        logger.error(f"Failed to fetch raw records: {e}")
        return {"success": False, "error": str(e)}

    if not raw_records:
        msg = (
            f"No raw records found for source '{source}'"
            if not require_validation
            else f"No validated raw records found for source '{source}'. "
            f"Run get_records_for_validation first, then submit_raw_validation."
        )
        return {
            "success": True,
            "source": source,
            "message": msg,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
        }

    logger.info(
        f"Processing {len(raw_records)} raw records from source '{source}' "
        f"(require_validation={require_validation})"
    )

    # 3. Validate, normalize, build upsert operations
    target_collection = db[TARGET_COLLECTION]

    operations = []
    inserted = 0
    updated = 0
    skipped = 0
    errors = 0
    error_details = []

    upserted_titles = []  # track titles for auto_image lookups
    for record in raw_records:
        # Store title for auto_image lookup
        record_title = record.get("title", "")

        # Validate
        valid, reason = _validate_record(record)
        if not valid:
            skipped += 1
            error_details.append(reason)
            logger.warning(reason)
            continue

        # Normalize
        try:
            normalized = _normalize_record(record, source)
        except Exception as e:
            errors += 1
            msg = f"Normalization error for '{record.get('title', '<untitled>')}': {e}"
            error_details.append(msg)
            logger.error(msg)
            continue

        title = normalized.get("title", "")
        # Track for auto_image lookup
        if auto_image and title:
            upserted_titles.append(title)

        filter_key = {"source": source, "title": title}

        operations.append(
            UpdateOne(
                filter_key,
                {"$set": normalized},
                upsert=True,
            )
        )

    # 4. Execute bulk upsert in batches of 100
    if operations:
        batch_size = 100
        for i in range(0, len(operations), batch_size):
            batch = operations[i : i + batch_size]
            try:
                result = target_collection.bulk_write(batch, ordered=False)
                inserted += result.upserted_count
                updated += result.modified_count
            except Exception as e:
                errors += len(batch)
                msg = f"Bulk write error on batch {i // batch_size}: {e}"
                error_details.append(msg)
                logger.error(msg)

    # 5. Mark processed raw records (optional: delete or flag)
    try:
        raw_ids = [r["_id"] for r in raw_records if "_id" in r]
        raw_collection.update_many(
            {"_id": {"$in": raw_ids}},
            {"$set": {"processedAt": datetime.now(timezone.utc).isoformat()}},
        )
    except Exception as e:
        logger.warning(f"Could not mark raw records as processed: {e}")

    return {
        "success": True,
        "source": source,
        "totalFetched": len(raw_records),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "details": error_details[:10],  # limit error details to first 10
    }
