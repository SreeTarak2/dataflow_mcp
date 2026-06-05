"""
Web Validator — Orchestrates chatbot-driven web validation.

The MCP server does NOT do web searches itself. Instead it:
  1. Gives unvalidated records to chatbots with validation instructions
  2. Chatbots use their own web search capability to verify each record
  3. Chatbots report back validation results
  4. MCP records the results and tracks status

Status lifecycle:
  pending        → record needs validation, not yet assigned
  in_progress    → assigned to a chatbot, being validated
  validated      → chatbot confirmed data is accurate
  failed         → chatbot found discrepancies
  skipped        → record had no source URL or was empty
  error          → validation encountered an error

Supports multiple chatbots working in parallel via chatbot_id tracking.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Validation Status Constants
# ──────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_VALIDATED = "validated"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

VALID_STATUSES = {
    STATUS_PENDING,
    STATUS_IN_PROGRESS,
    STATUS_VALIDATED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_ERROR,
}


# ──────────────────────────────────────────────
# Validation Prompt Builder
# ──────────────────────────────────────────────

def build_raw_validation_prompt(records: List[Dict[str, Any]]) -> str:
    """
    Build a prompt instructing the chatbot to web-search and validate
    each raw scraped record against its source URL.

    The chatbot uses its OWN web search capability (not the MCP server)
    to verify fields like title, deadline, prize, and fee.
    """
    prompt = """You are a CONTEST DATA VALIDATION ENGINE.

Your task: Validate each raw scraped contest record below by using your
web search capability to find the actual source page, then cross-reference
the scraped fields against what you find on the web.

==================================================
INSTRUCTIONS
==================================================

For EACH record:
1. Search the web for the contest title to find the official page
2. Visit the source URL if provided and reachable
3. Compare the scraped fields against what's on the actual page
4. Report any discrepancies

If the source URL is provided and reachable, use it as the primary source.
Otherwise, search the web using the contest title.

==================================================
FIELDS TO VALIDATE
==================================================

Check the following fields for accuracy:

1. **title** — Does the page title/h1 match the scraped title?
2. **deadline** — Does the deadline on the page match?
3. **prize** — Are prize amounts and type consistent?
4. **description** — Is the scraped description accurate?
5. **url** — Is the source URL correct and reachable?

==================================================
OUTPUT FORMAT — return a JSON object with this structure:
==================================================

Do NOT include any text outside the JSON object.

{
  "validations": [
    {
      "record_id": "<the record's _id field>",
      "title": "<contest title>",
      "status": "validated | failed | skipped | error",
      "confidence": 0.0-1.0,
      "source_url_found": "<URL found via search or the source URL>",
      "issues": ["list of specific discrepancies found, or empty if validated"],
      "details": {
        "title_match": true|false|null,
        "deadline_match": true|false|null,
        "prize_match": true|false|null,
        "description_accurate": true|false|null,
        "url_reachable": true|false
      },
      "notes": "Any additional context about the validation"
    }
  ]
}

Rules:
- validated: All key fields match the source page (or are absent on both sides)
- failed: One or more fields clearly contradict the source page
- skipped: Record has no title, no URL, or cannot be found on the web
- error: You encountered an issue trying to validate
- confidence: How confident you are in your assessment (0.0 = none, 1.0 = certain)
- issues: Be specific — e.g. "Deadline is March 15 but scraped as April 1"
- url_reachable: Did you successfully access the source URL?

==================================================
RECORDS TO VALIDATE
==================================================

"""

    for i, record in enumerate(records):
        record_id = str(record.get("_id", ""))
        title = record.get("title", "<untitled>")
        source = record.get("source", "unknown")
        url = record.get("url", "")
        deadline = record.get("deadline", "not specified")
        prize = record.get("prize", "not specified")
        description = record.get("description", "")[:200]

        prompt += f"""--- Record {i + 1} ---
record_id: {record_id}
title: {title}
source: {source}
url: {url}
deadline: {deadline}
prize: {prize}
description: {description}

"""

    prompt += """
==================================================
FINAL CHECK
==================================================
- Return ONLY a JSON object — no markdown, no explanations
- If you cannot access the source URL, note it in issues
- If you cannot find the contest on the web, set status to "skipped"
- Be truthful — do not fabricate validation results
"""

    return prompt


def build_contest_validation_prompt(contests: List[Dict[str, Any]]) -> str:
    """
    Build a prompt for validating existing contest documents before LLM
    normalization. Similar to raw but adapted for the Contests schema.
    """
    prompt = """You are a CONTEST DATA VALIDATION ENGINE.

