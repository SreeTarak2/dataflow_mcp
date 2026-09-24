"""Duplicate audit, safe replacement, and discrepancy flagging MCP tools."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from dataflow_mcp.core import mcp, logger, check_rate_limit, update_metrics
from tools.dedup_gate import build_title_index, classify_replacement, normalize_title

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
def replace_contest(
    new_record_id: str,
    collection_name: str = "Contests",
    dry_run: bool = True,
) -> dict:
    """
    Safely delete old duplicates that share the SAME TITLE with a new record.

    Use this after a schema migration / re-structuring created a new document
    for a contest that already exists under the old schema. The tool refuses
    to delete anything unless a guard classifies the group as a safe
    replacement:

      1. Every other LIVE record sharing the new record's normalized title
         must have the EXACT same title (case/punctuation/whitespace
         insensitive, same word order). Reworded titles → verdict
         "review_required", nothing is deleted.
      2. If no other live record shares the title → "blocked" (there is no
         duplicate to delete).
      3. On a live run, EVERY duplicate is first snapshotted into
         "<collection>_archived" (full document + archivedAt + replacedBy),
         and only then removed. If archiving is incomplete, NOTHING is
         deleted.

    Restore anytime with `restore_contest(archive_id, collection_name)`.

    Args:
        new_record_id: ObjectId (string) of the NEW record to keep.
        collection_name: Collection to operate on (default "Contests").
        dry_run: When true (default), only reports what WOULD be deleted.

    Returns:
        Verdict, reason, and the ids archived/deleted (empty on dry_run or
        any non-safe verdict).
    """
    client_id = "replace_contest"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        from bson import ObjectId

        from config.mongodb import db

        try:
            new_oid = ObjectId(new_record_id)
        except Exception:
            return {
                "success": False,
                "error": "Invalid new_record_id — must be a 24-character MongoDB ObjectId",
            }

        collection = db[collection_name]
        survivor = collection.find_one({"_id": new_oid})
        if not survivor:
            return {
                "success": False,
                "error": f"Record {new_record_id} not found in {collection_name}",
            }

        # Guard: classify the title group (pure function, same normalization
        # as the ingestion dedup gate and find_duplicate_contests).
        title_index = build_title_index(collection)
        norm = normalize_title(survivor.get("title"))
        candidates = title_index.get(norm, [])
        verdict = classify_replacement(survivor, candidates, target_id=new_record_id)

        if verdict["verdict"] != "safe_replace":
            logger.warning(
                f"replace_contest blocked ({verdict['verdict']}) for "
                f"{new_record_id}: {verdict['reason']}"
            )
            return {
                "success": False,
                "verdict": verdict["verdict"],
                "reason": verdict["reason"],
                "review": verdict["review"],
                "archived_ids": [],
                "deleted": [],
                "message": (
                    "Dry run — nothing was deleted."
                    if dry_run
                    else "Nothing was deleted."
                ),
            }

        delete_ids = verdict["delete_ids"]

        if dry_run:
            return {
                "success": True,
                "verdict": "safe_replace",
                "dry_run": True,
                "kept_id": new_record_id,
                "would_delete": delete_ids,
                "review": [],
                "message": (
                    "Dry run only. Re-run with dry_run=false to archive and "
                    "delete these exact-title duplicates."
                ),
            }

        # Live run — snapshot EVERY duplicate before deleting ANY.
        now = datetime.now(timezone.utc).isoformat()
        archive = db[f"{collection_name}_archived"]
        archived_ids: List[str] = []
        for dup_id in delete_ids:
            old = collection.find_one({"_id": ObjectId(dup_id)})
            if not old:
                continue  # vanished since the index was built; will abort below
            snapshot = {
                "_id": old["_id"],
                "archivedAt": now,
                "archivedFromCollection": collection_name,
                "replacedBy": new_record_id,
                "reason": "replace_contest: exact-title duplicate",
                "originalDocument": old,
            }
            archive.replace_one({"_id": old["_id"]}, snapshot, upsert=True)
            archived_ids.append(dup_id)

        if len(archived_ids) != len(delete_ids):
            logger.error(
                f"replace_contest aborted for {new_record_id}: archived "
                f"{len(archived_ids)}/{len(delete_ids)} — nothing deleted"
            )
            return {
                "success": False,
                "verdict": "safe_replace",
                "error": "Archiving incomplete — nothing was deleted.",
                "archived_ids": archived_ids,
                "deleted": [],
            }

        # All duplicates safely archived — now remove them.
        deleted: List[Dict[str, Any]] = []
        for dup_id in delete_ids:
            result = collection.delete_one({"_id": ObjectId(dup_id)})
            deleted.append(
                {
                    "_id": dup_id,
                    "deleted": result.deleted_count == 1,
                    "restorable": True,
                }
            )

        update_metrics(True)
        logger.info(
            f"replace_contest: kept {new_record_id}, archived+deleted "
            f"{len(archived_ids)} duplicate(s) from {collection_name}"
        )
        return {
            "success": True,
            "verdict": "safe_replace",
            "dry_run": False,
            "kept_id": new_record_id,
            "archived_ids": archived_ids,
            "deleted": deleted,
            "archive_collection": f"{collection_name}_archived",
            "message": (
                f"Archived {len(archived_ids)} duplicate(s) to "
                f"{collection_name}_archived, then removed them from "
                f"{collection_name}. Restore anytime with restore_contest."
            ),
        }

    except Exception as e:
        logger.error(f"Error in replace_contest: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def restore_contest(archive_id: str, collection_name: str = "Contests") -> dict:
    """
    Undo a replace_contest deletion: put an archived document back.

    Copies the snapshot from "<collection>_archived" back into the live
    collection under its ORIGINAL _id, then removes the archive entry.

    Args:
        archive_id: ObjectId (string) of the archived document (same as the
            original document's _id).
        collection_name: Live collection to restore into (default "Contests").

    Returns:
        Dictionary with the restored document's id and title.
    """
    client_id = "restore_contest"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        from bson import ObjectId

        from config.mongodb import db

        try:
            oid = ObjectId(archive_id)
        except Exception:
            return {
                "success": False,
                "error": "Invalid archive_id — must be a 24-character MongoDB ObjectId",
            }

        archive = db[f"{collection_name}_archived"]
        snapshot = archive.find_one({"_id": oid})
        if not snapshot:
            return {
                "success": False,
                "error": f"No archived document {archive_id} in {collection_name}_archived",
            }

        doc = snapshot.get("originalDocument")
        if not isinstance(doc, dict) or "_id" not in doc:
            return {
                "success": False,
                "error": "Archive snapshot has no restorable originalDocument",
            }

        collection = db[collection_name]
        collection.replace_one({"_id": oid}, doc, upsert=True)
        archive.delete_one({"_id": oid})

        update_metrics(True)
        logger.info(f"restore_contest: restored {archive_id} into {collection_name}")
        return {
            "success": True,
            "restored_id": archive_id,
            "title": doc.get("title"),
            "message": f"Document restored into {collection_name} under its original _id.",
        }

    except Exception as e:
        logger.error(f"Error in restore_contest: {e}")
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
