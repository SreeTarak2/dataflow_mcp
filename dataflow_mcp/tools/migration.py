"""Contest migration / backfill MCP tools (v4.4 schema patches).

Includes the restructure path (``get_contest_for_restructuring``) for bringing
EXISTING old-schema records up to v4.4 with the full checklist in one call.
"""

import json
import os
from typing import Any, Dict

from dataflow_mcp.core import (
    mcp,
    logger,
    check_rate_limit,
    update_metrics,
    load_prompt_text,
    PROMPT_BACKFILL,
    PROMPT_CONTEST_STRUCTURING,
    _json_safe,
)
from tools.contest_migration import ContestMigration


# ────────────────────────────────────────────────────────────────────────
# Canonical taxonomy (spec item 12) — server-side source of truth
# ────────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_taxonomy(category: str = "") -> Dict[str, Any]:
    """
    Get the canonical CH taxonomy: main categories and their verbatim allowed
    subcategories (spec item 12).

    Call this BEFORE setting category/subCategory on any record. Every write
    path validates against this exact taxonomy — values outside it are
    rejected at write time, and known legacy/scraper inventions
    ("Robotics & Autonomous Systems", "Architecture & Urban Design", …) are
    auto-mapped to canonical values during ingestion.

    The taxonomy is code, not a document the model must remember: it changes
    only via a maintainer commit, NEVER as a side effect of writing a record.

    Args:
        category: Optional canonical category name to filter by (returns just
                  that category's subcategory list). Empty returns everything.

    Returns:
        Dictionary with categories, subcategories per category, and counts
    """
    from tools import taxonomy

    client_id = "get_taxonomy"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(True)

        if category:
            resolved = taxonomy.canonical_category(category)
            if not resolved:
                return {
                    "success": False,
                    "error": f"Unknown category '{category}'",
                    "hint": "Call get_taxonomy() with no argument to list the canonical categories.",
                    "canonical_categories": taxonomy.CANONICAL_CATEGORIES,
                }
            return {
                "success": True,
                "category": resolved,
                "subcategories": taxonomy.subcategories_for(resolved),
                "subcategory_count": len(taxonomy.subcategories_for(resolved)),
            }

        return {
            "success": True,
            "categories": taxonomy.CANONICAL_CATEGORIES,
            "subcategories_by_category": taxonomy.SUBCATEGORIES_BY_CATEGORY,
            **taxonomy.taxonomy_summary(),
            "rules": {
                "category_scoped": "A subCategory must belong to the record's category's list",
                "no_creation": "Taxonomy changes only via maintainer commit — never via record writes",
                "auto_mapping": (
                    "Known legacy/scraper inventions are auto-mapped on ingestion "
                    "(e.g. 'Robotics & Autonomous Systems' is ambiguous — spanning "
                    "Robotics / Autonomous Vehicles / Rovers & Space Robotics — so it "
                    "is flagged, not guessed)"
                ),
                "open_category_note": taxonomy.OPEN_CATEGORY_NOTE,
            },
        }

    except Exception as e:
        logger.error(f"Error in get_taxonomy: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_prompted_contests(
    prompt_name: str = PROMPT_BACKFILL,
    batch_size: int = 10,
    skip: int = 0,
    include_prompt: bool = True,
) -> Dict[str, Any]:
    """
    Return the backfill prompt together with contest documents for AI
    processing (merges the former get_contests_for_migration tool).

    Use this when Claude or ChatGPT needs both the instructions and the raw
    MongoDB contests in a single response so it can normalize them locally.
    With include_prompt=False it returns ONLY the contest documents needing
    migration — useful for custom pipelines that carry their own prompt or
    for pagination-only inspection.

    Args:
        prompt_name: Prompt file to bundle (default contest-backfill-v4.0.txt).
                     Old alias name "Prompts-backfill.txt" is also accepted.
        batch_size: Contests to fetch (max 100)
        skip: Number of contests to skip for pagination
        include_prompt: When False, omit the prompt text and return only the
                        contest documents needing migration
    """
    client_id = "get_prompted_contests"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(
            f"Building prompted contest bundle from {prompt_name} "
            f"(batch_size={batch_size}, skip={skip}, include_prompt={include_prompt})"
        )

        prompt_text = load_prompt_text(prompt_name) if include_prompt else None
        contest_result = ContestMigration.get_contests_needing_migration(
            batch_size=min(int(batch_size), 100),
            skip=max(int(skip), 0),
        )

        if not contest_result.get("success"):
            update_metrics(False)
            return contest_result

        contests = _json_safe(contest_result.get("contests", []))

        result = {
            "success": True,
            "prompt_name": prompt_name if include_prompt else None,
            "prompt_text": prompt_text,
            "contest_count": len(contests),
            "total_needing_migration": contest_result.get("total_needing_migration", 0),
            "skip": contest_result.get("skip", skip),
            "batch_size": contest_result.get("batch_size", batch_size),
            "contests": contests,
            "usage": {
                "purpose": "Send prompt_text and contests to your LLM, then apply the returned JSON patches with apply_migration_patch or bulk_apply_migrations.",
                "expected_llm_output": "A JSON patch per contest, containing only fields that need updates.",
            },
        }

        update_metrics(True)
        return result

    except FileNotFoundError as e:
        logger.error(f"Prompt file error: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in get_prompted_contests: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_migration_status() -> Dict[str, Any]:
    """
    Get overall migration progress statistics for the 810 contests.

    Shows how many contests have been migrated to v4.0 schema,
    how many are pending, and what fields are missing.

    Returns:
        Dictionary with migration progress and breakdown by field
    """
    client_id = "get_migration_status"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info("Fetching migration status")

        result = ContestMigration.get_migration_status()
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in get_migration_status: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ────────────────────────────────────────────────────────────────────────
# Restructure path: bring an EXISTING (old-schema) contest up to v4.4.
#
# Why this exists: get_records_for_structuring pulls RAW scrapes from
# CHRawdata — there was no designed path for "restructure a record already in
# the DB". Agents fell back to raw CRUD improvisation, never saw the v4.4
# checklist, and fields went missing (the "AI missed fields" failure mode).
# ────────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_contest_for_restructuring(
    contest_id: str = "",
    title: str = "",
) -> Dict[str, Any]:
    """
    Fetch an EXISTING contest document plus everything needed to restructure
    it to the current schema (v4.4) in one call.

    Use this instead of raw read_collection when a record is already in the DB
    but predates the current schema (missing edition, timeline.tiers,
    filterKeys, structured location, etc.). The response bundles:

      1. The existing document (so you can see what's already correct and
         must be preserved)
      2. The full v4.4 structuring prompt (the complete field-by-field
         checklist — the same rules the ingestion pipeline enforces)
      3. Step-by-step usage: research → produce patches → apply via
         apply_migration_patch → flag corrections

    Recommended flow:
      1. Call this tool with contest_id OR title
      2. Research the official source (web search) for fields that are null
         or legacy-shaped
      3. Build ONE JSON patch containing ONLY the fields that need updating
         (all v4.4 fields are patch-whitelisted)
      4. Call apply_migration_patch with the patch
         (use force=true ONLY when deliberately nulling a populated legacy
          field, e.g. clearing the deprecated audience.location string)
      5. If you corrected a scraped field against official evidence, also
         call flag_contest_discrepancy (scraped vs. official + sourceUrl)
      6. Run audit_records(ids_json=[contest_id]) to confirm nothing is
         still missing

    Args:
        contest_id: MongoDB ObjectId of the contest (preferred)
        title: Exact (or fragment) contest title — used when you don't have
               the id. Multiple matches → an error listing them.

    Returns:
        Dictionary with contest (existing document), prompt_text (v4.4),
        legacy_gaps (fields missing or legacy-shaped on this record), and
        the recommended patch/flag/audit flow
    """
    client_id = "get_contest_for_restructuring"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        from bson import ObjectId

        from config.mongodb import db

        collection = db[os.getenv("COLLECTION_NAME", "Contests")]

        contest = None
        if contest_id:
            try:
                contest = collection.find_one({"_id": ObjectId(contest_id)})
            except Exception:
                return {
                    "success": False,
                    "error": f"contest_id '{contest_id}' is not a valid MongoDB ObjectId",
                }
            if not contest:
                return {
                    "success": False,
                    "error": f"No contest found with _id {contest_id}",
                }
        elif title:
            # Exact match first, then case-insensitive fragment fallback.
            contest = collection.find_one({"title": title})
            if not contest:
                matches = list(
                    collection.find(
                        {"title": {"$regex": _regex_escape(title), "$options": "i"},
                         "archivedAt": None},
                        {"_id": 1, "title": 1, "source": 1, "link": 1},
                    ).limit(10)
                )
                if len(matches) == 1:
                    contest = collection.find_one({"_id": matches[0]["_id"]})
                elif matches:
                    return {
                        "success": False,
                        "error": f"{len(matches)} contests match title '{title}' — pass contest_id instead",
                        "matches": [
                            {
                                "_id": str(m["_id"]),
                                "title": m.get("title"),
                                "source": (m.get("source") or {}).get("name")
                                if isinstance(m.get("source"), dict)
                                else m.get("source"),
                                "link": m.get("link"),
                            }
                            for m in matches
                        ],
                    }
                else:
                    return {
                        "success": False,
                        "error": f"No contest found matching title '{title}'",
                    }
        else:
            return {
                "success": False,
                "error": "Provide contest_id (preferred) or title",
            }

        contest["_id"] = str(contest["_id"])
        contest = _json_safe(contest)

        prompt_text = load_prompt_text(PROMPT_CONTEST_STRUCTURING)

        # Field-gap scan so the agent knows exactly what to research.
        legacy_gaps = _detect_legacy_gaps(contest)

        update_metrics(True)
        logger.info(
            "Restructure bundle built for contest %s (%d gap(s))",
            contest["_id"],
            len(legacy_gaps),
        )
        return {
            "success": True,
            "contest_id": contest["_id"],
            "title": contest.get("title"),
            "contest": contest,
            "prompt_name": PROMPT_CONTEST_STRUCTURING,
            "prompt_text": prompt_text,
            "legacy_gaps": legacy_gaps,
            "workflow": {
                "step_1": (
                    "Review contest + legacy_gaps. Fields NOT listed as gaps are "
                    "presumed correct — do not rewrite them."
                ),
                "step_2": (
                    "Research the official source (web search) for each gap. "
                    "Follow the v4.4 prompt rules: null when unverifiable, never guess."
                ),
                "step_3": (
                    "Build ONE JSON patch with ONLY the gap fields, then call "
                    "apply_migration_patch(contest_id, patch_json). All v4.4 fields "
                    "(edition, timeline.tiers, filterKeys, flags, audienceScope, "
                    "source, participationGeography, …) are whitelisted."
                ),
                "step_4": (
                    "Use force=true ONLY when deliberately nulling a populated "
                    "legacy field (e.g. clearing the deprecated audience.location)."
                ),
                "step_5": (
                    "Corrected a scraped field against official evidence? Also call "
                    "flag_contest_discrepancy (scrapedValue vs officialValue + sourceUrl)."
                ),
                "step_6": (
                    "Confirm with audit_records(ids_json=[\"<contest_id>\"]) — "
                    "the record is done when it reports clean."
                ),
            },
        }

    except FileNotFoundError as e:
        logger.error(f"Prompt file error: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in get_contest_for_restructuring: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


def _regex_escape(text: str) -> str:
    """Escape a title fragment for safe $regex use."""
    import re

    return re.escape(text.strip())


def _detect_legacy_gaps(contest: Dict[str, Any]) -> Dict[str, Any]:
    """Report which v4.4 fields are missing or legacy-shaped on a document.

    Returned as {missing: [...], legacy: [...]} so the agent researches exactly
    what the record needs instead of guessing field-by-field.
    """
    missing: list = []
    legacy: list = []

    timeline = contest.get("timeline") or {}
    audience = contest.get("audience") or {}
    location = contest.get("location") or {}
    part_geo = contest.get("participationGeography") or {}

    if not contest.get("edition"):
        missing.append("edition")
    if not timeline.get("tiers"):
        missing.append("timeline.tiers")
    if not contest.get("filterKeys"):
        missing.append("filterKeys")
    if not contest.get("flags"):
        missing.append("flags")
    if not contest.get("descriptionDetailed"):
        missing.append("descriptionDetailed")
    if audience.get("skillLevels") in (None, []):
        missing.append("audience.skillLevels")
    if not audience.get("eligibilityLabel"):
        missing.append("audience.eligibilityLabel")
    if location.get("scope") in (None, "unknown"):
        missing.append("location.scope")
    if not part_geo or part_geo.get("scope") in (None, "unknown"):
        missing.append("participationGeography")
    if not (contest.get("prize") or {}).get("prizeSummary"):
        missing.append("prize.prizeSummary")
    if (contest.get("entry") or {}).get("feeConfidence") is None:
        missing.append("entry.feeConfidence")

    # Legacy shapes that the v4.4 schema replaces
    if isinstance(audience.get("location"), str):
        legacy.append("audience.location (deprecated string → use structured location)")
    if "prizeSummary" in contest and contest.get("prizeSummary") is not None:
        legacy.append("top-level prizeSummary (legacy → use prize.prizeSummary)")
    if "feeConfidence" in contest and contest.get("feeConfidence") is not None:
        legacy.append("top-level feeConfidence (legacy → use entry.feeConfidence)")
    if isinstance(contest.get("entryFees"), dict):
        legacy.append("entryFees object (legacy → use timeline.tiers)")
    if isinstance(timeline.get("earlyBirdDeadlineUTC"), str) or isinstance(
        timeline.get("lateDeadlineUTC"), str
    ):
        legacy.append("ad-hoc tier deadline keys (legacy → use timeline.tiers)")

    return {"missing": missing, "legacy": legacy}


@mcp.tool()
def apply_migration_patch(
    contest_id: str,
    patch_json: str,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Apply a validated normalized patch to update a single contest.

    All patches go through 4 validations before writing:
    1. Field whitelist — only allowed fields may be patched
       (includes timeline.tiers for tiered fees and edition.* for the
       edition-lock block)
    2. Schema compliance — types, enums, formats checked
       (allowedRegions must use the canonical region vocabulary — "EU" and
       US-state names are rejected with the canonical spelling in the error)
    3. Destructive write protection — populated fields not overwritten with null
    4. Cross-field consistency — no contradictory values

    PATCH-THEN-FLAG (spec item 8): when this patch corrects a scraped field
    against official evidence, ALSO call flag_contest_discrepancy with the
    scraped value vs. official value and the sourceUrl, so validation reports
    and DB state stay reconciled and future validation runs don't re-litigate
    the settled field.

    Args:
        contest_id: MongoDB ObjectId of the contest (as string)
        patch_json: JSON string with fields to update (flat or nested object;
                    timeline.tiers entries follow the tier block in the
                    structuring prompt v4.4)
        force: If True, bypass destructive write protection (use with caution)

    Returns:
        Update result with validation info
    """
    client_id = "apply_migration_patch"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Applying patch to contest {contest_id}")

        # Parse JSON patch
        try:
            patch = json.loads(patch_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON patch: {patch_json}")
            return {"success": False, "error": "Invalid JSON in patch_json"}

        result = ContestMigration.apply_migration_patch(contest_id, patch, force=force)
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in apply_migration_patch: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def bulk_apply_migrations(
    migrations_json: str,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Apply multiple migration patches in one batch (with validation).

    All patches go through 4 validations before writing.

    Args:
        migrations_json: JSON string containing array of migrations
        force: If True, bypass destructive write protection for all patches

    Returns:
        Bulk operation results with per-item validation status
    """
    client_id = "bulk_apply_migrations"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info("Starting bulk migration")

        # Parse JSON migrations
        try:
            migrations = json.loads(migrations_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON migrations: {migrations_json[:100]}")
            return {"success": False, "error": "Invalid JSON in migrations_json"}

        if not isinstance(migrations, list):
            return {"success": False, "error": "migrations_json must be an array"}

        result = ContestMigration.bulk_apply_migrations(migrations, force=force)
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in bulk_apply_migrations: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}
