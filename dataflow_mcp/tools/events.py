"""Events pipeline MCP tools (events-v1.1 schema).

Workflow for AI chatbots::

    get_records_for_events(source=..., limit=10)   → raw URLs + event-structuring-v1.1.txt prompt
    [chatbot structures each event with the prompt]
    submit_structured_events(events_json)          → persists to the Events collection

    get_events_for_detail_generation(batch_size=10) → events + event-details-v1.0.txt prompt
    [chatbot researches and writes event details]
    submit_event_details(event_id, details_json)   → versioned save to event_details
    get_event_detail_status()                      → coverage metrics for the detail pipeline

Read back with ``get_events`` / ``get_events_overview`` (or the generic
``read_collection`` / ``get_document`` CRUD tools).
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dataflow_mcp.core import (
    mcp,
    logger,
    check_rate_limit,
    update_metrics,
    load_prompt_text,
    EVENT_COLLECTION,
    PROMPT_EVENTS,
    PROMPT_EVENT_DETAILS,
    _json_safe,
    _build_normalized_event,
)
from tools.dedup_gate import build_title_index, find_near_duplicates, normalize_title
from tools.data_manager import DataManager
from tools.event_detail_generator import EVENT_DETAILS_COLLECTION, EventDetailGenerator


@mcp.tool()
def get_records_for_events(
    source: Optional[str] = None,
    limit: int = 10,
    collection_name: str = "raw_urls",
) -> dict:
    """
    Fetch raw records + the Events prompt (event-structuring-v1.1.txt,
    events-v1.1 schema) so a chatbot can structure participatory events
    (conferences, workshops, meetups, webinars, summits, trainings).

    Use this when you want to go from raw event URLs/titles to a structured
    event document in one AI round-trip. The chatbot should:
      1. Read the event prompt_text (events-v1.1 schema and rules)
      2. For each record, pin the target event (name, edition, location, domain)
         and hunt the official site's subpages (speakers, agenda, pricing, venue)
      3. Extract fields following the events-v1.1 schema
      4. Return ONE event JSON per record via submit_structured_events

    Args:
        source: Filter by scraper source field (e.g. "women_opportunities_aug_2026").
                If None, all records in the collection.
        limit: Maximum raw records to fetch (default 10, max 25).
        collection_name: Raw DB collection to read from. Defaults to "raw_urls"
                (the collection of URLs harvested from the women-opportunities
                text file); pass the pipeline collection name to reuse aggregation.

    Returns:
        Dictionary with the events prompt text and the raw records to process.
    """
    client_id = "get_records_for_events"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        from config.mongodb import get_raw_db

        prompt_text = load_prompt_text(PROMPT_EVENTS)
        raw_collection = get_raw_db()[collection_name]

        db_filter: dict = {}
        if source:
            db_filter["source"] = source

        records = list(
            raw_collection.find(db_filter).sort("insertedAt", -1).limit(min(int(limit), 25))
        )

        if not records:
            return {
                "success": True,
                "message": f"No records found in collection '{collection_name}'."
                + (f" Source filter: '{source}'." if source else ""),
                "records": [],
                "record_count": 0,
                "prompt_name": PROMPT_EVENTS,
                "prompt_text": None,
            }

        for r in records:
            r["_id"] = str(r["_id"])

        logger.info(
            f"Returning {len(records)} records for event structuring "
            f"(collection={collection_name}, source={source})"
        )

        update_metrics(True)
        return {
            "success": True,
            "record_count": len(records),
            "records": records,
            "prompt_name": PROMPT_EVENTS,
            "prompt_text": prompt_text,
            "usage": {
                "purpose": (
                    "Send each record to your LLM with prompt_text above. "
                    "Research and structure each target event following the "
                    "events-v1.1 schema. "
                    "Output ONE event document per input record."
                ),
                "expected_output": (
                    "A JSON object per event matching the events-v1.1 schema. "
                    "Submit them via submit_structured_events."
                ),
            },
        }

    except FileNotFoundError as e:
        logger.error(f"Prompt file error: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in get_records_for_events: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def submit_structured_events(
    events_json: str,
    dedupe_gate: bool = True,
    keep_metadata: bool = False,
) -> dict:
    """
    Submit structured event records (following the event-structuring-v1.1.txt
    events-v1.1 schema) produced by a chatbot and persist them to the Events
    collection.

    This is the event counterpart of submit_structured_records:

      - Deduplication key: source.name + title (upsert-in-place on re-submit).
      - Optional duplicate-title GATE (on by default): a record whose title
        matches an existing LIVE event — same normalized title from a different
        source, or a reworded title from the same source — is SKIPPED and
        reported under "duplicates".
      - events-v1.1 defaults are applied (type="event", status="draft",
        visibility="public", featured=false, analytics=0) and off-schema enum
        values are downgraded to null with a warning (see details).
      - The audit ``metadata`` block is stripped unless keep_metadata=True.

    Args:
        events_json: JSON string — either a single object or an array of
                     structured event objects following the events-v1.1 schema
        dedupe_gate: If True (default), block events that duplicate an
                     existing live event by normalized title. Set False to
                     force-insert (e.g. intentional re-ingest).
        keep_metadata: If True, retain each event's metadata audit block
                     (searchLog, fieldConfidence, discoveredEvents, ...).

    Returns:
        Dictionary with inserted/updated/duplicates/skipped/error counts
        and per-event validation warnings.
    """
    client_id = "submit_structured_events"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        # Parse the JSON
        try:
            parsed = json.loads(events_json)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid JSON: {e}"}

        # Normalize to a list
        events = parsed if isinstance(parsed, list) else [parsed]
        if not events:
            return {"success": False, "error": "Empty events array"}

        from pymongo import UpdateOne
        from config.mongodb import db

        target_collection = db[EVENT_COLLECTION]
        now_iso = datetime.now(timezone.utc).isoformat()

        # Preload the live-event title index once per call so the gate is
        # O(1) per record instead of a full scan per record.
        title_index = build_title_index(target_collection) if dedupe_gate else {}
        seen_in_batch: set = set()

        inserted = 0
        updated = 0
        skipped = 0
        duplicates = 0
        errors = 0
        error_details = []
        warnings_all: list = []
        operations = []

        for event in events:
            title = event.get("title", "")
            if not title or not str(title).strip():
                skipped += 1
                error_details.append("Event missing 'title'")
                continue

            title = str(title).strip()

            source_obj = event.get("source")
            source_name = ""
            if isinstance(source_obj, dict):
                name = source_obj.get("name")
                source_name = str(name).strip() if name else ""
            elif isinstance(source_obj, str):
                source_name = source_obj.strip()
            if not source_name:
                # Source name is the dedup dimension — fall back so we don't
                # merge unrelated events under an empty key.
                source_name = "unknown"

            normalized, warnings = _build_normalized_event(event, source_name, now_iso, keep_metadata)
            if warnings:
                warnings_all.extend(warnings)

            # ── Duplicate-title gate ──
            # Blocks when another LIVE event already exists with the same
            # normalized title (different source, or a reworded title from the
            # same source). A same-source exact-title match is the intended
            # update path and passes through. Also catches repeats within this
            # same batch.
            if dedupe_gate:
                norm_key = normalize_title(title)
                if norm_key and norm_key in seen_in_batch:
                    duplicates += 1
                    error_details.append(
                        f"Duplicate gate: '{title}' repeats an event already "
                        "submitted in this batch. Skipped (pass dedupe_gate=false to force)."
                    )
                    continue
                duplicate_matches = find_near_duplicates(
                    title_index, title, source_name, source_nested=True
                )
                if duplicate_matches:
                    duplicates += 1
                    dup_desc = "; ".join(
                        f"'{m['title']}' (source={m['source'] or '?'}, _id={m['_id']})"
                        for m in duplicate_matches[:3]
                    )
                    error_details.append(
                        f"Duplicate gate: '{title}' already exists as: {dup_desc}. "
                        "Skipped (pass dedupe_gate=false to force)."
                    )
                    continue
                if norm_key:
                    seen_in_batch.add(norm_key)

            # Compute dedup key (mirrors the contest pipeline)
            filter_key = {"source.name": source_name, "title": title}

            # createdAt is preserved on re-submits ($setOnInsert) while the
            # rest of the document refreshes via $set.
            created_at = normalized.pop("createdAt", now_iso)
            operations.append(
                UpdateOne(
                    filter_key,
                    {"$set": normalized, "$setOnInsert": {"createdAt": created_at}},
                    upsert=True,
                )
            )

        # Execute bulk upsert
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
                    error_details.append(f"Bulk write error on batch {i // batch_size}: {e}")

        update_metrics(True)
        return {
            "success": True,
            "total_submitted": len(events),
            "inserted": inserted,
            "updated": updated,
            "duplicates": duplicates,
            "skipped": skipped,
            "errors": errors,
            "event_warnings": len(warnings_all),
            "details": error_details[:10],
            "warnings": warnings_all[:20],
        }

    except Exception as e:
        logger.error(f"Error in submit_structured_events: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_events(
    event_type: Optional[str] = None,
    status: Optional[str] = None,
    upcoming_only: bool = False,
    limit: int = 20,
    skip: int = 0,
) -> dict:
    """
    Read structured events from the Events collection with filters.

    Use this to query events harvested by the pipeline — e.g. all upcoming
    conferences, or everything in draft status awaiting review.

    Args:
        event_type: Filter by events-v1.1 eventType
                    (conference, summit, workshop, webinar, meetup, expo,
                     trade_show, career_fair, networking_event,
                     training_program, festival)
        status: Filter by event status (published, draft, cancelled, archived)
        upcoming_only: If True, only return events whose eventDates.start is
                       in the future
        limit: Maximum events to return (max 100)
        skip: Number of events to skip for pagination

    Returns:
        Dictionary with matching events (JSON-safe), count and total.
    """
    client_id = "get_events"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        filter_dict: Dict[str, Any] = {}
        if event_type:
            filter_dict["eventType"] = event_type
        if status:
            filter_dict["status"] = status
        if upcoming_only:
            now_iso = datetime.now(timezone.utc).isoformat()
            filter_dict["eventDates.start"] = {"$gte": now_iso}

        result = DataManager.read_data(
            collection_name=EVENT_COLLECTION,
            filter_query=filter_dict,
            limit=min(max(int(limit), 1), 100),
            skip=max(int(skip), 0),
            sort_by="eventDates.start",
            sort_direction=-1,
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        events = result.get("data", [])
        update_metrics(True)
        return {
            "success": True,
            "count": len(events),
            "total": result.get("total", 0),
            "skip": result.get("skip", skip),
            "limit": result.get("limit", limit),
            "filters": {
                "event_type": event_type,
                "status": status,
                "upcoming_only": upcoming_only,
            },
            "events": _json_safe(events),
        }

    except Exception as e:
        logger.error(f"Error in get_events: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_events_overview() -> dict:
    """
    Get a quick overview of the Events collection.

    Returns counts by eventType and status, plus the number of upcoming
    events. Use this to decide what to review or process next.

    Returns:
        Dictionary with overview statistics.
    """
    client_id = "get_events_overview"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        from config.mongodb import db

        collection = db[EVENT_COLLECTION]

        total = collection.count_documents({})

        by_type = {
            s["_id"] or "unknown": s["count"]
            for s in collection.aggregate(
                [
                    {"$group": {"_id": "$eventType", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ]
            )
        }
        by_status = {
            s["_id"] or "unknown": s["count"]
            for s in collection.aggregate(
                [
                    {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ]
            )
        }

        now_iso = datetime.now(timezone.utc).isoformat()
        upcoming = collection.count_documents({"eventDates.start": {"$gte": now_iso}})

        update_metrics(True)
        return {
            "success": True,
            "collection": EVENT_COLLECTION,
            "total_events": total,
            "upcoming_events": upcoming,
            "by_event_type": by_type,
            "by_status": by_status,
            "usage": {
                "purpose": (
                    "Use this overview to decide which events to review. "
                    "Then call get_events(event_type=..., status=..., "
                    "upcoming_only=...) to read them, or "
                    "get_records_for_events() to harvest more from raw URLs."
                ),
            },
        }

    except Exception as e:
        logger.error(f"Error in get_events_overview: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ── Event detail generation pipeline ───────────────────────────────────────


@mcp.tool()
def get_events_for_detail_generation(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Return events needing AI-generated detail pages, sorted by priority.

    The response includes both the prompt text (event-details-v1.0.txt) and
    the event documents. Send both to the LLM so it can research and generate
    structured event details (whyAttend, whoShouldAttend, benefits, tips,
    agenda highlights, FAQ, SEO).

    Priority order: upcoming > published > registration open > has speakers/
    agenda > recently added.

    Args:
        batch_size: Number of events to return (default 10, max 50)
        skip: Number of events to skip (for pagination)

    Returns:
        Dictionary with prompt_text, events list, and queue metadata
    """
    client_id = "get_events_for_detail_generation"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(
            f"Building event detail generation bundle (batch_size={batch_size}, skip={skip})"
        )

        prompt_text = load_prompt_text(PROMPT_EVENT_DETAILS)

        generator = EventDetailGenerator()
        queue_result = generator.get_priority_queue(
            batch_size=min(int(batch_size), 50),
            skip=max(int(skip), 0),
        )

        if not queue_result.get("success"):
            update_metrics(False)
            return queue_result

        events = _json_safe(queue_result.get("events", []))

        result = {
            "success": True,
            "prompt_name": PROMPT_EVENT_DETAILS,
            "prompt_text": prompt_text,
            "event_count": len(events),
            "total_needing_generation": queue_result.get("total_needing_generation", 0),
            "skip": queue_result.get("skip", skip),
            "batch_size": queue_result.get("batch_size", batch_size),
            "events": events,
            "usage": {
                "purpose": "Send prompt_text + event(s) to your LLM. "
                "It will use its web search to research each event "
                "(speakers, agenda, pricing, venue subpages) and return "
                "structured JSON. Submit results via submit_event_details.",
                "expected_llm_output": (
                    "A JSON object per event matching the schema in "
                    "event-details-v1.0.txt"
                ),
            },
        }

        update_metrics(True)
        return result

    except FileNotFoundError as e:
        logger.error(f"Prompt file error: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in get_events_for_detail_generation: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def submit_event_details(
    event_id: str,
    details_json: str,
) -> Dict[str, Any]:
    """
    Submit AI-generated event details (from the LLM) for validation and storage.

    The details are validated for quality (minimum word count, honest
    readingTime, no first-person, no hallucinated URLs), then saved to the
    event_details collection with automatic versioning.

    Args:
        event_id: The MongoDB ObjectId of the event (from the Events collection)
        details_json: JSON string matching the event-details-v1.0.txt schema

    Returns:
        Dictionary with validation results, version info, and any warnings
    """
    client_id = "submit_event_details"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        # Parse the JSON
        try:
            parsed = json.loads(details_json)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid JSON: {e}"}

        if not isinstance(parsed, dict):
            return {"success": False, "error": "Expected a JSON object, got array"}

        # Fetch the event document for validation context
        from bson.objectid import ObjectId
        from config.mongodb import db

        event_oid = ObjectId(event_id)
        event_data = db[EVENT_COLLECTION].find_one({"_id": event_oid})

        if not event_data:
            return {"success": False, "error": f"Event {event_id} not found"}

        # Validate
        generator = EventDetailGenerator()
        validation = generator.validate(parsed, event_data)

        if not validation.get("valid"):
            logger.warning(
                f"Event {event_id} failed validation: "
                f"{validation.get('warning_count')} warnings"
            )

        # ── Reject truly empty content before saving ──
        # Content is empty if:
        #   - total_words < 50 (essentially no meaningful text), OR
        #   - only readingTime exists and nothing else meaningful
        total_words = validation.get("total_words", 0)
        validated_content = validation.get("content", parsed.get("content", {}))
        meaningful_keys = [k for k in validated_content.keys() if k != "readingTime"]
        is_empty_content = total_words < 50 or len(meaningful_keys) == 0

        if is_empty_content:
            logger.warning(
                f"Event {event_id} rejected: content too sparse "
                f"({total_words} words, {len(meaningful_keys)} meaningful keys)"
            )
            update_metrics(False)
            return {
                "success": False,
                "error": (
                    f"Generated content too sparse ({total_words} words, "
                    f"{len(meaningful_keys)} meaningful sections). "
                    f"The AI could not find enough information to generate "
                    f"event details. Review the research step and try again."
                ),
                "validation": {
                    "valid": validation.get("valid", False),
                    "warning_count": validation["warning_count"],
                    "warnings": validation["warnings"],
                    "total_words": total_words,
                },
            }

        # Save validated content
        save_result = generator.save(
            event_id=event_id,
            content=validated_content,
            seo=validation.get("seo", parsed.get("seo", {})),
            warnings=validation.get("warnings", []),
        )

        if not save_result.get("success"):
            update_metrics(False)
            return save_result

        result = {
            "success": True,
            "version": save_result["version"],
            "is_new": save_result["is_new"],
            "quality_score": save_result["quality_score"],
            "validation": {
                "valid": validation.get("valid", False),
                "warning_count": validation["warning_count"],
                "warnings": validation["warnings"],
                "total_words": validation["total_words"],
            },
        }

        update_metrics(True)
        return result

    except Exception as e:
        logger.error(f"Error in submit_event_details: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_event_detail_status() -> Dict[str, Any]:
    """
    Get coverage metrics for the event detail generation pipeline.

    Surfaces EventDetailGenerator.get_status: how many live events exist in
    the Events collection, how many already have event_details documents
    (broken down by detail status), how many still need generation, and the
    overall coverage percentage.

    Use this to see how much event-detail work remains before deciding how
    many batches of get_events_for_detail_generation to run.

    Returns:
        Dictionary with total_events, total_with_details,
        total_without_details, by_status, and coverage_pct.
    """
    client_id = "get_event_detail_status"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        generator = EventDetailGenerator()
        status = generator.get_status()

        if not status.get("success"):
            update_metrics(False)
            return status

        result = {
            "success": True,
            "collection": EVENT_DETAILS_COLLECTION,
            "total_events": status.get("total_events", 0),
            "total_with_details": status.get("total_with_details", 0),
            "total_without_details": status.get("total_without_details", 0),
            "by_status": status.get("by_status", {}),
            "coverage_pct": status.get("coverage_pct", 0.0),
            "usage": {
                "purpose": (
                    "Shows how many live events still need AI-generated detail "
                    "pages. If total_without_details > 0, call "
                    "get_events_for_detail_generation(batch_size=10) to fetch "
                    "the next batch plus the event-details-v1.0.txt prompt, then "
                    "submit results via submit_event_details."
                ),
            },
        }

        update_metrics(True)
        return result

    except Exception as e:
        logger.error(f"Error in get_event_detail_status: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
