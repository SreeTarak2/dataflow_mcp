"""Duplicate audit & discrepancy flagging MCP tools."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from dataflow_mcp.core import mcp, logger, check_rate_limit, update_metrics
from tools.dedup_gate import build_title_index, source_name_of

# Discrepancy flags live in the raw DB cluster (temporary/audit purposes).
FLAGGED_COLLECTION = "flagged_discrepancies"


@mcp.tool()
def find_duplicate_contests(min_live: int = 2) -> dict:
    """
    Read-only audit: find groups of LIVE contests that share the same
    normalized title (case/punctuation-insensitive, word-order-insensitive).

    These are the records the duplicate-title gate would block on ingestion.
    Same-source exact-title duplicates are also shown here (they only update
    in place during ingestion, but if two records share a source + title the
    older one is effectively shadowed).

    Args:
        min_live: Minimum number of live records in a group to report
                  (default 2). Archived contests are ignored.

    Returns:
        Dictionary with duplicate group count and per-group members
        (_id, title, source, link).
    """
    try:
        check_rate_limit("find_duplicate_contests")

        from config.mongodb import db

        collection = db[os.getenv("COLLECTION_NAME", "Contests")]
        title_index = build_title_index(collection)

        groups = []
        total_live = 0
        for norm, docs in sorted(title_index.items()):
            if len(docs) < min_live:
                continue
            groups.append(
                {
                    "normalized_title": norm,
                    "live_count": len(docs),
                    "members": [
                        {
                            "_id": str(d["_id"]),
                            "title": d.get("title"),
                            "source": source_name_of(d) or None,
                            "link": d.get("link"),
                        }
                        for d in docs
                    ],
                }
            )
            total_live += len(docs)

        update_metrics(True)
        return {
            "success": True,
            "duplicate_groups": len(groups),
            "total_live_records_in_groups": total_live,
            "groups": groups,
        }

    except Exception as e:
        logger.error(f"Error in find_duplicate_contests: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def flag_contest_discrepancy(
    contest_id: str,
    discrepancies_json: str,
    flagged_by: str = "",
    source: str = "ai_detail_generation",
) -> dict:
    """
    Flag a factual discrepancy found in CONTEST DATA during AI research.

    When a chatbot discovers a concrete, verifiable error in the Contests
    collection while doing research (e.g. the prize on the official page
    differs from what's stored), it can call this tool to save the finding
    to the flagged_discrepancies collection for human review.

    This tool does NOT modify the Contests collection — it only records
    the finding. A human should review and resolve via the appropriate
    pipeline (apply_migration_patch, etc.).

    Args:
        contest_id: The MongoDB ObjectId of the contest with the issue
        discrepancies_json: JSON string — array of discrepancy objects.
            Each object: {
              "field": "prize.totalUSD",
              "currentValue": 50000,
              "observedValue": 10000,
              "sourceUrl": "https://...",
              "confidence": 0.95,
              "notes": "Official page clearly states $10,000"
            }
        flagged_by: Identifier for the chatbot/AI that found it
                     (e.g. "claude-1", "chatgpt-mistral")
        source: Pipeline stage that detected it
                (e.g. "ai_detail_generation", "ai_validation")

    Returns:
        Dictionary with flag_id and summary
    """
    client_id = "flag_contest_discrepancy"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        logger.info(f"Flagging discrepancy for contest {contest_id} by {flagged_by}")

        from config.mongodb import get_raw_db

        # Parse the discrepancies JSON
        try:
            discrepancies = json.loads(discrepancies_json)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid JSON in discrepancies_json: {e}"}

        if not isinstance(discrepancies, list):
            return {"success": False, "error": "discrepancies_json must be a JSON array"}

        if not discrepancies:
            return {"success": False, "error": "discrepancies array is empty"}

        # Connect to raw DB and get/create the flagged_discrepancies collection
        raw_db = get_raw_db()
        flagged_collection = raw_db[FLAGGED_COLLECTION]

        now = datetime.now(timezone.utc).isoformat()

        doc = {
            "contestId": contest_id,
            "flaggedBy": flagged_by,
            "detectedAt": now,
            "source": source,
            "status": "pending",
            "discrepancies": discrepancies,
            "reviewedBy": None,
            "reviewedAt": None,
            "reviewNotes": None,
        }

        insert_result = flagged_collection.insert_one(doc)
        flag_id = str(insert_result.inserted_id)

        logger.info(
            f"Flagged discrepancy {flag_id} saved for contest {contest_id} "
            f"({len(discrepancies)} issue(s))"
        )

        update_metrics(True)
        return {
            "success": True,
            "flag_id": flag_id,
            "contest_id": contest_id,
            "discrepancy_count": len(discrepancies),
            "status": "pending",
            "message": "Discrepancy flagged for human review. The Contests collection has NOT been modified.",
        }

    except Exception as e:
        logger.error(f"Error in flag_contest_discrepancy: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