Your task: Validate each existing contest document below against its
actual source page using your web search capability.

This is Stage 2 validation — these contests are already in the database
but need LLM normalization for backfill fields. We want to verify that
the existing data is trustworthy before sending it to the normalization LLM.

==================================================
INSTRUCTIONS
==================================================

For EACH contest:
1. Search the web using the title + source name to find the official page
2. Visit the contest's link/source URL if provided
3. Compare key existing fields against the actual page
4. Report whether the data is trustworthy enough for LLM normalization

==================================================
FIELDS TO VALIDATE
==================================================

1. **title** — Does the page title match?
2. **deadline** (timeline.submissionDeadlineUTC) — Does it match?
3. **prize** (prize.prizeSummary / prize.originalAmount) — Consistent?
4. **eligibility** (audience.eligibilityLabel) — Accurate?
5. **description** — Reasonably accurate?
6. **category** — In the right ballpark?
7. **link** / source URL — Reachable?

==================================================
OUTPUT FORMAT — JSON ONLY
==================================================

{
  "validations": [
    {
      "contest_id": "<MongoDB _id>",
      "title": "<contest title>",
      "status": "validated | failed | skipped | error",
      "confidence": 0.0-1.0,
      "source_url_found": "<URL found via search>",
      "issues": ["list of discrepancies, or empty"],
      "details": {
        "title_match": true|false|null,
        "deadline_match": true|false|null,
        "prize_match": true|false|null,
        "eligibility_accurate": true|false|null,
        "description_accurate": true|false|null,
        "url_reachable": true|false
      },
      "notes": "Any additional context"
    }
  ]
}

Rules:
- validated: Data is trustworthy enough for LLM normalization
- failed: Clear contradictions found — do NOT send to normalization LLM
- skipped: Cannot find the contest on the web at all
- error: Validation encountered an issue

==================================================
CONTESTS TO VALIDATE
==================================================

"""

    for i, contest in enumerate(contests):
        contest_id = str(contest.get("_id", ""))
        title = contest.get("title", "<untitled>")
        source = contest.get("source", {})
        source_name = source.get("name", "unknown") if isinstance(source, dict) else "unknown"
        source_url = source.get("url", "") if isinstance(source, dict) else ""
        link = contest.get("link", "") or source_url
        category = contest.get("category", "not set")
        deadline = "not set"
        timeline = contest.get("timeline", {})
        if isinstance(timeline, dict):
            deadline = timeline.get("submissionDeadlineUTC", "not set") or "not set"
        prize = "not set"
        prize_obj = contest.get("prize", {})
        if isinstance(prize_obj, dict):
            prize = prize_obj.get("prizeSummary", "not set") or prize_obj.get("originalAmount", "not set") or "not set"
        eligibility = "not set"
        audience = contest.get("audience", {})
        if isinstance(audience, dict):
            eligibility = audience.get("eligibilityLabel", "not set") or "not set"
        description = contest.get("description", "")[:200]

        prompt += f"""--- Contest {i + 1} ---
contest_id: {contest_id}
title: {title}
source: {source_name}
url: {link}
category: {category}
deadline: {deadline}
prize: {prize}
eligibility: {eligibility}
description: {description}

"""

    prompt += """
