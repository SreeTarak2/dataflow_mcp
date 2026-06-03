import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, List
from fastmcp import FastMCP
from config.logging_config import get_logger
from config.security import RateLimiter, ValidationError
from tools.data_manager import DataManager
from tools.contest_migration import ContestMigration
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
                    (
                        metrics["successful_requests"]
                        / max(metrics["total_requests"], 1)
                    )
                    * 100,
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
            collection_name="Contests",
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
            collection_name="Contests",
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
            _build_formatted_broken_image_card(card, contest)
            for card, contest in zip(cards, data)
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

        doc_result = DataManager.get_document("Contests", contest_id)
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
            collection_name="Contests",
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
                    req2 = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="GET")
                    with urllib.request.urlopen(req2, timeout=6) as resp2:
                        if resp2.status == 200:
                            status_ok = True
                except Exception:
                    status_ok = False

            # Update document with status
            update_data = {"image": {"primary": {"url": url, "status": ("active" if status_ok else "broken")}}}
            try:
                # Use DataManager.update_document to safely validate and update
                update_result = DataManager.update_document("Contests", contest_id, update_data)
                if update_result.get("success"):
                    if status_ok:
                        report["active"] += 1
                    else:
                        report["broken"] += 1
                    report["details"].append({"contest_id": contest_id, "url": url, "status": ("active" if status_ok else "broken")})
                else:
                    report["details"].append({"contest_id": contest_id, "url": url, "status": "update-failed", "error": update_result.get("error")})
            except Exception as e:
                report["details"].append({"contest_id": contest_id, "url": url, "status": "update-exception", "error": str(e)})

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
        
        result = ContestMigration.get_contests_needing_migration(
            batch_size=batch_size,
            skip=skip
        )
        
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


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Starting DataFlow MCP Server")
    logger.info("=" * 60)
    mcp.run()


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Starting DataFlow MCP Server")
    logger.info("=" * 60)
    mcp.run()

