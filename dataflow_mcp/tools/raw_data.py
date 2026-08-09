"""Raw scraped data MCP tools (CHRawdata bridge + overview)."""

import json
from typing import Any, Dict, Optional

from dataflow_mcp.core import mcp, logger, check_rate_limit, update_metrics
from tools import raw_data_processor
from tools.data_manager import DataManager


@mcp.tool()
def get_raw_data_status(source: Optional[str] = None) -> dict:
    """
    Return a summary of what raw scraped data is available in CHRawdata.rawdata.

    If `source` is provided (e.g. "contestwatchers", "opportunityDesk"), only
    records from that scraper are considered.
    """
    try:
        check_rate_limit("get_raw_data_status")
        logger.info(f"Raw data status requested for source={source}")
        result = raw_data_processor.get_raw_data_status(source)
        update_metrics(result.get("success", False))
        return result
    except Exception as e:
        logger.error(f"Error in get_raw_data_status: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def read_raw_collection(
    collection_name: str,
    filter_query: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    sort_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read documents from the CHRawdata database (raw scraped data) with filtering and pagination.

    Args:
        collection_name: Name of the collection to read from (e.g. "rawdata")
        filter_query: JSON string with MongoDB filter query (optional)
        limit: Maximum number of documents to return (max 1000)
        skip: Number of documents to skip for pagination
        sort_by: Field name to sort by (optional)

    Returns:
        Dictionary containing the documents and metadata
    """
    client_id = "read_raw_collection"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Reading from raw collection: {collection_name}")

        filter_dict = None
        if filter_query:
            try:
                filter_dict = json.loads(filter_query)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON filter: {filter_query}")
                return {"success": False, "error": "Invalid JSON in filter_query"}

        result = DataManager.read_raw_data(
            collection_name=collection_name,
            filter_query=filter_dict,
            limit=limit,
            skip=skip,
            sort_by=sort_by,
        )

        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in read_raw_collection: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_scraped_overview(
    source: Optional[str] = None,
) -> dict:
    """
    Get a quick, actionable overview of what raw scraped records are available.

    Use this to see what's in the pipeline before deciding which source
    to work on. Returns counts by source, validation status breakdown,
    total records, newest/oldest record dates, and a few sample titles.

    This is designed for AI agents (ChatGPT, Mistral, Claude) to quickly
    understand what data is available and decide what to work on next.

    Args:
        source: Optional scraper source to filter by
                (e.g. "contestwatchers", "opportunityDesk")

    Returns:
        Dictionary with overview statistics and sample records
    """
    client_id = "get_scraped_overview"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        logger.info(f"Scraped overview requested for source={source}")

        from config.mongodb import get_raw_db, RAW_COLLECTION

        raw_collection = get_raw_db()[RAW_COLLECTION]

        match_filter = {"source": source} if source else {}

        # Total count
        total = raw_collection.count_documents(match_filter)

        # Breakdown by source
        source_pipeline = [
            {"$match": match_filter},
            {
                "$group": {
                    "_id": "$source",
                    "count": {"$sum": 1},
                    "last_scraped": {"$max": "$scrapedAt"},
                    "first_scraped": {"$min": "$scrapedAt"},
                }
            },
            {"$sort": {"count": -1}},
        ]
        by_source = list(raw_collection.aggregate(source_pipeline))

        # Validation status breakdown
        status_pipeline = [
            {"$match": match_filter},
            {
                "$group": {
                    "_id": {"$ifNull": ["$validationStatus", "pending"]},
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"count": -1}},
        ]
        by_status = list(raw_collection.aggregate(status_pipeline))

        # Get a few sample titles (most recent)
        samples = list(
            raw_collection.find(
                match_filter,
                {"title": 1, "source": 1, "scrapedAt": 1, "url": 1, "validationStatus": 1},
            )
            .sort("scrapedAt", -1)
            .limit(5)
        )

        # Convert ObjectIds to strings
        for s in samples:
            s["_id"] = str(s["_id"])

        # Count records that are ready for structuring (have title + url)
        ready_filter = {
            **match_filter,
            "title": {"$exists": True, "$ne": ""},
            "url": {"$exists": True, "$ne": ""},
        }
        ready_count = raw_collection.count_documents(ready_filter)

        update_metrics(True)
        return {
            "success": True,
            "database": "CHRawdata",
            "collection": RAW_COLLECTION,
            "source_filter": source or "all",
            "total_records": total,
            "ready_for_structuring": ready_count,
            "by_source": [
                {
                    "source": s["_id"],
                    "count": s["count"],
                    "last_scraped": s.get("last_scraped"),
                    "first_scraped": s.get("first_scraped"),
                }
                for s in by_source
            ],
            "by_validation_status": {s["_id"]: s["count"] for s in by_status},
            "samples": [
                {
                    "_id": s["_id"],
                    "title": s.get("title", ""),
                    "source": s.get("source", ""),
                    "url": s.get("url", ""),
                    "scraped_at": s.get("scrapedAt"),
                    "validation_status": s.get("validationStatus", "pending"),
                }
                for s in samples
            ],
            "usage": {
                "purpose": "Use this overview to decide which source to work on. Then call get_records_for_structuring(source=..., limit=5) to fetch records for AI structuring, or get_records_for_validation(source=..., limit=5) to validate records first.",
                "available_pipelines": [
                    "get_records_for_structuring — fetch records + prompts for AI structuring",
                    "get_records_for_validation — claim records for web validation",
                    "get_raw_data_status — detailed raw data stats",
                    "get_validation_status — validation pipeline progress",
                ],
            },
        }

    except Exception as e:
        logger.error(f"Error in get_scraped_overview: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def process_raw_data(
    source: str,
    limit: int = 100,
    auto_image: bool = False,
    dedupe_gate: bool = True,
) -> dict:
    """
    Read validated raw records from CHRawdata.rawdata for a given scraper
    `source`, normalize, deduplicate, and upsert them into the primary
    Contests collection (ContestHopperDb).

    NOTE: By default, only records with validationStatus="validated" are
    processed. Run get_records_for_validation + submit_raw_validation first.

    Args:
        source: Scraper name (e.g. "contestwatchers", "opportunityDesk")
        limit: Max records to process per call (default 100, max 1000)
        auto_image: If True, automatically download, convert (WebP+AVIF),
                    and upload images to R2 after upserting contest data
        dedupe_gate: If True (default), skip records whose title matches an
                     existing live contest (normalized-title match from a
                     different source, or reworded title from the same source).
                     Set False to force-insert.
    """
    try:
        check_rate_limit("process_raw_data")
        logger.info(f"Processing raw data for source={source}, limit={limit}")
        limit = min(limit, 1000)  # safety cap
        result = raw_data_processor.process_raw_data(
            source,
            limit,
            auto_image=auto_image,
            require_validation=True,
            dedupe_gate=dedupe_gate,
        )
        update_metrics(result.get("success", False))
        return result
    except Exception as e:
        logger.error(f"Error in process_raw_data: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
