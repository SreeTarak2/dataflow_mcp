import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, Dict, List
from fastmcp import FastMCP
import os
from config.logging_config import get_logger
from config.security import RateLimiter, ValidationError
from tools.data_manager import DataManager
from tools.contest_migration import ContestMigration
from tools.contest_detail_generator import ContestDetailGenerator
from tools import raw_data_processor
from tools import web_validator
import urllib.request
from urllib.error import URLError, HTTPError

logger = get_logger(__name__)

# Initialize MCP server
mcp = FastMCP("DataFlow MCP Server")

# Rate limiter (100 requests per 60 seconds)
rate_limiter = RateLimiter(max_requests=100, time_window=60)

# Metrics
metrics = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "start_time": time.time(),
}

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
DEFAULT_COLLECTION = os.getenv("COLLECTION_NAME", "Contests")


def _json_safe(value: Any) -> Any:
    """Convert MongoDB and datetime values into JSON-safe values."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        from bson.objectid import ObjectId

        if isinstance(value, ObjectId):
            return str(value)
    except Exception:
        pass
    return value


def load_prompt_text(prompt_name: str) -> str:
    """Load a prompt file from the local prompts directory."""
    prompt_path = PROMPTS_DIR / prompt_name

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_name}")

    return prompt_path.read_text(encoding="utf-8")


def _clean_text(value: Any, fallback: str = "Not specified") -> str:
    """Convert a value into a compact single-line string."""
    if value is None:
        return fallback
    if isinstance(value, str):
        cleaned = " ".join(value.strip().split())
        return cleaned if cleaned else fallback
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items) if items else fallback
    return str(value)


def _infer_theme(contest: Dict[str, Any]) -> str:
    """Infer a visual theme from contest fields with conservative fallback."""
    tags = [str(tag).lower() for tag in contest.get("tags", []) if isinstance(tag, str)]
    category = str(contest.get("category") or contest.get("rawCategory") or "").lower()
    description = str(contest.get("description") or "").lower()
    combined = " ".join(tags + [category, description])

    if any(key in combined for key in ["human-rights", "rights", "equity", "justice"]):
        return "Human Rights"
    if any(key in combined for key in ["climate", "sustainability", "environment"]):
        return "Sustainability"
    if any(key in combined for key in ["ai", "technology", "hackathon", "innovation"]):
        return "Technology"
    if any(key in combined for key in ["education", "student", "learning", "scholarship"]):
        return "Education"
    if any(key in combined for key in ["startup", "entrepreneur", "pitch"]):
        return "Entrepreneurship"
    if any(key in combined for key in ["research", "science", "lab"]):
        return "Research"
    if any(key in combined for key in ["leadership", "community", "social-impact"]):
        return "Leadership"
    if any(key in combined for key in ["art", "design", "creative", "film", "music"]):
        return "Creativity"
    return "Innovation"


def _build_cover_image_prompt(contest: Dict[str, Any]) -> str:
    """Generate an image-generation prompt for a missing contest banner."""
    title = _clean_text(contest.get("title"), "Untitled Opportunity")
    source = contest.get("source", {}) if isinstance(contest.get("source"), dict) else {}
    organizer = _clean_text(source.get("name"), "Organizer not specified")
    category = _clean_text(
        contest.get("category") or contest.get("rawCategory"),
        "Open / Multidisciplinary",
    )
    description = _clean_text(contest.get("description"))

    audience = contest.get("audience", {}) if isinstance(contest.get("audience"), dict) else {}
    eligibility = _clean_text(audience.get("eligibilityLabel"), "Open to eligible applicants")

    prize = contest.get("prize", {}) if isinstance(contest.get("prize"), dict) else {}
    prize_summary = _clean_text(prize.get("prizeSummary"), "Benefits not specified")

    theme = _infer_theme(contest)

    return (
        f"Create a premium wide landscape cover banner for '{title}'. "
        f"The scene should visually represent {theme} through powerful symbolic imagery. "
        f"Include visual cues for {category}, eligibility context ({eligibility}), "
        f"and the organizer mission of {organizer}. "
        f"Ground the composition in this contest context: {description}. "
        f"Benefits cue: {prize_summary}. "
        "Use a modern international editorial design language, cinematic lighting, rich details, "
        "sophisticated composition, diverse representation where appropriate, professional negative "
        "space for text overlays, high-end conference poster aesthetics, ultra sharp 8k detail, "
        "website hero-banner quality, no watermarks, no logos, no stock-photo look, no clutter."
    )


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse ISO datetime strings safely."""
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    try:
        # Support trailing Z timestamps
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _format_deadline(deadline_value: Any) -> str:
    """Convert deadline to a human-friendly label."""
    dt = _parse_iso_datetime(deadline_value)
    if not dt:
        return "Not specified"
    return dt.strftime("%B %-d, %Y")


