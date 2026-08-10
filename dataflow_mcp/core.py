"""
Core shared state and helpers for the DataFlow MCP server.

Single source of truth for the :class:`FastMCP` instance, rate limiter,
metrics, collection constants, prompt loading, and the normalisation helpers
shared by the per-domain tool modules.

Import pattern (no circular imports — tool modules import FROM here only)::

    from dataflow_mcp.core import mcp, check_rate_limit, load_prompt_text
"""

import copy
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from config.logging_config import get_logger
from config.security import RateLimiter

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# MCP server + runtime state
# ─────────────────────────────────────────────────────────────────────────

mcp = FastMCP("DataFlow MCP Server")

# Rate limiter (100 requests per 60 seconds)
rate_limiter = RateLimiter(max_requests=100, time_window=60)

# Metrics
metrics: Dict[str, Any] = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "start_time": time.time(),
}

# ─────────────────────────────────────────────────────────────────────────
# Paths & collection constants
# ─────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DEFAULT_COLLECTION = os.getenv("COLLECTION_NAME", "Contests")
EVENT_COLLECTION = os.getenv("EVENT_COLLECTION_NAME", "Events")

# Canonical prompt files (descriptive names — old Prompts*.txt names are
# kept as alias copies in prompts/ for backward compatibility).
PROMPT_CONTEST_STRUCTURING = "contest-structuring-v4.1.txt"
PROMPT_CONTEST_DETAILS = "contest-details-v1.0.txt"
PROMPT_EVENTS = "event-structuring-v1.1.txt"
PROMPT_EVENT_DETAILS = "event-details-v1.0.txt"
PROMPT_BACKFILL = "contest-backfill-v3.2.txt"
PROMPT_VALIDATION = "validation-v1.0.txt"

# ─────────────────────────────────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────────────────────────────────


def load_prompt_text(prompt_name: str) -> str:
    """Load a prompt file from the local prompts directory."""
    prompt_path = PROMPTS_DIR / prompt_name

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_name}")

    return prompt_path.read_text(encoding="utf-8")


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


# ─────────────────────────────────────────────────────────────────────────
# Image / cover helpers
# ─────────────────────────────────────────────────────────────────────────


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


# ─────────────────────────────────────────────────────────────────────────
# Contest record normalisation (Prompts.txt v4.0 schema -> Contests doc)
# ─────────────────────────────────────────────────────────────────────────


def _build_normalized_record(
    record: Dict[str, Any],
    source_name: str,
    now_iso: str,
) -> Dict[str, Any]:
    """
    Map a structured contest record (contest-structuring-v4.0.txt schema) to a
    normalized dict ready for upsert into the Contests collection.

    Shared between submit_structured_records and submit_full_generation.

    Args:
        record: A raw contest dict following the v4.0 schema
        source_name: The dedup source name (from source.name)
        now_iso: ISO timestamp to use for updatedAt

    Returns:
        Normalized dict with fields mapped to Contests collection schema
    """
    title = record.get("title", "")
    link = record.get("link", "")
    source_obj = record.get("source", {})

    normalized: Dict[str, Any] = {
        "title": title,
        "link": link,
        "updatedAt": now_iso,
    }

    # type — v4.0 schema: "contest" | "hackathon" | "grant" | "fellowship" | "award" | "challenge"
    if record.get("type") in (
        "contest",
        "hackathon",
        "grant",
        "fellowship",
        "award",
        "challenge",
    ):
        normalized["type"] = record["type"]

    # flags — v4.0 schema: ["women"] (women-exclusive) / ["all"] (gender-neutral).
    # audienceScope is the canonical For Her classifier — carried alongside and
    # kept in sync so the transition-safe invariant always holds on writes.
    flags = record.get("flags", [])
    gender_flags = [f for f in (flags if isinstance(flags, list) else []) if f in ("women", "all")]
    # 'women' and 'all' are opposites per the prompts — if the model emits both,
    # 'women' (the more restrictive classification) wins deterministically.
    if "women" in gender_flags:
        gender_flags = ["women"]
    elif "all" in gender_flags:
        gender_flags = ["all"]

    audience_scope = record.get("audienceScope")
    if audience_scope not in ("women", "all", None):
        audience_scope = None  # invalid value → treated as unset

    if audience_scope is not None:
        if audience_scope not in gender_flags:
            gender_flags.append(audience_scope)
        normalized["audienceScope"] = audience_scope
    elif gender_flags:
        normalized["audienceScope"] = gender_flags[0]

    if gender_flags:
        normalized["flags"] = gender_flags

    # Simple string fields
    if "description" in record and record["description"]:
        normalized["description"] = record["description"]
    if "rawCategory" in record and record["rawCategory"]:
        normalized["rawCategory"] = record["rawCategory"]
    if "category" in record and record["category"]:
        normalized["category"] = record["category"]

    # source
    if isinstance(source_obj, dict):
        source_field: Dict[str, Any] = {}
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
        image_field: Dict[str, Any] = {}
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
        entry_field: Dict[str, Any] = {}
        if entry.get("isFree") is not None:
            entry_field["isFree"] = entry["isFree"]
        fee = entry.get("fee")
        if isinstance(fee, dict):
            fee_field: Dict[str, Any] = {}
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
        prize_field: Dict[str, Any] = {}
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
        audience_field: Dict[str, Any] = {}
        if audience.get("skillLevels"):
            audience_field["skillLevels"] = audience["skillLevels"]
        if audience.get("primarySkillLevel"):
            audience_field["primarySkillLevel"] = audience["primarySkillLevel"]
        age = audience.get("age")
        if isinstance(age, dict):
            age_field: Dict[str, Any] = {}
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
            constraints_field: Dict[str, Any] = {}
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
                ts_field: Dict[str, Any] = {}
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
        timeline_field: Dict[str, Any] = {}
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
            from tools.tag_normalizer import normalize_tags_array

            normalized["tags"] = normalize_tags_array(tags, max_tags=6)
        except Exception:
            normalized["tags"] = tags[:6]

    # filterKeys
    filter_keys = record.get("filterKeys")
    if isinstance(filter_keys, dict):
        fk_field: Dict[str, Any] = {}
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

    return normalized


