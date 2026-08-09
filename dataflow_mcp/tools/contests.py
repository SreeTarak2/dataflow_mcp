"""Contest pipeline MCP tools.

Two ways to publish a contest from raw scraped data:

* Separate pipeline: ``get_records_for_structuring`` → ``submit_structured_records``
  then ``get_contests_for_detail_generation`` → ``submit_contest_details``.
* Full generation (one AI round-trip): ``get_records_for_full_generation`` →
  ``submit_full_generation``.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dataflow_mcp.core import (
    mcp,
    logger,
    check_rate_limit,
    update_metrics,
    load_prompt_text,
    PROMPT_CONTEST_STRUCTURING,
    PROMPT_CONTEST_DETAILS,
    _json_safe,
    _build_normalized_record,
)
from tools.contest_detail_generator import ContestDetailGenerator
from tools.dedup_gate import build_title_index, find_near_duplicates, normalize_title


# ── Structuring pipeline ─────────────────────────────────────────────────


@mcp.tool()
def get_records_for_structuring(
    source: Optional[str] = None,
    limit: int = 5,
    require_validated: bool = False,
) -> dict:
    """
    Fetch raw scraped records + the structuring prompt
    (contest-structuring-v4.0.txt, v4.0 schema) so a chatbot can structure
    them into the normalized Contests format.

    The chatbot should:
      1. Read the prompt_text for schema and rules
      2. For each record, use its URL (or title) to search the web and find
         the actual contest page
      3. Extract fields following the v4.0 schema
      4. Return a JSON array of structured records via submit_structured_records

    Args:
        source: Filter by scraper source (e.g. "contestwatchers"). If None, all sources.
        limit: Maximum raw records to fetch (default 5, max 20)
        require_validated: If True, only fetch records with validationStatus="validated"

    Returns:
        Dictionary with prompt_text, records, and usage instructions
    """
    client_id = "get_records_for_structuring"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        from config.mongodb import get_raw_db, RAW_COLLECTION

        prompt_text = load_prompt_text(PROMPT_CONTEST_STRUCTURING)
        raw_collection = get_raw_db()[RAW_COLLECTION]

        db_filter: dict = {}
        if source:
            db_filter["source"] = source
        if require_validated:
            db_filter["validationStatus"] = "validated"

        records = list(
            raw_collection.find(db_filter).sort("scrapedAt", -1).limit(min(int(limit), 20))
        )

        if not records:
            msg = (
                "No validated raw records found." if require_validated else "No raw records found."
            )
            if source:
                msg += f" Source filter: '{source}'."
            return {
                "success": True,
                "message": msg,
                "records": [],
                "record_count": 0,
                "prompt_text": None,
            }

        # Convert ObjectId to strings
        for r in records:
            r["_id"] = str(r["_id"])

        logger.info(f"Returning {len(records)} records for structuring (source={source})")

        update_metrics(True)
        return {
            "success": True,
            "record_count": len(records),
            "records": records,
            "prompt_name": PROMPT_CONTEST_STRUCTURING,
            "prompt_text": prompt_text,
            "usage": {
                "purpose": "Send each record to your LLM with the prompt_text. The LLM should use web search to find the official contest page, then extract fields following the v4.0 schema.",
                "expected_output": "A JSON array of structured contest objects. Submit via submit_structured_records.",
            },
        }

    except FileNotFoundError as e:
        logger.error(f"Prompt file error: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in get_records_for_structuring: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def submit_structured_records(
    records_json: str,
    dedupe_gate: bool = True,
) -> dict:
    """
    Submit structured contest records (following the contest-structuring-v4.0.txt
    schema) produced by a chatbot. Validates required fields and upserts into
    the Contests collection.

    Deduplication key: source.name + title (same as process_raw_data).
    Plus an optional duplicate-title GATE (on by default): any record whose
    title matches an existing LIVE contest — same normalized title from a
    different source, or a reworded title from the same source — is SKIPPED
    and reported under "duplicates". The same-source exact-title match is the
    intended update path and still updates in place.

    Args:
        records_json: JSON string — either a single object or an array of
                      structured contest objects following the v4.0 schema
        dedupe_gate: If True (default), block records that duplicate an
                     existing live contest by normalized title. Set False to
                     force-insert (e.g. intentional re-ingest).

    Returns:
        Dictionary with inserted/updated/duplicates/skipped/error counts
    """
    client_id = "submit_structured_records"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        # Parse the JSON
        try:
            parsed = json.loads(records_json)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid JSON: {e}"}

        # Normalize to a list
        records = parsed if isinstance(parsed, list) else [parsed]
        if not records:
            return {"success": False, "error": "Empty records array"}

        from pymongo import UpdateOne
        from config.mongodb import db

        target_collection = db[os.getenv("COLLECTION_NAME", "Contests")]
        now_iso = datetime.now(timezone.utc).isoformat()

        # Preload the live-contest title index once per call so the gate is
        # O(1) per record instead of a full scan per record.
        title_index = build_title_index(target_collection) if dedupe_gate else {}
        seen_in_batch: set = set()

        inserted = 0
        updated = 0
        skipped = 0
        duplicates = 0
        errors = 0
        error_details = []
        operations = []

        for record in records:
            title = record.get("title", "")
            if not title:
                skipped += 1
                error_details.append("Record missing 'title'")
                continue

            link = record.get("link", "")
            if not link:
                skipped += 1
                error_details.append(f"Record '{title}' missing 'link'")
                continue

            source_obj = record.get("source", {})
            source_name = (
                source_obj.get("name", "") if isinstance(source_obj, dict) else str(source_obj)
            )

            normalized = _build_normalized_record(record, source_name, now_iso)

            # ── Duplicate-title gate ──
            # Blocks a record when another LIVE contest already exists with the
            # same normalized title (different source, or a reworded title from
            # the same source). A same-source exact-title match is the intended
            # update path and passes through. Also catches repeats within this
            # same batch.
            if dedupe_gate:
                norm_key = normalize_title(title)
                if norm_key and norm_key in seen_in_batch:
                    duplicates += 1
                    error_details.append(
                        f"Duplicate gate: '{title}' repeats a record already "
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

            # Compute dedup key
            dedup_source = source_name if source_name else "unknown"
            filter_key = {"source.name": dedup_source, "title": title}

            operations.append(
                UpdateOne(
                    filter_key,
                    {"$set": normalized},
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
            "total_submitted": len(records),
            "inserted": inserted,
            "updated": updated,
            "duplicates": duplicates,
            "skipped": skipped,
            "errors": errors,
            "details": error_details[:10],
        }

    except Exception as e:
        logger.error(f"Error in submit_structured_records: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ── Full generation pipeline (raw → structured + details in one pass) ────


@mcp.tool()
def get_records_for_full_generation(
    source: Optional[str] = None,
    limit: int = 3,
    require_validated: bool = False,
) -> dict:
    """
    Fetch raw scraped records + BOTH prompts (contest-structuring-v4.0.txt +
    contest-details-v1.0.txt) so a chatbot can structure AND generate contest
    details in one pass.

    Use this when you want to go from raw scraped data to published contest details
    in a single AI round-trip. The chatbot should:
      1. Read BOTH prompt texts (structuring schema + detail generation rules)
      2. For each record, use its URL (or title) to search the web and find
         the actual contest page
      3. Extract structured fields following the v4.0 schema
      4. Research and generate contest details following contest-details-v1.0.txt
      5. Return both via submit_full_generation

    Args:
        source: Filter by scraper source (e.g. "contestwatchers"). If None, all sources.
        limit: Maximum raw records to fetch (default 3, max 10)
        require_validated: If True, only fetch records with validationStatus="validated"

    Returns:
        Dictionary with both prompts, raw records, and usage instructions
    """
    client_id = "get_records_for_full_generation"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        from config.mongodb import get_raw_db, RAW_COLLECTION

        structuring_prompt = load_prompt_text(PROMPT_CONTEST_STRUCTURING)
        details_prompt = load_prompt_text(PROMPT_CONTEST_DETAILS)
        raw_collection = get_raw_db()[RAW_COLLECTION]

        db_filter: dict = {}
        if source:
            db_filter["source"] = source
        if require_validated:
            db_filter["validationStatus"] = "validated"

        records = list(
            raw_collection.find(db_filter).sort("scrapedAt", -1).limit(min(int(limit), 10))
        )

        if not records:
            msg = (
                "No validated raw records found." if require_validated else "No raw records found."
            )
            if source:
                msg += f" Source filter: '{source}'."
            return {
                "success": True,
                "message": msg,
                "records": [],
                "record_count": 0,
                "structuring_prompt": None,
                "details_prompt": None,
            }

        # Convert ObjectId to strings
        for r in records:
            r["_id"] = str(r["_id"])

        logger.info(f"Returning {len(records)} records for full generation (source={source})")

        update_metrics(True)
        return {
            "success": True,
            "record_count": len(records),
            "records": records,
            "structuring_prompt_name": PROMPT_CONTEST_STRUCTURING,
            "structuring_prompt": structuring_prompt,
            "details_prompt_name": PROMPT_CONTEST_DETAILS,
            "details_prompt": details_prompt,
            "usage": {
                "purpose": (
                    "Send each record to your LLM with BOTH prompts above. "
                    "First structure the record using contest-structuring-v4.0.txt schema, "
                    "then use web search to research and generate contest details "
                    "following contest-details-v1.0.txt. "
                    "Submit both as a combined result via submit_full_generation."
                ),
                "expected_output": (
                    "A JSON object with 'items' array, each item having "
                    "'record' (structured contest data) and 'details' (contest details). "
                    "Submit via submit_full_generation."
                ),
            },
        }

    except FileNotFoundError as e:
        logger.error(f"Prompt file error: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in get_records_for_full_generation: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def submit_full_generation(
    generation_json: str,
    dedupe_gate: bool = True,
) -> dict:
    """
    Submit a full generation result that includes BOTH structured contest data
    AND contest details in one call.

    Use this after get_records_for_full_generation. The JSON must contain an
    'items' array, where each item has:
      - record: Structured contest data following the v4.0 schema
      - details: Contest details following contest-details-v1.0.txt schema

    This tool:
      1. Upserts each structured record into the Contests collection
         (same dedup logic as submit_structured_records, plus the
          duplicate-title gate — blocked items are reported with
          "duplicate": true and their details are NOT saved)
      2. Finds the resulting contest _id by dedup key
      3. Validates and saves contest_details for each

    Args:
        generation_json: JSON string with format:
            {
              "items": [
                {
                  "record": { ... structured contest object ... },
                  "details": { ... contest details object ... }
                }
              ]
            }
        dedupe_gate: If True (default), skip items whose title matches an
                     existing LIVE contest (normalized-title match from a
                     different source or reworded title from the same source).
                     Set False to force-insert.

    Returns:
        Dictionary with per-item results and summary counts
    """
    client_id = "submit_full_generation"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        # Parse the JSON
        try:
            parsed = json.loads(generation_json)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid JSON: {e}"}

        items = parsed.get("items", [])
        if not isinstance(items, list) or not items:
            return {
                "success": False,
                "error": "generation_json must contain an 'items' array with at least one item",
            }

        from pymongo import UpdateOne
        from bson.objectid import ObjectId
        from config.mongodb import db

        target_collection = db[os.getenv("COLLECTION_NAME", "Contests")]
        now_iso = datetime.now(timezone.utc).isoformat()
        generator = ContestDetailGenerator()

        # Preload the live-contest title index once per call for the gate.
        title_index = build_title_index(target_collection) if dedupe_gate else {}
        seen_in_batch: set = set()

        structured_results = []
        total_success = 0
        total_duplicates = 0
        total_errors = 0
        error_details = []

        for idx, item in enumerate(items):
            record = item.get("record", {})
            details = item.get("details", {})
            item_result = {"index": idx}

            # ── Step 1: Upsert structured record into Contests ──

            title = record.get("title", "")
            if not title:
                item_result["error"] = "Record missing 'title'"
                error_details.append(f"Item {idx}: record missing 'title'")
                total_errors += 1
                structured_results.append(item_result)
                continue

            link = record.get("link", "")
            if not link:
                item_result["error"] = f"Record '{title}' missing 'link'"
                error_details.append(f"Item {idx}: '{title}' missing 'link'")
                total_errors += 1
                structured_results.append(item_result)
                continue

            source_obj = record.get("source", {})
            source_name = (
                source_obj.get("name", "") if isinstance(source_obj, dict) else str(source_obj)
            )

            normalized = _build_normalized_record(record, source_name, now_iso)

            # ── Duplicate-title gate ──
            # Same semantics as submit_structured_records: block when another
            # LIVE contest already exists with the same normalized title.
            # Same-source exact-title matches (re-submits) pass through.
            if dedupe_gate:
                norm_key = normalize_title(title)
                if norm_key and norm_key in seen_in_batch:
                    item_result["duplicate"] = True
                    item_result["duplicate_of"] = "repeats a record in this batch"
                    item_result["title"] = title
                    error_details.append(
                        f"Item {idx}: duplicate gate blocked '{title}' (repeats this batch)"
                    )
                    total_duplicates += 1
                    structured_results.append(item_result)
                    continue
                duplicate_matches = find_near_duplicates(
                    title_index, title, source_name, source_nested=True
                )
                if duplicate_matches:
                    item_result["duplicate"] = True
                    item_result["duplicate_of"] = [
                        {
                            "_id": m["_id"],
                            "title": m["title"],
                            "source": m["source"],
                            "link": m["link"],
                        }
                        for m in duplicate_matches[:3]
                    ]
                    item_result["title"] = title
                    dup_desc = "; ".join(
                        f"'{m['title']}' (source={m['source'] or '?'}, _id={m['_id']})"
                        for m in duplicate_matches[:3]
                    )
                    error_details.append(
                        f"Item {idx}: duplicate gate blocked '{title}' — already exists as: {dup_desc}"
                    )
                    total_duplicates += 1
                    structured_results.append(item_result)
                    continue
                if norm_key:
                    seen_in_batch.add(norm_key)

            # Dedup key for upsert
            dedup_source = source_name if source_name else "unknown"
            filter_key = {"source.name": dedup_source, "title": title}

            try:
                upsert_result = target_collection.update_one(
                    filter_key,
                    {"$set": normalized},
                    upsert=True,
                )

                # Get the contest _id (either the upserted_id or find existing)
                if upsert_result.upserted_id:
                    contest_id = str(upsert_result.upserted_id)
                else:
                    existing = target_collection.find_one(filter_key, {"_id": 1})
                    contest_id = str(existing["_id"]) if existing else ""

                if not contest_id:
                    item_result["error"] = f"Contest '{title}' could not be found after upsert"
                    error_details.append(f"Item {idx}: '{title}' not found after upsert")
                    total_errors += 1
                    structured_results.append(item_result)
                    continue

                item_result["contest_id"] = contest_id
                item_result["is_new"] = upsert_result.upserted_id is not None
                item_result["title"] = title

            except Exception as e:
                item_result["error"] = f"Upsert failed for '{title}': {e}"
                error_details.append(f"Item {idx}: upsert failed for '{title}': {e}")
                total_errors += 1
                structured_results.append(item_result)
                continue

            # ── Step 2: Save contest_details ──

            if not isinstance(details, dict) or not details:
                item_result["details_warning"] = "No details provided, skipping details save"
                structured_results.append(item_result)
                total_success += 1
                continue

            try:
                # Fetch the contest document for validation context
                contest_oid = ObjectId(contest_id)
                contest_data = target_collection.find_one({"_id": contest_oid})

                if not contest_data:
                    item_result["details_error"] = f"Contest {contest_id} not found after upsert"
                    error_details.append(f"Item {idx}: contest {contest_id} not found after upsert")
                    total_errors += 1
                    continue

                # Validate the details
                validation = generator.validate(details, contest_data)

                if not validation.get("valid"):
                    logger.warning(
                        f"Contest {contest_id} failed validation: "
                        f"{validation.get('warning_count')} warnings"
                    )

                # Check for empty content
                total_words = validation.get("total_words", 0)
                validated_content = validation.get("content", details.get("content", {}))
                meaningful_keys = [k for k in validated_content.keys() if k != "readingTime"]
                is_empty_content = total_words < 50 or len(meaningful_keys) == 0

                if is_empty_content:
                    item_result["details_warning"] = (
                        f"Details too sparse ({total_words} words, "
                        f"{len(meaningful_keys)} meaningful keys)"
                    )
                    structured_results.append(item_result)
                    total_success += 1
                    continue

                # Save validated content
                save_result = generator.save(
                    contest_id=contest_id,
                    content=validated_content,
                    seo=validation.get("seo", details.get("seo", {})),
                    warnings=validation.get("warnings", []),
                )

                if save_result.get("success"):
                    item_result["version"] = save_result["version"]
                    item_result["details_saved"] = True
                    total_success += 1
                else:
                    item_result["details_error"] = save_result.get("error", "Unknown save error")
                    error_details.append(
                        f"Item {idx}: details save failed for '{title}': {save_result.get('error')}"
                    )
                    total_errors += 1
                    continue

            except Exception as e:
                item_result["details_error"] = str(e)
                error_details.append(f"Item {idx}: details error for '{title}': {e}")
                total_errors += 1
                continue

            structured_results.append(item_result)

        # Gate blocks are successful handling (not failures) — count them as success.
        update_metrics(total_success > 0 or total_duplicates > 0)
        return {
            "success": True,
            "total_items": len(items),
            "successful": total_success,
            "duplicates": total_duplicates,
            "errors": total_errors,
            "results": structured_results,
            "error_details": error_details[:10],
        }

    except Exception as e:
        logger.error(f"Error in submit_full_generation: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ── Detail generation pipeline ───────────────────────────────────────────


@mcp.tool()
def get_contests_for_detail_generation(
    batch_size: int = 11,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Return contests needing AI-generated detail pages, sorted by priority.

    The response includes both the prompt text (contest-details-v1.0.txt)
    and the contest documents. Send both to Mistral so it can research and
    generate structured contest details.

    Priority order: trending > open > high view velocity > recently added > prize value.

    Args:
        batch_size: Number of contests to return (default 11, max 50)
        skip: Number of contests to skip (for pagination)

    Returns:
        Dictionary with prompt_text, contests list, and queue metadata
    """
    client_id = "get_contests_for_detail_generation"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(
            f"Building contest detail generation bundle (batch_size={batch_size}, skip={skip})"
        )

        prompt_text = load_prompt_text(PROMPT_CONTEST_DETAILS)

        generator = ContestDetailGenerator()
        queue_result = generator.get_priority_queue(
            batch_size=min(int(batch_size), 50),
            skip=max(int(skip), 0),
        )

        if not queue_result.get("success"):
            update_metrics(False)
            return queue_result

        contests = _json_safe(queue_result.get("contests", []))

        result = {
            "success": True,
            "prompt_name": PROMPT_CONTEST_DETAILS,
            "prompt_text": prompt_text,
            "contest_count": len(contests),
            "total_needing_generation": queue_result.get("total_needing_generation", 0),
            "skip": queue_result.get("skip", skip),
            "batch_size": queue_result.get("batch_size", batch_size),
            "contests": contests,
            "usage": {
                "purpose": "Send prompt_text + contest(s) to Mistral. "
                "It will use its web search to research each contest "
                "and return structured JSON. Submit results via "
                "submit_contest_details.",
                "expected_llm_output": (
                    "A JSON object per contest matching the schema in contest-details-v1.0.txt"
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
        logger.error(f"Error in get_contests_for_detail_generation: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def submit_contest_details(
    contest_id: str,
    details_json: str,
) -> Dict[str, Any]:
    """
    Submit AI-generated contest details (from Mistral) for validation and storage.

    The details are validated for quality, then saved to the contest_details
    collection with automatic versioning.

    Args:
        contest_id: The MongoDB ObjectId of the contest
        details_json: JSON string matching the contest-details-v1.0.txt schema

    Returns:
        Dictionary with validation results, version info, and any warnings
    """
    client_id = "submit_contest_details"

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

        # Fetch the contest document for validation context
        from bson.objectid import ObjectId
        from config.mongodb import db

        contest_oid = ObjectId(contest_id)
        contest_data = db[os.getenv("COLLECTION_NAME", "Contests")].find_one({"_id": contest_oid})

        if not contest_data:
            return {"success": False, "error": f"Contest {contest_id} not found"}

        # Validate
        generator = ContestDetailGenerator()
        validation = generator.validate(parsed, contest_data)

        if not validation.get("valid"):
            logger.warning(
                f"Contest {contest_id} failed validation: "
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
                f"Contest {contest_id} rejected: content too sparse "
                f"({total_words} words, {len(meaningful_keys)} meaningful keys)"
            )
            update_metrics(False)
            return {
                "success": False,
                "error": (
                    f"Generated content too sparse ({total_words} words, "
                    f"{len(meaningful_keys)} meaningful sections). "
                    f"The AI could not find enough information to generate "
                    f"contest details. Review the research step and try again."
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
            contest_id=contest_id,
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
        logger.error(f"Error in submit_contest_details: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