==================================================
FINAL CHECK
==================================================
- Return ONLY valid JSON — no surrounding text
- Be honest about what you could and could not verify
"""

    return prompt


# ──────────────────────────────────────────────
# Validation Result Processing
# ──────────────────────────────────────────────

def process_validation_results(
    chatbot_response: str,
) -> Dict[str, Any]:
    """
    Parse the chatbot's validation JSON response and normalize it.

    Args:
        chatbot_response: The raw text response from the chatbot
        expected_ids: List of record/contest IDs that were sent for validation

    Returns:
        Dict with:
          - success: bool
          - validations: list of parsed validation results
          - errors: list of parsing errors
          - summary: summary stats
    """
    import json
    import re

    result = {
        "success": False,
        "validations": [],
        "errors": [],
        "summary": None,
    }

    # Try to extract JSON from the response (handles markdown code blocks)
    json_str = chatbot_response.strip()

    # Strip markdown code fences if present
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", json_str)
    if json_match:
        json_str = json_match.group(1).strip()

    # Try to find a JSON object in the response
    json_obj_match = re.search(r"\{[\s\S]*\}", json_str)
    if json_obj_match:
        json_str = json_obj_match.group(0)

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        result["errors"].append(f"Failed to parse chatbot response as JSON: {e}")
        return result

    validations = parsed.get("validations", [])
    if not isinstance(validations, list):
        result["errors"].append("'validations' is not a list")
        return result

    # Normalize each validation
    normalized = []
    for v in validations:
        record_id = v.get("record_id") or v.get("contest_id", "")
        status = v.get("status", STATUS_ERROR)
        if status not in VALID_STATUSES:
            status = STATUS_ERROR

        normalized.append({
            "record_id": record_id,
            "title": v.get("title", ""),
            "status": status,
            "confidence": min(max(float(v.get("confidence", 0.5)), 0.0), 1.0),
            "source_url_found": v.get("source_url_found", ""),
            "issues": v.get("issues", []),
            "details": v.get("details", {}),
            "notes": v.get("notes", ""),
        })

    # Compute summary
    total = len(normalized)
    validated_count = sum(1 for v in normalized if v["status"] == STATUS_VALIDATED)
    failed_count = sum(1 for v in normalized if v["status"] == STATUS_FAILED)
    skipped_count = sum(1 for v in normalized if v["status"] == STATUS_SKIPPED)
    error_count = sum(1 for v in normalized if v["status"] == STATUS_ERROR)

    all_issues = []
    for v in normalized:
        all_issues.extend(v.get("issues", []))

    result["success"] = len(result["errors"]) == 0
    result["validations"] = normalized
    result["summary"] = {
        "total": total,
        "validated": validated_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "errors": error_count,
        "average_confidence": round(
            sum(v["confidence"] for v in normalized) / max(total, 1), 3
        ),
        "total_issues": len(all_issues),
    }

    return result


def build_validation_update(
    record_id: str,
    validation: Dict[str, Any],
    chatbot_id: str,
) -> Dict[str, Any]:
    """
    Build the MongoDB update document for recording a validation result.

    Args:
        record_id: The _id of the record
        validation: The parsed validation result from process_validation_results
        chatbot_id: Which chatbot performed the validation

    Returns:
        Dict suitable for MongoDB $set
    """
    now = datetime.now(timezone.utc).isoformat()

    return {
        "validationStatus": validation.get("status", STATUS_ERROR),
        "validatedAt": now,
        "validatedBy": chatbot_id,
        "validationConfidence": validation.get("confidence", 0.0),
        "validationIssues": validation.get("issues", []),
        "validationDetails": validation.get("details", {}),
        "validationNotes": validation.get("notes", ""),
        "sourceUrlFound": validation.get("source_url_found", ""),
        "lastModified": now,
    }


# ──────────────────────────────────────────────
# Status Query Helpers
# ──────────────────────────────────────────────

def build_status_filter(
    status_filter: Optional[str] = None,
    chatbot_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a MongoDB filter for querying records by validation status.

    Args:
        status_filter: One of "pending", "in_progress", "validated", "failed",
                      "skipped", "error", "needs_review", or None for all
        chatbot_id: If provided, filter by which chatbot validated it

    Returns:
        MongoDB filter dict
    """
    mongo_filter: Dict[str, Any] = {}

    if status_filter == "pending":
        mongo_filter["validationStatus"] = {"$in": [None, STATUS_PENDING]}
    elif status_filter == "needs_review":
        mongo_filter["validationStatus"] = {"$in": [STATUS_FAILED, STATUS_ERROR]}
    elif status_filter and status_filter in VALID_STATUSES:
        mongo_filter["validationStatus"] = status_filter
    elif status_filter == "all":
        pass  # no filter
    # else: return empty filter (all records)

    if chatbot_id:
        mongo_filter["validatedBy"] = chatbot_id

    return mongo_filter


def get_next_pending_filter(
    source: Optional[str] = None,
    chatbot_id: str = "",
) -> Dict[str, Any]:
    """
    Build a filter to find records that need validation and claim them.

    Uses atomic $set to prevent two chatbots from claiming the same record.

    Args:
        source: Optional source name to filter by
        chatbot_id: The chatbot claiming these records

    Returns:
        MongoDB filter for find_and_modify
    """
    filter_dict: Dict[str, Any] = {
        "validationStatus": {"$in": [None, STATUS_PENDING]},
    }
    if source:
        filter_dict["source"] = source

    return filter_dict


def get_next_pending_update(chatbot_id: str) -> Dict[str, Any]:
    """
    Build the update to atomically claim a pending record.

    Args:
        chatbot_id: The chatbot claiming this record

    Returns:
        MongoDB update dict for find_one_and_update
    """
    return {
        "$set": {
            "validationStatus": STATUS_IN_PROGRESS,
            "validatedBy": chatbot_id,
            "claimedAt": datetime.now(timezone.utc).isoformat(),
        }
    }