# ─────────────────────────────────────────────────────────────────────────
# Event record normalisation (events-v1.1 schema -> Events doc)
# ─────────────────────────────────────────────────────────────────────────


def _slugify(title: Any) -> str:
    """Generate a URL-safe slug from a title (lowercase, hyphen-separated)."""
    if not title or not isinstance(title, str):
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int without crashing on non-numeric input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Enums from the events-v1.1 prompt schema (used for light validation)
EVENT_TYPES = {
    "conference",
    "summit",
    "workshop",
    "webinar",
    "meetup",
    "expo",
    "trade_show",
    "career_fair",
    "networking_event",
    "training_program",
    "festival",
}
EVENT_REGISTRATION_STATUSES = {"open", "closed", "waitlist"}
EVENT_VENUE_MODES = {"online", "offline", "hybrid"}
EVENT_DIFFICULTY_LEVELS = {"beginner", "intermediate", "advanced"}
EVENT_STATUSES = {"published", "draft", "cancelled", "archived"}

# Whitelist of event fields persisted verbatim (everything except the audit
# metadata block, which is stripped unless keep_metadata=True).
_EVENT_FIELDS = (
    "headline",
    "eventType",
    "shortSummary",
    "mediumSummary",
    "detailedOverview",
    "topics",
    "tags",
    "eventDates",
    "registration",
    "venue",
    "organizer",
    "speakers",
    "agenda",
    "pricing",
    "targetAudience",
    "benefits",
    "certifications",
    "sponsors",
    "partners",
    "faqs",
    "resources",
    "contact",
    "eventInsights",
    "seo",
    "quality",
    "flags",
    "audienceScope",
    "isRecurring",
    "recurrence",
    "customFields",
)


