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
    resolution: str = "",
) -> dict:
    """
    Flag a factual discrepancy found in CONTEST DATA during AI research.

    When a chatbot discovers a concrete, verifiable error in the Contests
    collection while doing research (e.g. the prize on the official page
    differs from what's stored), it can call this tool to save the finding
    to the flagged_discrepancies collection for human review.

    PART OF THE DOCUMENTED PATCH FLOW (spec item 8): when you correct a
    scraped field — via apply_migration_patch or update_document — ALSO call
    this tool with the scraped value vs. official value and the source URL.
    Patch + flag keeps validation reports and DB state reconciled: future
    validation runs see a settled discrepancy with a paper trail instead of
    re-litigating a field that was already fixed.

    Structured evidence shape (aliases accepted for compatibility):
      field        — the dotted field path that was wrong
      currentValue — what the DB had (alias: scrapedValue)
      observedValue— what the official source says (alias: officialValue)
      sourceUrl    — where the official value was read
      resolution   — what was done ("patched", "flag_only", …)

    This tool does NOT modify the Contests collection — it only records
    the finding.

    Args:
        contest_id: The MongoDB ObjectId of the contest with the issue
        discrepancies_json: JSON string — array of discrepancy objects (see shape above)
        flagged_by: Identifier for the chatbot/AI that found it
                     (e.g. "claude-1", "chatgpt-mistral")
        source: Pipeline stage that detected it
                (e.g. "ai_detail_generation", "ai_validation", "ai_patching")
        resolution: Optional overall resolution note (e.g. "patched via apply_migration_patch")

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

        # Normalize evidence aliases (spec item 8): scrapedValue→currentValue,
        # officialValue→observedValue so both vocabularies are first-class.
        normalized_discrepancies = []
        for d in discrepancies:
            if not isinstance(d, dict):
                normalized_discrepancies.append(d)
                continue
            entry = dict(d)
            if "currentValue" not in entry and "scrapedValue" in entry:
                entry["currentValue"] = entry.pop("scrapedValue")
            if "observedValue" not in entry and "officialValue" in entry:
                entry["observedValue"] = entry.pop("officialValue")
            normalized_discrepancies.append(entry)

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
            "discrepancies": normalized_discrepancies,
            "resolution": resolution or None,
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
            "message": (
                "Discrepancy flagged for human review. The Contests collection has NOT "
                "been modified. If you also patched this field, the flag records the "
                "paper trail (scraped vs. official) for future validation runs."
            ),
        }

    except Exception as e:
        logger.error(f"Error in flag_contest_discrepancy: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────────────
# Server-side batch audit (spec item 7)
# ────────────────────────────────────────────────────────────────────────


AUDIT_FIELD_CHECKS: Dict[str, str] = {
    "entry.fee.amount": "entryFee",
    "entry.isFree": "entryFee",
    "entry.feeConfidence": "entryFee",
    "location.scope": "location",
    "location.display": "location",
    "audience.mode": "mode",
    "audience.skillLevels": "skillLevels",
    "audience.eligibilityLabel": "eligibility",
    "participationGeography.scope": "geography",
    "timeline.submissionDeadlineUTC": "deadline",
}

DEFAULT_AUDIT_FIELDS = [
    "entry.fee.amount",
    "entry.isFree",
    "entry.feeConfidence",
    "location.scope",
    "location.display",
    "audience.mode",
    "audience.skillLevels",
    "audience.eligibilityLabel",
    "participationGeography.scope",
    "timeline.submissionDeadlineUTC",
]


def _audit_one(doc: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    """Check one document for missing (None/absent) audited fields."""

    def get_path(d: Any, path: str) -> Any:
        current = d
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    missing: List[str] = []
    empty: List[str] = []
    for field in fields:
        value = get_path(doc, field)
        if value is None:
            missing.append(field)
        elif isinstance(value, list) and not value:
            empty.append(field)
        elif isinstance(value, str) and not value.strip():
            empty.append(field)
    return {"missing": missing, "empty": empty}


@mcp.tool()
def audit_records(
    collection_name: str = "Contests",
    ids_json: str = "",
    fields_json: str = "",
    limit: int = 500,
) -> dict:
    """
    Server-side post-batch audit (spec item 7): check records for missing or
    empty critical fields in ONE call instead of N read_collection calls with
    client-side joins.

    Run this after every enrichment batch — it is cheap by design. Any field
    reported under "missing"/"empty" needs a patch before the batch counts as
    complete (the two "completed but had no entry-fee field at all" records
    from the original audit were exactly this failure mode).

    Args:
        collection_name: Collection to audit (default "Contests")
        ids_json: JSON array of document _ids to audit. Empty → whole collection.
        fields_json: JSON array of dotted field paths to check
                     (default: entryFee, location, audience.mode,
                      audience.skillLevels, eligibility, geography, deadline)
        limit: Max documents to scan when auditing the whole collection
               (default 500, max 2000)

    Returns:
        Per-record {id, missing, empty} plus aggregate counts. Records with no
        issues are omitted from "records" but counted in "clean".
    """
    client_id = "audit_records"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        from bson import ObjectId

        from config.mongodb import db

        collection = db[collection_name]
        limit = min(max(int(limit), 1), 2000)

        fields: List[str] = DEFAULT_AUDIT_FIELDS
        if fields_json:
            try:
                parsed_fields = json.loads(fields_json)
                if isinstance(parsed_fields, list) and parsed_fields:
                    fields = [str(f) for f in parsed_fields]
            except json.JSONDecodeError:
                return {"success": False, "error": "Invalid JSON in fields_json"}

        if ids_json:
            try:
                id_list = json.loads(ids_json)
            except json.JSONDecodeError:
                return {"success": False, "error": "Invalid JSON in ids_json"}
            if not isinstance(id_list, list) or not id_list:
                return {"success": False, "error": "ids_json must be a non-empty JSON array"}
            try:
                oids = [ObjectId(i) for i in id_list]
            except Exception:
                return {
                    "success": False,
                    "error": "ids_json contains an invalid ObjectId (must be 24-char hex)",
                }
            docs = list(collection.find({"_id": {"$in": oids}}))
        else:
            docs = list(collection.find({}).limit(limit))

        records: List[Dict[str, Any]] = []
        clean = 0
        for doc in docs:
            issues = _audit_one(doc, fields)
            if not issues["missing"] and not issues["empty"]:
                clean += 1
                continue
            records.append(
                {
                    "id": str(doc["_id"]),
                    "title": doc.get("title"),
                    **issues,
                }
            )

        # Aggregate: which fields are most often missing (drives scraper fixes)
        field_counts: Dict[str, int] = {}
        for r in records:
            for f in r["missing"]:
                field_counts[f] = field_counts.get(f, 0) + 1
            for f in r["empty"]:
                field_counts[f] = field_counts.get(f, 0) + 1

        update_metrics(True)
        return {
            "success": True,
            "collection": collection_name,
            "audited": len(docs),
            "clean": clean,
            "with_issues": len(records),
            "fields_checked": fields,
            "records": records,
            "missing_field_counts": dict(
                sorted(field_counts.items(), key=lambda kv: kv[1], reverse=True)
            ),
        }

    except Exception as e:
        logger.error(f"Error in audit_records: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────────────
# Status freshness guard (spec item 11)
# ────────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_stale_status_records(limit: int = 200) -> dict:
    """
    Find records whose stored status contradicts their own dates (spec item 11):
    "open"/"scheduled" records whose submission deadline (or event end) has
    already passed.

    A stale "open" status is a direct user-trust failure — a user plans to
    enter and discovers the contest closed months ago. This check requires no
    research or judgment, only comparing two stored fields, so run it often.

    Pair with refresh_stale_statuses (dry_run=true first) to fix them in one
    call with a full audit note per record.

    Args:
        limit: Max stale records to return (default 200, max 1000)

    Returns:
        Stale records with id, title, status, deadline, and days past due
    """
    client_id = "get_stale_status_records"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        from config.mongodb import db

        collection = db[config_module().COLLECTION_NAME]
        limit = min(max(int(limit), 1), 1000)
        now = datetime.now(timezone.utc)

        stale_filter = {
            "status": {"$in": ["open", "scheduled"]},
            "$or": [
                {
                    "timeline.submissionDeadlineUTC": {
                        "$type": "string",
                        "$lt": _cutoff_iso(now),
                    }
                },
                {"timeline.eventEndUTC": {"$type": "string", "$lt": _cutoff_iso(now)}},
            ],
        }

        docs = list(collection.find(stale_filter).limit(limit))

        stale = []
        for doc in docs:
            timeline = doc.get("timeline", {}) if isinstance(doc.get("timeline"), dict) else {}
            deadline = timeline.get("submissionDeadlineUTC") or timeline.get("eventEndUTC")
            days_past = _days_past(deadline, now)
            stale.append(
                {
                    "id": str(doc["_id"]),
                    "title": doc.get("title"),
                    "status": doc.get("status"),
                    "deadline": deadline,
                    "days_past_due": days_past,
                }
            )

        stale.sort(key=lambda s: s["days_past_due"] or 0, reverse=True)

        update_metrics(True)
        return {
            "success": True,
            "stale_count": len(stale),
            "records": stale,
            "message": (
                f"{len(stale)} record(s) marked open/scheduled with a past deadline. "
                "Run refresh_stale_statuses(dry_run=true) to preview the auto-close."
            ),
        }

    except Exception as e:
        logger.error(f"Error in get_stale_status_records: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


def config_module():
    """Lazy accessor for the os-level env (kept as a helper for testability)."""
    import os

    class _Env:
        COLLECTION_NAME = os.getenv("COLLECTION_NAME", "Contests")

    return _Env


def _cutoff_iso(now: datetime) -> str:
    """ISO cutoff string for string-typed deadline comparison (lexical ISO order)."""
    return now.strftime("%Y-%m-%dT%H:%M:%S")


def _days_past(deadline: Any, now: datetime):
    """Days past due for an ISO deadline string (None when unparseable)."""
    if not isinstance(deadline, str) or not deadline.strip():
        return None
    try:
        raw = deadline.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        return round(delta.total_seconds() / 86400, 1)
    except Exception:
        return None


@mcp.tool()
def refresh_stale_statuses(dry_run: bool = True, limit: int = 500) -> dict:
    """
    Auto-close records whose deadline has passed (spec item 11, action half).

    Every record with status "open"/"scheduled" and a past
    timeline.submissionDeadlineUTC / timeline.eventEndUTC flips to "closed"
    with an audit note (statusCheckedAt, statusAutoClosed) — a human can always
    distinguish an automated close from a researched one.

    Args:
        dry_run: When True (default), reports what WOULD change without writing.
        limit: Max records to update per call (default 500, max 2000)

    Returns:
        Per-record results and summary counts
    """
    client_id = "refresh_stale_statuses"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        from config.mongodb import db

        collection = db[config_module().COLLECTION_NAME]
        limit = min(max(int(limit), 1), 2000)
        now = datetime.now(timezone.utc)

        stale_filter = {
            "status": {"$in": ["open", "scheduled"]},
            "$or": [
                {"timeline.submissionDeadlineUTC": {"$type": "string", "$lt": _cutoff_iso(now)}},
                {"timeline.eventEndUTC": {"$type": "string", "$lt": _cutoff_iso(now)}},
            ],
        }

        docs = list(collection.find(stale_filter, {"title": 1, "timeline": 1}).limit(limit))

        if dry_run:
            preview = [
                {
                    "id": str(doc["_id"]),
                    "title": doc.get("title"),
                    "status": doc.get("status"),
                    "deadline": (doc.get("timeline") or {}).get("submissionDeadlineUTC")
                    or (doc.get("timeline") or {}).get("eventEndUTC"),
                    "would_set": "closed",
                }
                for doc in docs
            ]
            update_metrics(True)
            return {
                "success": True,
                "dry_run": True,
                "would_close": len(preview),
                "records": preview,
                "message": "Dry run — re-run with dry_run=false to apply.",
            }

        now_iso = now.isoformat()
        changed = 0
        details = []
        for doc in docs:
            result = collection.update_one(
                {"_id": doc["_id"], "status": {"$in": ["open", "scheduled"]}},
                {
                    "$set": {
                        "status": "closed",
                        "statusAutoClosed": True,
                        "statusCheckedAt": now_iso,
                    }
                },
            )
            if result.modified_count:
                changed += 1
                details.append({"id": str(doc["_id"]), "title": doc.get("title"), "new_status": "closed"})

        update_metrics(True)
        logger.info(f"refresh_stale_statuses: auto-closed {changed} record(s)")
        return {
            "success": True,
            "dry_run": False,
            "closed": changed,
            "records": details,
            "message": (
                f"Auto-closed {changed} stale record(s) with statusAutoClosed=true "
                "audit notes."
            ),
        }

    except Exception as e:
        logger.error(f"Error in refresh_stale_statuses: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
