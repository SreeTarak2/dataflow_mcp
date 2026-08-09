"""Web validation pipeline MCP tools (chatbot-driven).

Architecture:
   1. get_records_for_validation()  — fetches unvalidated records + prompt for chatbot
   2. Chatbot does web search on its own, returns JSON validation results
   3. submit_raw_validation()       — chatbot submits its validation results
   4. submit_contest_validation()   — same for contests
   5. get_validation_status()       — check progress
   6. get_validation_prompt()       — preview the prompt without claiming records

Multiple chatbots can work in parallel by passing different chatbot_id strings.
"""

from typing import Optional

from dataflow_mcp.core import (
    mcp,
    logger,
    check_rate_limit,
    update_metrics,
    load_prompt_text,
    DEFAULT_COLLECTION,
    PROMPT_VALIDATION,
)
from tools import web_validator


@mcp.tool()
def get_records_for_validation(
    source: str,
    limit: int = 5,
    chatbot_id: str = "default",
) -> dict:
    """
    Claim a batch of unvalidated raw records and return them with a
    validation prompt for a chatbot.

    The chatbot uses its OWN web search capability to verify each record
    by visiting the source URL or searching the web for the contest title.

    Records are atomically marked as 'in_progress' for this chatbot_id,
    preventing other chatbots from claiming the same records.

    Args:
        source: Scraper source name (e.g. "contestwatchers", "opportunityDesk")
        limit: Max records to claim (default 5, max 25)
        chatbot_id: Identifier for the chatbot doing the validation.
                    Use different IDs ("claude-1", "gpt-4", etc.) for
                    parallel processing across multiple chatbots.

    Returns:
        Dictionary with:
          - validation_prompt: The full prompt text to send to the chatbot
          - records: The raw records to validate (with _id for submitting results)
          - chatbot_id: The chatbot ID these records are claimed for
          - claimed_count: Number of records claimed
    """
    try:
        check_rate_limit("get_records_for_validation")
        logger.info(f"Claiming raw records for validation: source={source}, chatbot={chatbot_id}")

        from config.mongodb import get_raw_db, RAW_COLLECTION

        raw_collection = get_raw_db()[RAW_COLLECTION]
        limit = min(int(limit), 25)

        # Atomically claim pending records using find_one_and_update
        claimed_records = []
        for _ in range(limit):
            record = raw_collection.find_one_and_update(
                filter=web_validator.get_next_pending_filter(source, chatbot_id),
                update=web_validator.get_next_pending_update(chatbot_id),
                sort={"scrapedAt": -1},
            )
            if record is None:
                break
            # Convert ObjectId to string
            record["_id"] = str(record["_id"])
            claimed_records.append(record)

        if not claimed_records:
            return {
                "success": True,
                "source": source,
                "chatbot_id": chatbot_id,
                "message": f"No unvalidated records found for source '{source}'. All records may already be claimed or validated.",
                "claimed_count": 0,
                "records": [],
                "validation_prompt": None,
            }

        # Build the validation prompt
        prompt_text = load_prompt_text(PROMPT_VALIDATION)
        record_section = ""
        for i, rec in enumerate(claimed_records):
            record_section += (
                f"--- Record {i + 1} ---\n"
                f"record_id: {rec.get('_id', '')}\n"
                f"title: {rec.get('title', '<untitled>')}\n"
                f"source: {rec.get('source', 'unknown')}\n"
                f"url: {rec.get('url', '')}\n"
                f"deadline: {rec.get('deadline', 'not specified')}\n"
                f"prize: {rec.get('prize', 'not specified')}\n"
                f"description: {str(rec.get('description', ''))[:200]}\n\n"
            )

        full_prompt = (
            prompt_text
            + record_section
            + "\n"
            + (
                "Now validate each record above and return ONLY a JSON object "
                "with the 'validations' array. Do not include any text outside the JSON."
            )
        )

        update_metrics(True)
        return {
            "success": True,
            "source": source,
            "chatbot_id": chatbot_id,
            "claimed_count": len(claimed_records),
            "records": claimed_records,
            "validation_prompt": full_prompt,
            "usage": {
                "purpose": "Send the validation_prompt + records to your chatbot. The chatbot will use its own web search to verify each record. Then call submit_raw_validation with the chatbot's JSON response.",
                "expected_chatbot_output": "A JSON object with a 'validations' array, each item having record_id, status, confidence, issues, details",
            },
        }

    except Exception as e:
        logger.error(f"Error in get_records_for_validation: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def submit_raw_validation(
    chatbot_id: str,
    validation_json: str,
) -> dict:
    """
    Submit validation results from a chatbot for raw scraped records.

    The chatbot should have received records via `get_records_for_validation`,
    validated them using its own web search, and returned a JSON response.
    This tool processes that JSON and updates each record's validation status
    in the database.

    Args:
        chatbot_id: The chatbot identifier that matches get_records_for_validation
        validation_json: The JSON response from the chatbot containing
                        the 'validations' array

    Returns:
        Dictionary with update results and summary
    """
    try:
        check_rate_limit("submit_raw_validation")
        logger.info(f"Submitting raw validation from chatbot={chatbot_id}")

        from config.mongodb import get_raw_db, RAW_COLLECTION

        raw_collection = get_raw_db()[RAW_COLLECTION]

        # Parse and normalize validation results
        result = web_validator.process_validation_results(validation_json)

        if not result["success"]:
            update_metrics(False)
            return {
                "success": False,
                "error": "Failed to parse validation results",
                "details": result["errors"],
            }

        # Update each record in the database
        updated_count = 0
        error_count = 0
        error_details = []

        for validation in result["validations"]:
            record_id = validation.get("record_id", "")
            if not record_id:
                error_count += 1
                error_details.append("Validation missing record_id")
                continue

            try:
                from bson.objectid import ObjectId

                doc_id = ObjectId(record_id)
            except Exception:
                error_count += 1
                error_details.append(f"Invalid record_id format: {record_id}")
                continue

            update_doc = web_validator.build_validation_update(record_id, validation, chatbot_id)

            try:
                update_result = raw_collection.update_one(
                    {"_id": doc_id},
                    {"$set": update_doc},
                )
                if update_result.matched_count > 0:
                    updated_count += 1
                else:
                    error_count += 1
                    error_details.append(f"Record not found: {record_id}")
            except Exception as e:
                error_count += 1
                error_details.append(f"DB error for {record_id}: {e}")

        update_metrics(True)
        return {
            "success": True,
            "chatbot_id": chatbot_id,
            "total_validations": len(result["validations"]),
            "updated": updated_count,
            "errors": error_count,
            "summary": result["summary"],
            "validations": result["validations"],
            "error_details": error_details[:5],
        }

    except Exception as e:
        logger.error(f"Error in submit_raw_validation: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def submit_contest_validation(
    chatbot_id: str,
    validation_json: str,
) -> dict:
    """
    Submit validation results from a chatbot for existing contest documents.

    Same as submit_raw_validation but updates the Contests collection.
    Use this for Stage 2 validation before LLM normalization.

    Args:
        chatbot_id: The chatbot identifier
        validation_json: The JSON response from the chatbot with 'validations' array

    Returns:
        Dictionary with update results and summary
    """
    try:
        check_rate_limit("submit_contest_validation")
        logger.info(f"Submitting contest validation from chatbot={chatbot_id}")

        from config.mongodb import db

        target_collection = db[DEFAULT_COLLECTION]

        result = web_validator.process_validation_results(validation_json)

        if not result["success"]:
            update_metrics(False)
            return {
                "success": False,
                "error": "Failed to parse validation results",
                "details": result["errors"],
            }

        updated_count = 0
        error_count = 0
        error_details = []

        for validation in result["validations"]:
            contest_id = validation.get("record_id", "")
            if not contest_id:
                error_count += 1
                continue

            try:
                from bson.objectid import ObjectId

                doc_id = ObjectId(contest_id)
            except Exception:
                error_count += 1
                error_details.append(f"Invalid contest_id: {contest_id}")
                continue

            update_doc = web_validator.build_validation_update(contest_id, validation, chatbot_id)

            try:
                update_result = target_collection.update_one(
                    {"_id": doc_id},
                    {"$set": update_doc},
                )
                if update_result.matched_count > 0:
                    updated_count += 1
                else:
                    error_count += 1
                    error_details.append(f"Contest not found: {contest_id}")
            except Exception as e:
                error_count += 1
                error_details.append(f"DB error for {contest_id}: {e}")

        update_metrics(True)
        return {
            "success": True,
            "chatbot_id": chatbot_id,
            "total_validations": len(result["validations"]),
            "updated": updated_count,
            "errors": error_count,
            "summary": result["summary"],
            "validations": result["validations"],
            "error_details": error_details[:5],
        }

    except Exception as e:
        logger.error(f"Error in submit_contest_validation: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_validation_status(source: Optional[str] = None) -> dict:
    """
    Get an overview of the validation pipeline status.

    Shows how many raw records are pending, in progress, validated,
    failed, or skipped — broken down by source if specified.

    Use this to monitor progress across multiple chatbots and
    decide when to run process_raw_data.

    Args:
        source: Optional scraper source to filter by

    Returns:
        Dictionary with status counts by validation status
    """
    try:
        check_rate_limit("get_validation_status")
        logger.info(f"Validation status requested for source={source}")

        from config.mongodb import get_raw_db, RAW_COLLECTION

        raw_collection = get_raw_db()[RAW_COLLECTION]

        base_filter = {"source": source} if source else {}

        # Count by status
        total = raw_collection.count_documents(base_filter)
        pending = raw_collection.count_documents(
            {
                **base_filter,
                **web_validator.build_status_filter("pending"),
            }
        )
        in_progress = raw_collection.count_documents(
            {
                **base_filter,
                **web_validator.build_status_filter("in_progress"),
            }
        )
        validated = raw_collection.count_documents(
            {
                **base_filter,
                **web_validator.build_status_filter("validated"),
            }
        )
        failed = raw_collection.count_documents(
            {
                **base_filter,
                **web_validator.build_status_filter("failed"),
            }
        )
        skipped = raw_collection.count_documents(
            {
                **base_filter,
                **web_validator.build_status_filter("skipped"),
            }
        )
        errors = raw_collection.count_documents(
            {
                **base_filter,
                **web_validator.build_status_filter("error"),
            }
        )

        # Breakdown by chatbot if any
        chatbot_pipeline = [
            {"$match": {"validatedBy": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": "$validatedBy", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        if source:
            chatbot_pipeline[0]["$match"]["source"] = source
        by_chatbot = list(raw_collection.aggregate(chatbot_pipeline))

        update_metrics(True)
        return {
            "success": True,
            "source": source or "all sources",
            "total": total,
            "status_breakdown": {
                "pending": pending,
                "in_progress": in_progress,
                "validated": validated,
                "failed": failed,
                "skipped": skipped,
                "error": errors,
            },
            "validated_percentage": round(validated / max(total, 1) * 100, 1),
            "by_chatbot": [{"chatbot_id": c["_id"], "count": c["count"]} for c in by_chatbot],
            "ready_to_process": validated,
        }

    except Exception as e:
        logger.error(f"Error in get_validation_status: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_records_for_contest_validation(
    batch_size: int = 5,
    chatbot_id: str = "default",
) -> dict:
    """
    Claim a batch of unvalidated contest documents and return them with a
    validation prompt for a chatbot.

    Similar to get_records_for_validation but works on the Contests
    collection. Use this for Stage 2 validation before LLM normalization.

    The chatbot uses its OWN web search to verify each contest's key
    fields (title, deadline, prize, eligibility) against the source page.

    Args:
        batch_size: Max contests to claim (default 5, max 25)
        chatbot_id: Identifier for the chatbot doing the validation

    Returns:
        Dictionary with validation_prompt, contests, and claimed_count
    """
    try:
        check_rate_limit("get_records_for_contest_validation")
        logger.info(f"Claiming contests for validation: batch={batch_size}, chatbot={chatbot_id}")

        from config.mongodb import db

        target_collection = db[DEFAULT_COLLECTION]
        batch_size = min(int(batch_size), 25)

        # Atomically claim contests needing migration
        claimed_contests = []
        for _ in range(batch_size):
            contest = target_collection.find_one_and_update(
                filter={
                    "validationStatus": {"$in": [None, "pending"]},
                    "$or": [
                        {"prizeSummary": {"$exists": False}},
                        {"feeConfidence": {"$exists": False}},
                        {"subCategory": {"$exists": False}},
                    ],
                },
                update=web_validator.get_next_pending_update(chatbot_id),
            )
            if contest is None:
                break
            contest["_id"] = str(contest["_id"])
            claimed_contests.append(contest)

        if not claimed_contests:
            return {
                "success": True,
                "chatbot_id": chatbot_id,
                "message": "No unvalidated contests needing migration found.",
                "claimed_count": 0,
                "contests": [],
                "validation_prompt": None,
            }

        # Build contest validation prompt
        prompt_text = load_prompt_text(PROMPT_VALIDATION)
        contest_section = ""
        for i, contest in enumerate(claimed_contests):
            source = contest.get("source", {})
            source_name = source.get("name", "unknown") if isinstance(source, dict) else "unknown"
            source_url = source.get("url", "") if isinstance(source, dict) else ""
            link = contest.get("link", "") or source_url
            timeline = contest.get("timeline", {}) or {}
            deadline = timeline.get("submissionDeadlineUTC", "not set") or "not set"
            prize_obj = contest.get("prize", {}) or {}
            prize = (
                prize_obj.get("prizeSummary", "")
                or prize_obj.get("originalAmount", "")
                or "not set"
            )
            audience = contest.get("audience", {}) or {}
            eligibility = audience.get("eligibilityLabel", "not set") or "not set"

            contest_section += (
                f"--- Contest {i + 1} ---\n"
                f"contest_id: {contest.get('_id', '')}\n"
                f"title: {contest.get('title', '<untitled>')}\n"
                f"source: {source_name}\n"
                f"url: {link}\n"
                f"category: {contest.get('category', 'not set')}\n"
                f"deadline: {deadline}\n"
                f"prize: {prize}\n"
                f"eligibility: {eligibility}\n"
                f"description: {str(contest.get('description', ''))[:200]}\n\n"
            )

        full_prompt = (
            prompt_text
            + contest_section
            + "\n"
            + (
                "Now validate each contest above and return ONLY a JSON object "
                "with the 'validations' array. Do not include any text outside the JSON."
            )
        )

        update_metrics(True)
        return {
            "success": True,
            "chatbot_id": chatbot_id,
            "claimed_count": len(claimed_contests),
            "contests": claimed_contests,
            "validation_prompt": full_prompt,
            "usage": {
                "purpose": "Send the validation_prompt + contests to your chatbot. The chatbot will use its own web search to verify each contest. Then call submit_contest_validation with the chatbot's JSON response.",
                "expected_chatbot_output": "A JSON object with a 'validations' array, each item having contest_id, status, confidence, issues, details",
            },
        }

    except Exception as e:
        logger.error(f"Error in get_records_for_contest_validation: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_validation_prompt(
    record_type: str = "raw",
) -> dict:
    """
    Preview the validation prompt without claiming any records.

    Use this to see the instructions that will be sent to the chatbot
    before starting the validation workflow.

    Args:
        record_type: "raw" for raw data validation prompt,
                     "contest" for contest validation prompt

    Returns:
        The full validation prompt text
    """
    try:
        check_rate_limit("get_validation_prompt")

        prompt_text = load_prompt_text(PROMPT_VALIDATION)

        update_metrics(True)
        return {
            "success": True,
            "record_type": record_type,
            "validation_prompt": prompt_text,
        }

    except Exception as e:
        logger.error(f"Error in get_validation_prompt: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