def _build_normalized_event(
    record: Dict[str, Any],
    source_name: str,
    now_iso: str,
    keep_metadata: bool = False,
) -> tuple[Dict[str, Any], List[str]]:
    """
    Map a structured event record (event-structuring-v1.1.txt schema) to a
    normalized dict ready for upsert into the Events collection.

    Applies the events-v1.1 defaults and light enum validation:

      - ``type`` is always forced to ``"event"``
      - ``status`` defaults to ``"draft"``, ``visibility`` to ``"public"``,
        ``featured`` to ``False``, analytics counters to ``0``
      - invalid enum values (eventType, registration.status, venue.mode,
        difficultyLevel, status) are downgraded to ``None`` and reported as
        warnings so the database never stores off-schema values
      - the audit ``metadata`` block is stripped unless ``keep_metadata=True``
        (per the events prompt's own guidance)

    Args:
        record: A raw event dict following the events-v1.1 schema
        source_name: The dedup source name (from source.name)
        now_iso: ISO timestamp to use for updatedAt / createdAt
        keep_metadata: If True, retain the metadata audit block

    Returns:
        (normalized dict, list of human-readable validation warnings)
    """
    warnings: List[str] = []

    title = str(record.get("title", "")).strip()
    source_obj = record.get("source") or {}

    source_field: Dict[str, Any] = {}
    if isinstance(source_obj, dict):
        for key in ("name", "url", "scrapedAt", "lastUpdated"):
            if source_obj.get(key):
                source_field[key] = str(source_obj[key])
    elif isinstance(source_obj, str) and source_obj:
        source_field["name"] = source_obj
    # Always guarantee a source.name so the dedup filter
    # {"source.name": ...} always has a key to match.
    source_field.setdefault("name", source_name)

    normalized: Dict[str, Any] = {
        "type": "event",
        "title": title,
        "source": source_field,
        "updatedAt": now_iso,
        "createdAt": now_iso,
    }
    if source_field.get("url"):
        # Convenience mirror of the contest `link` convention so the dedup
        # gate report and chatbot reads have a stable URL field.
        normalized["link"] = source_field["url"]

    # Persist whitelisted schema fields verbatim (only when present).
    # Deep-copied so enum sanitizing below never mutates the caller's input.
    for field in _EVENT_FIELDS:
        if field in record and record[field] is not None:
            normalized[field] = copy.deepcopy(record[field])

    # audienceScope — canonical For Her classifier. Validate the enum and keep
    # it synced with flags (the model may set one without the other).
    ev_scope = normalized.get("audienceScope")
    if ev_scope is not None and ev_scope not in ("women", "all"):
        warnings.append(f"audienceScope '{ev_scope}' is not in ('women', 'all'); set to null")
        normalized["audienceScope"] = None
        ev_scope = None
    ev_flags = normalized.get("flags")
    if not isinstance(ev_flags, list):
        ev_flags = []
    gender_flags = [f for f in ev_flags if f in ("women", "all")]
    other_flags = [f for f in ev_flags if f not in ("women", "all")]
    # 'women' wins over 'all' when both appear (they are opposites).
    if "women" in gender_flags:
        gender_flags = ["women"]
    elif "all" in gender_flags:
        gender_flags = ["all"]
    if ev_scope in ("women", "all"):
        if ev_scope not in gender_flags:
            gender_flags.append(ev_scope)
    elif gender_flags:
        normalized["audienceScope"] = gender_flags[0]
    # Always preserve non-gender lifecycle flags (event-ended, capacity-limited,
    # free-event, …) — only women/all are managed here.
    if gender_flags:
        normalized["flags"] = gender_flags + other_flags

    # slug — generate from title when missing
    slug = normalized.get("slug")
    if not slug and title:
        slug = _slugify(title)
    if slug:
        normalized["slug"] = slug

    # ── Light enum validation / defaults ──
    event_type = normalized.get("eventType")
    if event_type is not None and event_type not in EVENT_TYPES:
        warnings.append(f"eventType '{event_type}' is not in the events-v1.1 enum; set to null")
        normalized["eventType"] = None

    registration = normalized.get("registration")
    if isinstance(registration, dict) and registration.get("status") not in (
        None,
        *EVENT_REGISTRATION_STATUSES,
    ):
        warnings.append(
            f"registration.status '{registration['status']}' is invalid; set to null"
        )
        registration["status"] = None

    venue = normalized.get("venue")
    if isinstance(venue, dict) and venue.get("mode") not in (None, *EVENT_VENUE_MODES):
        warnings.append(f"venue.mode '{venue['mode']}' is invalid; set to null")
        venue["mode"] = None

    insights = normalized.get("eventInsights")
    if isinstance(insights, dict):
        difficulty = insights.get("difficultyLevel")
        if difficulty is not None and difficulty not in EVENT_DIFFICULTY_LEVELS:
            warnings.append(f"eventInsights.difficultyLevel '{difficulty}' is invalid; set to null")
            insights["difficultyLevel"] = None
        for score_key in (
            "careerValue",
            "networkingValue",
            "learningValue",
            "industryExposure",
            "overallValue",
        ):
            score = insights.get(score_key)
            if score is not None and (not isinstance(score, int) or not 1 <= score <= 5):
                warnings.append(
                    f"eventInsights.{score_key} must be an integer 1-5 or null; set to null"
                )
                insights[score_key] = None

    status = record.get("status")
    if status not in EVENT_STATUSES:
        if status is not None:
            warnings.append(f"status '{status}' is invalid; defaulted to 'draft'")
        normalized["status"] = "draft"
    else:
        normalized["status"] = status

    visibility = record.get("visibility")
    if visibility not in ("public", "private"):
        if visibility is not None:
            warnings.append(
                f"visibility '{visibility}' is invalid; defaulted to 'public'"
            )
        normalized["visibility"] = "public"
    else:
        normalized["visibility"] = visibility

    featured = record.get("featured")
    if isinstance(featured, str):
        normalized["featured"] = featured.strip().lower() in ("true", "1", "yes", "on")
    else:
        normalized["featured"] = bool(featured) if featured is not None else False

    analytics = record.get("analytics")
    if not isinstance(analytics, dict):
        analytics = {}
    normalized["analytics"] = {
        "views": _safe_int(analytics.get("views")),
        "registrations": _safe_int(analytics.get("registrations")),
        "shares": _safe_int(analytics.get("shares")),
    }

    # Audit metadata — stripped unless explicitly requested.
    if keep_metadata and isinstance(record.get("metadata"), dict):
        normalized["metadata"] = record["metadata"]

    return normalized, warnings
