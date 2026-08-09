"""Contest migration / backfill MCP tools (v4.0 schema patches)."""

import json
from typing import Any, Dict

from dataflow_mcp.core import (
    mcp,
    logger,
    check_rate_limit,
    update_metrics,
    load_prompt_text,
    PROMPT_BACKFILL,
    _json_safe,
)
from tools.contest_migration import ContestMigration


@mcp.tool()
def get_prompted_contests(
    prompt_name: str = PROMPT_BACKFILL,
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Return the prompt text together with contest documents for AI processing.

    Use this when Claude or ChatGPT needs both the instructions and the raw
    MongoDB contests in a single response so it can normalize them locally.

    Args:
        prompt_name: Prompt file to bundle (default contest-backfill-v2.0.txt).
                     Old alias name "Prompts-backfill.txt" is also accepted.
        batch_size: Contests to fetch (max 100)
        skip: Number of contests to skip for pagination
    """
    client_id = "get_prompted_contests"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(
            f"Building prompted contest bundle from {prompt_name} "
            f"(batch_size={batch_size}, skip={skip})"
        )

        prompt_text = load_prompt_text(prompt_name)
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
            "prompt_name": prompt_name,
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


@mcp.tool()
def get_contests_for_migration(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Get a batch of existing contests that need migration to v4.0 schema.

    Returns contests missing key fields like category, prizeSummary,
    or feeConfidence. Use pagination to process in batches.

    Args:
        batch_size: Number of contests to fetch (max 100)
        skip: Number of documents to skip for pagination

    Returns:
        Dictionary containing contest documents and pagination info
    """
    client_id = "get_contests_for_migration"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Fetching {batch_size} contests for migration")

        # Enforce limits
        batch_size = min(int(batch_size), 100)
        skip = int(skip)

        if batch_size < 1:
            batch_size = 10
        if skip < 0:
            skip = 0

        result = ContestMigration.get_contests_needing_migration(batch_size=batch_size, skip=skip)

        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in get_contests_for_migration: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


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
    2. Schema compliance — types, enums, formats checked
    3. Destructive write protection — populated fields not overwritten with null
    4. Cross-field consistency — no contradictory values

    Args:
        contest_id: MongoDB ObjectId of the contest (as string)
        patch_json: JSON string with fields to update
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