def _derive_contest_status(contest: Dict[str, Any]) -> str:
    """Infer contest status from explicit field or submission deadline."""
    explicit = _clean_text(contest.get("status"), "")
    if explicit:
        return explicit

    timeline = contest.get("timeline", {}) if isinstance(contest.get("timeline"), dict) else {}
    deadline_dt = _parse_iso_datetime(timeline.get("submissionDeadlineUTC"))
    if not deadline_dt:
        return "Unknown"

    # Compare in UTC-aware style where possible
    now = datetime.utcnow().replace(tzinfo=deadline_dt.tzinfo)
    return "Closed" if deadline_dt < now else "Open"


def _build_broken_image_card(contest: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact, card-friendly contest summary for chatbot output."""
    timeline = contest.get("timeline", {}) if isinstance(contest.get("timeline"), dict) else {}
    audience = contest.get("audience", {}) if isinstance(contest.get("audience"), dict) else {}
    prize = contest.get("prize", {}) if isinstance(contest.get("prize"), dict) else {}
    image = contest.get("image", {}) if isinstance(contest.get("image"), dict) else {}
    primary = image.get("primary", {}) if isinstance(image.get("primary"), dict) else {}

    prize_text = _clean_text(prize.get("prizeSummary"), "Not specified")
    if prize_text == "Not specified":
        amount = _clean_text(prize.get("originalAmount"), "")
        currency = _clean_text(prize.get("currency"), "")
        if amount:
            prize_text = f"{amount} {currency}".strip()

    return {
        "contest_id": contest.get("_id"),
        "title": _clean_text(contest.get("title"), "Untitled Opportunity"),
        "category": _clean_text(
            contest.get("category") or contest.get("rawCategory"),
            "Open / Multidisciplinary",
        ),
        "status": _derive_contest_status(contest),
        "prize": prize_text,
        "deadline": _format_deadline(timeline.get("submissionDeadlineUTC")),
        "eligibility": _clean_text(audience.get("eligibilityLabel"), "Not specified"),
        "image_url": _clean_text(primary.get("url"), "Not specified"),
    }


def _build_formatted_broken_image_card(card: Dict[str, Any], contest: Dict[str, Any]) -> str:
    """Return the exact text block format requested for chatbot display."""
    source = contest.get("source", {}) if isinstance(contest.get("source"), dict) else {}
    source_name = _clean_text(source.get("name"), "Not specified")

    return (
        f"Title: {card.get('title', 'Untitled Opportunity')}\n"
        f"Category: {card.get('category', 'Open / Multidisciplinary')}\n"
        f"Status: {card.get('status', 'Unknown')}\n"
        f"Prize: {card.get('prize', 'Not specified')}\n"
        f"Deadline: {card.get('deadline', 'Not specified')}\n"
        f"Eligibility: {card.get('eligibility', 'Not specified')}\n"
        f"Link: {source_name}"
    )


def check_rate_limit(client_id: str = "default") -> bool:
    """Check if request is allowed by rate limiter."""
    if not rate_limiter.is_allowed(client_id):
        logger.warning(f"Rate limit exceeded for client: {client_id}")
        return False
    return True


def update_metrics(success: bool):
    """Update request metrics."""
    metrics["total_requests"] += 1
    if success:
        metrics["successful_requests"] += 1
    else:
        metrics["failed_requests"] += 1


# ==================== HEALTH CHECK ====================


@mcp.tool()
def health_check() -> Dict[str, Any]:
    """
    Check the health status of the MCP server process.

    Returns:
        Dictionary with health status and metrics
    """
    try:
        logger.info("Health check requested")

        uptime = time.time() - metrics["start_time"]

        health_data = {
            "status": "healthy",
            "uptime_seconds": round(uptime, 2),
            "metrics": {
                "total_requests": metrics["total_requests"],
                "successful_requests": metrics["successful_requests"],
                "failed_requests": metrics["failed_requests"],
                "success_rate": round(
                    (metrics["successful_requests"] / max(metrics["total_requests"], 1)) * 100,
                    2,
                ),
            },
        }

        return health_data

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


@mcp.tool()
def database_status() -> Dict[str, Any]:
    """
    Check MongoDB connectivity separately from server health.

    Returns:
        Database connection status and any connection error message
    """
    try:
        logger.info("Database status requested")

        from config.mongodb import ping_database

        mongo_ready, mongo_error = ping_database()
        return {
            "status": "healthy" if mongo_ready else "degraded",
            "mongodb": {
                "connected": mongo_ready,
                "error": mongo_error,
            },
        }

    except Exception as e:
        logger.error(f"Database status failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


@mcp.tool()
def get_prompted_contests(
    prompt_name: str = "Prompts-backfill.txt",
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Return the prompt text together with contest documents for AI processing.

    Use this when Claude or ChatGPT needs both the instructions and the raw
    MongoDB contests in a single response so it can normalize them locally.
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
def get_contests_missing_images(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Fetch contests where the primary image URL is missing or empty.

    Use this to identify documents that need AI-generated replacement banners.
    """
    client_id = "get_contests_missing_images"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        filter_dict = {
            "$or": [
                {"image": {"$exists": False}},
                {"image": None},
                {"image.primary": {"$exists": False}},
                {"image.primary": None},
                {"image.primary.url": {"$exists": False}},
                {"image.primary.url": None},
                {"image.primary.url": ""},
            ]
        }

        result = DataManager.read_data(
            collection_name=DEFAULT_COLLECTION,
            filter_query=filter_dict,
            limit=min(max(int(batch_size), 1), 100),
            skip=max(int(skip), 0),
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        data = result.get("data", [])
        update_metrics(True)
        return {
            "success": True,
            "count": len(data),
            "total": result.get("total", 0),
            "skip": result.get("skip", skip),
            "limit": result.get("limit", batch_size),
            "contests": _json_safe(data),
        }

    except Exception as e:
        logger.error(f"Error in get_contests_missing_images: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_contests_with_broken_images(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Fetch contests whose image.primary.status is marked as 'broken'.

    Returns the contests so the chatbot can display or re-generate banners.
    """
    client_id = "get_contests_with_broken_images"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        filter_dict = {"image.primary.status": "broken"}

        result = DataManager.read_data(
            collection_name=DEFAULT_COLLECTION,
            filter_query=filter_dict,
            limit=min(max(int(batch_size), 1), 500),
            skip=max(int(skip), 0),
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        data = result.get("data", [])
        cards = [_build_broken_image_card(contest) for contest in data]
        formatted_cards = [
            _build_formatted_broken_image_card(card, contest) for card, contest in zip(cards, data)
        ]
        update_metrics(True)
        return {
            "success": True,
            "count": len(data),
            "total": result.get("total", 0),
            "skip": result.get("skip", skip),
            "limit": result.get("limit", batch_size),
            "contest_cards": cards,
            "formatted_cards": formatted_cards,
            "contests": _json_safe(data),
        }

    except Exception as e:
        logger.error(f"Error in get_contests_with_broken_images: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def generate_cover_prompt_for_contest(contest_id: str) -> Dict[str, Any]:
    """
    Generate a premium image-generation prompt for one contest by ID.

    This returns a single prompt string that can be fed into any image model
    when the original contest banner is missing.
    """
    client_id = "generate_cover_prompt_for_contest"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        doc_result = DataManager.get_document(DEFAULT_COLLECTION, contest_id)
        if not doc_result.get("success"):
            update_metrics(False)
            return doc_result

        contest = doc_result.get("data", {})
        prompt = _build_cover_image_prompt(contest)

        update_metrics(True)
        return {
            "success": True,
            "contest_id": contest_id,
            "title": contest.get("title"),
            "image_prompt": prompt,
        }

    except Exception as e:
        logger.error(f"Error in generate_cover_prompt_for_contest: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def verify_image_urls(
    batch_size: int = 50,
    skip: int = 0,
    user_agent: str = "DataFlow-MCP/1.0",
) -> Dict[str, Any]:
    """
    Verify image URLs for contests and mark broken images in the database.

    This tool scans contests that have an `image.primary.url`, performs a
    lightweight HTTP HEAD/GET to verify reachability, and updates
    `image.primary.status` to 'active' or 'broken'. It returns a report.
    """
    client_id = "verify_image_urls"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        # Fetch contests that include an image URL
        filter_query = {"image.primary.url": {"$exists": True, "$ne": None, "$ne": ""}}
        result = DataManager.read_data(
            collection_name=DEFAULT_COLLECTION,
            filter_query=filter_query,
            limit=min(max(int(batch_size), 1), 500),
            skip=max(int(skip), 0),
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        contests = result.get("data", [])
        report = {"checked": 0, "active": 0, "broken": 0, "details": []}

        for contest in contests:
            report["checked"] += 1
            contest_id = contest.get("_id")
            url = None
            try:
                image = contest.get("image") or {}
                primary = image.get("primary") if isinstance(image, dict) else None
                url = primary.get("url") if isinstance(primary, dict) else None
            except Exception:
                url = None

            if not url:
                report["details"].append({"contest_id": contest_id, "status": "no-url"})
                continue

            # Attempt a HEAD request first; fall back to GET
            req = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="HEAD")
            status_ok = False
            try:
                with urllib.request.urlopen(req, timeout=6) as resp:
                    if resp.status == 200:
                        status_ok = True
            except HTTPError as e:
                # Non-200 status codes
                status_ok = False
            except URLError:
                status_ok = False
            except Exception:
                status_ok = False

            # If HEAD failed, try GET as some servers don't support HEAD
            if not status_ok:
                try:
                    req2 = urllib.request.Request(
                        url, headers={"User-Agent": user_agent}, method="GET"
                    )
                    with urllib.request.urlopen(req2, timeout=6) as resp2:
                        if resp2.status == 200:
                            status_ok = True
                except Exception:
                    status_ok = False

            # Update document with status
            update_data = {
                "image": {"primary": {"url": url, "status": ("active" if status_ok else "broken")}}
            }
            try:
                # Use DataManager.update_document to safely validate and update
                update_result = DataManager.update_document(
                    DEFAULT_COLLECTION, contest_id, update_data
                )
                if update_result.get("success"):
                    if status_ok:
                        report["active"] += 1
                    else:
                        report["broken"] += 1
                    report["details"].append(
                        {
                            "contest_id": contest_id,
                            "url": url,
                            "status": ("active" if status_ok else "broken"),
                        }
                    )
                else:
                    report["details"].append(
                        {
                            "contest_id": contest_id,
                            "url": url,
                            "status": "update-failed",
                            "error": update_result.get("error"),
                        }
                    )
            except Exception as e:
                report["details"].append(
                    {
                        "contest_id": contest_id,
                        "url": url,
                        "status": "update-exception",
                        "error": str(e),
                    }
                )

        update_metrics(True)
        return {"success": True, "report": report}

    except Exception as e:
        logger.error(f"Error in verify_image_urls: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ==================== READ OPERATIONS ====================


@mcp.tool()
def read_collection(
    collection_name: str,
    filter_query: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    sort_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read documents from a MongoDB collection with filtering and pagination.

    Args:
        collection_name: Name of the collection to read from
        filter_query: JSON string with MongoDB filter query (optional)
        limit: Maximum number of documents to return (max 1000)
        skip: Number of documents to skip for pagination
        sort_by: Field name to sort by (optional)

    Returns:
        Dictionary containing the documents and metadata
    """
    client_id = "read_collection"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)  # Assume failure, update if success

        logger.info(f"Reading from collection: {collection_name}")

        # Parse JSON filter query if provided
        filter_dict = None
        if filter_query:
            try:
                filter_dict = json.loads(filter_query)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON filter: {filter_query}")
                return {"success": False, "error": "Invalid JSON in filter_query"}

        # Call data manager
        result = DataManager.read_data(
            collection_name=collection_name,
            filter_query=filter_dict,
            limit=limit,
            skip=skip,
            sort_by=sort_by,
        )

        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in read_collection: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_document(
    collection_name: str,
    document_id: str,
) -> Dict[str, Any]:
    """
    Get a single document by ID.

    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document

    Returns:
        Dictionary containing the document data
    """
    client_id = "get_document"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Getting document {document_id} from {collection_name}")

        result = DataManager.get_document(collection_name, document_id)
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in get_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== CREATE OPERATIONS ====================


@mcp.tool()
def create_document(
    collection_name: str,
    document_json: str,
) -> Dict[str, Any]:
    """
    Create a new document in a collection.

    Args:
        collection_name: Name of the collection
        document_json: JSON string representing the document to create

    Returns:
        Dictionary with the ID of the created document
    """
    client_id = "create_document"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Creating document in {collection_name}")

        # Parse JSON document
        try:
            document = json.loads(document_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON document: {document_json}")
            return {"success": False, "error": "Invalid JSON in document_json"}

        # Remove _id if present (let MongoDB generate it)
        document.pop("_id", None)

        result = DataManager.create_document(collection_name, document)
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in create_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== UPDATE OPERATIONS ====================


@mcp.tool()
def update_document(
    collection_name: str,
    document_id: str,
    update_json: str,
) -> Dict[str, Any]:
    """
    Update an existing document in a collection.

    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document to update
        update_json: JSON string with the fields to update

    Returns:
        Dictionary with update result
    """
    client_id = "update_document"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Updating document {document_id} in {collection_name}")

        # Parse JSON update data
        try:
            update_data = json.loads(update_json)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON update: {update_json}")
            return {"success": False, "error": "Invalid JSON in update_json"}

        result = DataManager.update_document(collection_name, document_id, update_data)
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in update_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== DELETE OPERATIONS ====================


@mcp.tool()
def delete_document(
    collection_name: str,
    document_id: str,
) -> Dict[str, Any]:
    """
    Delete a document from a collection.

    Args:
        collection_name: Name of the collection
        document_id: The MongoDB object ID of the document to delete

    Returns:
        Dictionary with deletion result
    """
    client_id = "delete_document"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        logger.info(f"Deleting document {document_id} from {collection_name}")

        result = DataManager.delete_document(collection_name, document_id)
        update_metrics(result.get("success", False))
        return result

    except Exception as e:
        logger.error(f"Error in delete_document: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


# ==================== MIGRATION OPERATIONS ====================


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


# ---------------------------------------------------------------------------
# Raw Data Processing Tools (bridge CHRawdata.rawdata -> ContestHopperDb.Contests)
# ---------------------------------------------------------------------------


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
def get_records_for_structuring(
    source: Optional[str] = None,
    limit: int = 5,
    require_validated: bool = False,
) -> dict:
    """
    Fetch raw scraped records + Prompts.txt (v4.0 schema) so a chatbot can
    structure them into the normalized Contests format.

    The chatbot should:
      1. Read the prompt_text for schema and rules
      2. For each record, use its URL (or title) to search the web and find
         the actual contest page
      3. Extract fields following the Prompts.txt schema
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

        prompt_text = load_prompt_text("Prompts.txt")
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
            "prompt_name": "Prompts.txt",
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
) -> dict:
    """
    Submit structured contest records (following Prompts.txt v4.0 schema)
    produced by a chatbot. Validates required fields and upserts into the
    Contests collection.

    Deduplication key: source.name + title (same as process_raw_data).

    Args:
        records_json: JSON string — either a single object or an array of
                      structured contest objects following the Prompts.txt schema

    Returns:
        Dictionary with inserted/updated/skipped/error counts
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
        from tools.tag_normalizer import normalize_tags_array

        target_collection = db[os.getenv("COLLECTION_NAME", "Contests")]
        now_iso = datetime.now(timezone.utc).isoformat()

        required_fields = {"title", "link"}
        inserted = 0
        updated = 0
        skipped = 0
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

            normalized = {
                "title": title,
                "link": link,
                "updatedAt": now_iso,
            }

            # type — Prompts.txt: "contest" | "hackathon" | "grant" | "fellowship" | "award" | "challenge"
            if record.get("type") in (
                "contest",
                "hackathon",
                "grant",
                "fellowship",
                "award",
                "challenge",
            ):
                normalized["type"] = record["type"]

            # flags — Prompts.txt: ["women"] for women-exclusive contests
            flags = record.get("flags", [])
            if isinstance(flags, list) and any(f in ("women",) for f in flags):
                normalized["flags"] = [f for f in flags if f in ("women",)]

            # Map Prompts.txt fields to Contests collection schema
            if "description" in record and record["description"]:
                normalized["description"] = record["description"]
            if "rawCategory" in record and record["rawCategory"]:
                normalized["rawCategory"] = record["rawCategory"]
            if "category" in record and record["category"]:
                normalized["category"] = record["category"]

            # source
            if isinstance(source_obj, dict):
                source_field = {}
                if source_obj.get("name"):
                    source_field["name"] = source_obj["name"]
                if source_obj.get("url"):
                    source_field["url"] = source_obj["url"]
                if source_obj.get("type"):
                    source_field["type"] = source_obj["type"]
                if source_field:
                    normalized["source"] = source_field
            else:
                normalized["source"] = {"name": source_name}

            # image
            image = record.get("image")
            if isinstance(image, dict):
                image_field = {}
                primary = image.get("primary", {})
                if isinstance(primary, dict) and primary.get("url"):
                    image_field["primary"] = {
                        "url": primary["url"],
                        "status": primary.get("status", "active"),
                    }
                    if image_field:
                        normalized["image"] = image_field
                elif image.get("url"):
                    normalized["image"] = {"primary": {"url": image["url"], "status": "active"}}

            # entry
            entry = record.get("entry")
            if isinstance(entry, dict):
                entry_field = {}
                if entry.get("isFree") is not None:
                    entry_field["isFree"] = entry["isFree"]
                fee = entry.get("fee")
                if isinstance(fee, dict):
                    fee_field = {}
                    if fee.get("amount") is not None:
                        fee_field["amount"] = fee["amount"]
                    if fee.get("currency"):
                        fee_field["currency"] = fee["currency"]
                    if fee_field:
                        entry_field["fee"] = fee_field
                if entry.get("feeConfidence"):
                    entry_field["feeConfidence"] = entry["feeConfidence"]
                if entry.get("feeNote"):
                    entry_field["feeNote"] = entry["feeNote"]
                if entry_field:
                    normalized["entry"] = entry_field

            # prize
            prize = record.get("prize")
            if isinstance(prize, dict):
                prize_field = {}
                if prize.get("isMonetary") is not None:
                    prize_field["isMonetary"] = prize["isMonetary"]
                if prize.get("originalAmount") is not None:
                    prize_field["originalAmount"] = prize["originalAmount"]
                if prize.get("totalUSD") is not None:
                    prize_field["totalUSD"] = prize["totalUSD"]
                if prize.get("currency"):
                    prize_field["currency"] = prize["currency"]
                if prize.get("prizeSummary"):
                    prize_field["prizeSummary"] = prize["prizeSummary"]
                if prize.get("description"):
                    prize_field["description"] = prize["description"]
                if prize_field:
                    normalized["prize"] = prize_field

            # audience
            audience = record.get("audience")
            if isinstance(audience, dict):
                audience_field = {}
                if audience.get("skillLevels"):
                    audience_field["skillLevels"] = audience["skillLevels"]
                if audience.get("primarySkillLevel"):
                    audience_field["primarySkillLevel"] = audience["primarySkillLevel"]
                age = audience.get("age")
                if isinstance(age, dict):
                    age_field = {}
                    if age.get("min") is not None:
                        age_field["min"] = age["min"]
                    if age.get("max") is not None:
                        age_field["max"] = age["max"]
                    if age_field:
                        audience_field["age"] = age_field
                if audience.get("eligibilityLabel"):
                    audience_field["eligibilityLabel"] = audience["eligibilityLabel"]
                if audience.get("eligibilityDetail"):
                    audience_field["eligibilityDetail"] = audience["eligibilityDetail"]
                constraints = audience.get("constraints")
                if isinstance(constraints, dict):
                    constraints_field = {}
                    for key in (
                        "participantType",
                        "academicStatus",
                        "graduationAfter",
                        "organizationFoundedAfter",
                    ):
                        if constraints.get(key) is not None:
                            constraints_field[key] = constraints[key]
                    team_size = constraints.get("teamSize")
                    if isinstance(team_size, dict):
                        ts_field = {}
                        if team_size.get("min") is not None:
                            ts_field["min"] = team_size["min"]
                        if team_size.get("max") is not None:
                            ts_field["max"] = team_size["max"]
                        if ts_field:
                            constraints_field["teamSize"] = ts_field
                    if constraints_field:
                        audience_field["constraints"] = constraints_field
                if audience.get("location"):
                    audience_field["location"] = audience["location"]
                if audience.get("mode"):
                    audience_field["mode"] = audience["mode"]
                if audience_field:
                    normalized["audience"] = audience_field

            # timeline
            timeline = record.get("timeline")
            if isinstance(timeline, dict):
                timeline_field = {}
                for key in (
                    "startDateUTC",
                    "submissionDeadlineUTC",
                    "eventEndUTC",
                    "organizerTimeZone",
                ):
                    if timeline.get(key):
                        timeline_field[key] = timeline[key]
                if timeline_field:
                    normalized["timeline"] = timeline_field

            # tags
            tags = record.get("tags", [])
            if isinstance(tags, list) and tags:
                try:
                    normalized["tags"] = normalize_tags_array(tags, max_tags=6)
                except Exception:
                    normalized["tags"] = tags[:6]

            # filterKeys
            filter_keys = record.get("filterKeys")
            if isinstance(filter_keys, dict):
                fk_field = {}
                if filter_keys.get("domain"):
                    fk_field["domain"] = filter_keys["domain"]
                if filter_keys.get("format"):
                    fk_field["format"] = filter_keys["format"]
                if filter_keys.get("medium"):
                    fk_field["medium"] = filter_keys["medium"]
                if filter_keys.get("themes"):
                    fk_field["themes"] = filter_keys["themes"]
                if fk_field:
                    normalized["filterKeys"] = fk_field

            # slug
            if "slug" in record and record["slug"]:
                normalized["slug"] = record["slug"]

            # derived status from deadline
            normalized["status"] = "open"

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
            "skipped": skipped,
            "errors": errors,
            "details": error_details[:10],
        }

    except Exception as e:
        logger.error(f"Error in submit_structured_records: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Web Validation Tools (chatbot-driven — chatbot does its own web search)
# ---------------------------------------------------------------------------
#
# Architecture:
#   1. get_records_for_validation()  — fetches unvalidated records + prompt for chatbot
#   2. Chatbot does web search on its own, returns JSON validation results
#   3. submit_raw_validation()       — chatbot submits its validation results
#   4. submit_contest_validation()   — same for contests
#   5. get_validation_status()       — check progress
#   6. get_validation_prompt()       — preview the prompt without claiming records
#
# Multiple chatbots can work in parallel by passing different chatbot_id strings.
# Status tracking prevents duplicate work.
# ---------------------------------------------------------------------------


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
        prompt_text = load_prompt_text("Prompts-validation.txt")
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
        import json as json_module

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
        prompt_text = load_prompt_text("Prompts-validation.txt")
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

        prompt_text = load_prompt_text("Prompts-validation.txt")

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


@mcp.tool()
def process_raw_data(source: str, limit: int = 100, auto_image: bool = False) -> dict:
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
    """
    try:
        check_rate_limit("process_raw_data")
        logger.info(f"Processing raw data for source={source}, limit={limit}")
        limit = min(limit, 1000)  # safety cap
        result = raw_data_processor.process_raw_data(
            source, limit, auto_image=auto_image, require_validation=True
        )
        update_metrics(result.get("success", False))
        return result
    except Exception as e:
        logger.error(f"Error in process_raw_data: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}


# ==================== CONTEST DETAIL GENERATION ====================


@mcp.tool()
def get_contests_for_detail_generation(
    batch_size: int = 11,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Return contests needing AI-generated detail pages, sorted by priority.

    The response includes both the prompt text (Prompts-contest-details.txt)
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

        prompt_text = load_prompt_text("Prompts-contest-details.txt")

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
            "prompt_name": "Prompts-contest-details.txt",
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
                    "A JSON object per contest matching the schema in Prompts-contest-details.txt"
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
        details_json: JSON string matching the Prompts-contest-details.txt schema

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

        # Save (even with warnings — warnings mean low quality, not unusable)
        save_result = generator.save(
            contest_id=contest_id,
            content=validation.get("content", parsed.get("content", {})),
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
                "valid": validation["valid"],
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


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Starting DataFlow MCP Server")
    logger.info("=" * 60)
    mcp.run()
