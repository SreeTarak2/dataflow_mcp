"""
Shared canonical-enum and schema validation for contest records.

Single source of truth for the canonical vocabularies defined by the
structuring/validation prompts, enforced on EVERY write path:

  - ``update_document`` / ``create_document``   (dataflow_mcp/tools/crud.py)
  - ``submit_structured_records``               (dataflow_mcp/tools/contests.py)
  - ``submit_full_generation``                  (via _build_normalized_record)
  - ``apply_migration_patch`` / ``bulk_apply_migrations``  (PatchValidator)

Import safety:
  - No MongoDB, network, or config imports — safe to import anywhere
    (including tests that run from the repo root without ``config``).
  - ``contest_migration.py`` imports the shared constants from here (item 1:
    one set of enums everywhere instead of two drifting copies).

Spec items covered:
  1  Write-time enum & schema validation (reject or warn-and-tag per write)
  9  Tiered deadlines & fees — ``timeline.tiers`` block validation
  10 Required ``edition`` block — cycle validation helpers
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from tools.taxonomy import resolve_subcategory, canonical_category, subcategories_for

# ─────────────────────────────────────────────────────────────────────────
# Canonical vocabularies (mirrors structuring prompt v4.3+ / validation v2.0)
# ─────────────────────────────────────────────────────────────────────────

VALID_SKILL_LEVELS = {"beginner", "intermediate", "advanced", "open"}
VALID_SKILL_LEVEL_SOURCES = {"explicit", "inferred", "default"}
VALID_FEE_CONFIDENCE = {"confirmed", "extracted", "unknown"}
VALID_MODES = {"online", "offline", "in-person", "hybrid", "unknown"}
VALID_LOCATION_SCOPES = {
    "city",
    "country",
    "region",
    "worldwide",
    "online",
    "hybrid",
    "multi_location",
    "unknown",
}
VALID_LOCATION_PRECISIONS = {
    "venue",
    "city",
    "country",
    "region",
    "worldwide",
    "online",
    "unknown",
}
VALID_PARTICIPATION_SCOPES = {"worldwide", "countries", "country", "region", "unknown"}
VALID_TYPES = {"contest", "hackathon", "grant", "fellowship", "award", "challenge"}
VALID_SOURCE_TYPES = {"official", "aggregator", "unknown"}
VALID_GENDER_FLAGS = {"women", "all"}
VALID_ALL_FLAGS = {"women", "hero", "broken-link", "all"}

# CH Taxonomy v2 (2026-09) — the only category values a write may persist.
CANONICAL_CATEGORIES = {
    "Creative Arts & Design",
    "AI & Technology",
    "Engineering & Innovation",
    "Science & Research",
    "Business & Entrepreneurship",
    "Writing & Media",
    "Environment & Sustainability",
    "Education & Learning",
    "Social Impact & Leadership",
    "Open & Multidisciplinary",
}

# Canonical region vocabulary for allowedRegions (structuring prompt v4.3+).
CANONICAL_REGIONS = {
    "Africa",
    "Northern Africa",
    "Western Africa",
    "Middle Africa",
    "Eastern Africa",
    "Southern Africa",
    "Sub-Saharan Africa",
    "Americas",
    "North America",
    "Latin America",
    "Central America",
    "Caribbean",
    "South America",
    "Asia",
    "Central Asia",
    "Eastern Asia",
    "South-eastern Asia",
    "Southern Asia",
    "Western Asia",
    "Asia-Pacific",
    "Europe",
    "Eastern Europe",
    "Northern Europe",
    "Southern Europe",
    "Western Europe",
    "European Union",
    "Oceania",
    "Australia and New Zealand",
    "MENA",
    "Middle East",
    "Arab Region",
    "UK & Ireland",
}

# Legacy/alias spellings observed in real records → canonical region.
# Stored values are NOT rewritten silently: the validator reports the alias as
# a violation (with the canonical spelling in ``expected``) so callers can
# normalize deliberately. warning_severity=notice lets lenient callers accept
# them while still surfacing the drift.
REGION_ALIASES = {
    "EU": "European Union",
    "european union": "European Union",
    "uk": "UK & Ireland",
    "uk & ireland": "UK & Ireland",
    "usa": "North America",
    "america": "Americas",
    "mena region": "MENA",
    "middle-east": "Middle East",
}

# US states and other sub-national units are NOT regions (spec item 1).
_SUBNATIONAL_HINT_RE = re.compile(
    r"^(?:[^,]+,\s*)?(?:alabama|alaska|arizona|arkansas|california|colorado|"
    r"connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|"
    r"kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|"
    r"vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    r"neighboring states|neighboring countries|surrounding states)\b",
    re.IGNORECASE,
)

# Fee tier types — item 9.
VALID_TIER_TYPES = {"early", "regular", "late", "final"}

# audience.mode / location values that are participation MODES, never places —
# kept in sync with the mode-like-location guards in core.py.
VALID_AUDIENCE_MODES = VALID_MODES


def _iso_datetime_str(value: Any) -> bool:
    """True when value looks like an ISO 8601 date or datetime string."""
    if not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    date_part = raw.split("T", 1)[0]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
        return False
    if "T" in raw:
        time_part = raw.split("T", 1)[1]
        # Accept any reasonable HH:mm[:ss[.ffffff]][Z|±HH:MM] tail
        return bool(re.match(r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$", time_part))
    return True


def _year_month_str(value: Any) -> bool:
    """True when value looks like YYYY-MM."""
    return isinstance(value, str) and bool(re.match(r"^\d{4}-(0[1-9]|1[0-2])$", value.strip()))


# ─────────────────────────────────────────────────────────────────────────
# Violation model
# ─────────────────────────────────────────────────────────────────────────

SEVERITY_ERROR = "error"
SEVERITY_NOTICE = "notice"


class Violation:
    """One schema violation found in a contest document."""

    __slots__ = ("field", "value", "reason", "severity", "expected")

    def __init__(
        self,
        field: str,
        value: Any,
        reason: str,
        severity: str = SEVERITY_ERROR,
        expected: Optional[str] = None,
    ):
        self.field = field
        self.value = value
        self.reason = reason
        self.severity = severity
        self.expected = expected

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
            "severity": self.severity,
        }
        if self.expected:
            out["expected"] = self.expected
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Violation({self.field!r}, {self.value!r}, {self.severity})"


def _violation_to_str(v: Violation) -> str:
    base = f"{v.field}: {v.value!r} — {v.reason}"
    if v.expected:
        base += f" (expected: {v.expected})"
    return base


# ─────────────────────────────────────────────────────────────────────────
# Field-level validators (operate on a full document / update payload)
# ─────────────────────────────────────────────────────────────────────────

def _check_enum(
    violations: List[Violation],
    field: str,
    value: Any,
    allowed: set,
    severity: str = SEVERITY_ERROR,
) -> None:
    """Validate a single enum-valued field if present."""
    if value is None:
        return
    if not isinstance(value, str):
        violations.append(
            Violation(
                field,
                value,
                f"expected a string, got {type(value).__name__}",
                expected=f"one of: {', '.join(sorted(allowed))}",
                severity=severity,
            )
        )
        return
    if value in allowed or value.strip() in allowed:
        return
    hint = ""
    lowered = value.strip().lower()
    for candidate in sorted(allowed):
        if candidate.lower() == lowered:
            hint = f" (did you mean '{candidate}'?)"
            break
    violations.append(
        Violation(
            field,
            value,
            "not in canonical enum",
            expected=f"one of: {', '.join(sorted(allowed))}{hint}",
            severity=severity,
        )
    )


def _check_skill_levels(violations: List[Violation], value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        violations.append(
            Violation(
                "audience.skillLevels", value, f"expected a list, got {type(value).__name__}",
                expected="list of: beginner | intermediate | advanced | open",
            )
        )
        return
    for item in value:
        if not isinstance(item, str) or item not in VALID_SKILL_LEVELS:
            violations.append(
                Violation(
                    "audience.skillLevels",
                    value,
                    f"invalid entry {item!r}",
                    expected="list drawn from: beginner | intermediate | advanced | open",
                )
            )
            return


def _check_country_objects(violations: List[Violation], field: str, value: Any) -> None:
    """allowedCountries/restrictedCountries use {name, code} objects."""
    if value is None or not isinstance(value, list):
        return
    for i, item in enumerate(value):
        if isinstance(item, str):
            violations.append(
                Violation(
                    f"{field}[{i}]",
                    item,
                    "plain string — country must be a {name, code} object",
                    expected='{"name": "India", "code": "IN"}',
                    severity=SEVERITY_NOTICE,
                )
            )
        elif isinstance(item, dict) and not item.get("code"):
            violations.append(
                Violation(
                    f"{field}[{i}]",
                    item,
                    "missing ISO alpha-2 'code'",
                    expected='{"name": ..., "code": "IN"}',
                    severity=SEVERITY_NOTICE,
                )
            )


def region_violation(item: Any) -> Optional[Tuple[str, str]]:
    """Classify one allowedRegions entry.

    Returns ``None`` when the value is canonical, otherwise
    ``(reason, expected)`` where reason describes the problem and expected is
    the canonical value or a guidance string. Shared by the write-time
    validator and the migration patch validator.
    """
    if not isinstance(item, str):
        return (f"expected a string, got {type(item).__name__}", "a canonical region name")
    if item in CANONICAL_REGIONS or item.strip() in CANONICAL_REGIONS:
        return None
    stripped = item.strip()
    lowered = {r.lower(): r for r in CANONICAL_REGIONS}
    canonical = lowered.get(stripped.lower())
    if canonical:
        return ("non-canonical casing", canonical)
    alias = REGION_ALIASES.get(stripped) or REGION_ALIASES.get(stripped.lower())
    if alias:
        return ("non-canonical region alias", alias)
    if _SUBNATIONAL_HINT_RE.match(stripped):
        return (
            "sub-national units (US states etc.) are not regions",
            "a canonical region name",
        )
    return (
        "not a canonical region name",
        "canonical region vocabulary (e.g. 'European Union', 'Europe', 'MENA')",
    )


def _check_regions(violations: List[Violation], field: str, value: Any) -> None:
    """allowedRegions must use canonical region names (aliases flagged as notice)."""
    if value is None or not isinstance(value, list):
        return
    for i, item in enumerate(value):
        violation = region_violation(item)
        if violation is None:
            continue
        reason, expected = violation
        # Casing/alias drift is a notice (canonical value known, meaning
        # preserved); unknown and sub-national values are errors.
        severity = (
            SEVERITY_NOTICE
            if reason in ("non-canonical casing", "non-canonical region alias")
            else SEVERITY_ERROR
        )
        violations.append(
            Violation(
                f"{field}[{i}]" if isinstance(item, str) else field,
                item,
                reason,
                expected=expected,
                severity=severity,
            )
        )


def _check_participation_geography(violations: List[Violation], pg: Any) -> None:
    if not isinstance(pg, dict) or not pg:
        return
    scope = pg.get("scope")
    _check_enum(violations, "participationGeography.scope", scope, VALID_PARTICIPATION_SCOPES)
    _check_country_objects(violations, "participationGeography.allowedCountries", pg.get("allowedCountries"))
    _check_country_objects(violations, "participationGeography.restrictedCountries", pg.get("restrictedCountries"))
    _check_regions(violations, "participationGeography.allowedRegions", pg.get("allowedRegions"))

    # Restricted scope with ALL allowed lists empty → invisible to every filter.
    if scope in ("countries", "country", "region"):
        allowed_countries = pg.get("allowedCountries") or []
        allowed_regions = pg.get("allowedRegions") or []
        if not allowed_countries and not allowed_regions:
            violations.append(
                Violation(
                    "participationGeography",
                    {"scope": scope, "allowedCountries": allowed_countries, "allowedRegions": allowed_regions},
                    "restricted scope with empty allowed lists — invisible to every location filter",
                    expected='scope "unknown" or a non-empty allowedCountries/allowedRegions list',
                )
            )


def _check_location(violations: List[Violation], location: Any) -> None:
    if not isinstance(location, dict) or not location:
        return
    _check_enum(violations, "location.scope", location.get("scope"), VALID_LOCATION_SCOPES)
    _check_enum(violations, "location.precision", location.get("precision"), VALID_LOCATION_PRECISIONS)


def _check_audience(violations: List[Violation], audience: Any) -> None:
    if not isinstance(audience, dict) or not audience:
        return
    _check_enum(violations, "audience.mode", audience.get("mode"), VALID_AUDIENCE_MODES)
    _check_skill_levels(violations, audience.get("skillLevels"))
    _check_enum(
        violations, "audience.primarySkillLevel", audience.get("primarySkillLevel"), VALID_SKILL_LEVELS
    )
    _check_enum(
        violations,
        "audience.skillLevelSource",
        audience.get("skillLevelSource"),
        VALID_SKILL_LEVEL_SOURCES,
    )


def _check_entry(violations: List[Violation], entry: Any) -> None:
    if not isinstance(entry, dict) or not entry:
        return
    _check_enum(violations, "entry.feeConfidence", entry.get("feeConfidence"), VALID_FEE_CONFIDENCE)
    fee = entry.get("fee")
    if isinstance(fee, dict):
        amount = fee.get("amount")
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))):
            violations.append(
                Violation(
                    "entry.fee.amount",
                    amount,
                    f"expected a number, got {type(amount).__name__}",
                    expected="number or null",
                )
            )


def _check_tiers(violations: List[Violation], tiers: Any) -> None:
    """Item 9: timeline.tiers — typed fee/deadline tiers."""
    if tiers is None:
        return
    if not isinstance(tiers, list):
        violations.append(
            Violation("timeline.tiers", tiers, f"expected a list, got {type(tiers).__name__}")
        )
        return
    for i, tier in enumerate(tiers):
        prefix = f"timeline.tiers[{i}]"
        if not isinstance(tier, dict):
            violations.append(
                Violation(prefix, tier, f"expected an object, got {type(tier).__name__}")
            )
            continue
        _check_enum(violations, f"{prefix}.type", tier.get("type"), VALID_TIER_TYPES)
        deadline = tier.get("deadlineUTC")
        if deadline is not None and not _iso_datetime_str(deadline):
            violations.append(
                Violation(
                    f"{prefix}.deadlineUTC",
                    deadline,
                    "not an ISO 8601 date/datetime string",
                    expected="e.g. 2027-01-17T23:59:59",
                )
            )
        fee = tier.get("entryFee")
        if fee is not None:
            if not isinstance(fee, dict):
                violations.append(
                    Violation(
                        f"{prefix}.entryFee",
                        fee,
                        f"expected an object, got {type(fee).__name__}",
                        expected='{"amount": number, "currency": "EUR"}',
                    )
                )
            else:
                amount = fee.get("amount")
                if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))):
                    violations.append(
                        Violation(
                            f"{prefix}.entryFee.amount",
                            amount,
                            f"expected a number, got {type(amount).__name__}",
                        )
                    )
                currency = fee.get("currency")
                if currency is not None and (not isinstance(currency, str) or len(currency) > 5):
                    violations.append(
                        Violation(
                            f"{prefix}.entryFee.currency",
                            currency,
                            "expected an ISO currency code (max 5 chars)",
                        )
                    )


def _check_edition(violations: List[Violation], edition: Any) -> None:
    """Item 10: edition block — machine-checkable TARGET IDENTITY LOCK."""
    if edition is None:
        return
    if not isinstance(edition, dict):
        violations.append(
            Violation("edition", edition, f"expected an object, got {type(edition).__name__}")
        )
        return
    label = edition.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        violations.append(
            Violation("edition.label", label, "expected a non-empty string (organizer's branding)")
        )
    ordinal = edition.get("ordinal")
    if ordinal is not None and (isinstance(ordinal, bool) or not isinstance(ordinal, int)):
        violations.append(
            Violation("edition.ordinal", ordinal, f"expected an integer, got {type(ordinal).__name__}")
        )
    for key in ("cycleStart", "cycleEnd"):
        value = edition.get(key)
        if value is not None and not _year_month_str(value):
            violations.append(
                Violation(
                    f"edition.{key}",
                    value,
                    "expected YYYY-MM",
                    expected="e.g. 2026-10",
                )
            )
    start, end = edition.get("cycleStart"), edition.get("cycleEnd")
    if (
        isinstance(start, str)
        and isinstance(end, str)
        and _year_month_str(start)
        and _year_month_str(end)
        and end < start
    ):
        violations.append(
            Violation("edition.cycleEnd", end, f"cycleEnd {end} is before cycleStart {start}")
        )


def _check_timeline_dates(violations: List[Violation], timeline: Any) -> None:
    if not isinstance(timeline, dict) or not timeline:
        return
    for key in ("startDateUTC", "submissionDeadlineUTC", "eventEndUTC"):
        value = timeline.get(key)
        if value is not None and not _iso_datetime_str(value):
            violations.append(
                Violation(
                    f"timeline.{key}",
                    value,
                    "not an ISO 8601 date/datetime string",
                    expected="e.g. 2027-05-30T23:59:59",
                )
            )
    _check_tiers(violations, timeline.get("tiers"))


def _check_flags(violations: List[Violation], flags: Any) -> None:
    if flags is None or not isinstance(flags, list):
        return
    for item in flags:
        if item not in VALID_ALL_FLAGS:
            violations.append(
                Violation(
                    "flags",
                    item,
                    "not a valid flag",
                    expected="subset of: women | hero | broken-link | all",
                )
            )


def _check_type(violations: List[Violation], value: Any) -> None:
    _check_enum(violations, "type", value, VALID_TYPES)


def _check_category_pair(
    violations: List[Violation], category: Any, subcategory: Any
) -> None:
    """Spec item 12: category-scoped subCategory validation.

    Aliases/case differences are notices (canonical value known); ambiguous
    inventions ("Robotics & Autonomous Systems") and unknown values are
    errors — the response names the fix so the agent self-corrects in one
    round-trip.
    """
    resolved_cat = canonical_category(category) or category
    if subcategory is None or not isinstance(subcategory, str) or not subcategory.strip():
        return

    if not resolved_cat:
        # Category not in this fragment (patch of subCategory only): validate
        # against the GLOBAL canonical set. Category-scoped checking happens
        # when both fields are present (or via the patch validator's
        # cross-field check, which sees the existing document).
        from tools.taxonomy import subcategory_alias, _SUB_TO_CATEGORIES

        raw = subcategory.strip()
        alias = subcategory_alias(raw)
        if alias is not None:
            if alias != raw:
                violations.append(
                    Violation(
                        "subCategory",
                        subcategory,
                        "non-canonical subCategory (legacy/scraper value)",
                        expected=alias,
                        severity=SEVERITY_NOTICE,
                    )
                )
            return
        if raw in _SUB_TO_CATEGORIES:
            return  # canonical somewhere — cross-field check will scope it
        violations.append(
            Violation(
                "subCategory",
                subcategory,
                "not in the canonical CH taxonomy",
                expected=(
                    "a canonical subCategory — call get_taxonomy(); never invent values"
                ),
            )
        )
        return

    resolved, status = resolve_subcategory(resolved_cat, subcategory)
    if status == "canonical":
        return
    if status in ("case_normalized", "alias"):
        violations.append(
            Violation(
                "subCategory",
                subcategory,
                "non-canonical subCategory"
                + (" casing" if status == "case_normalized" else " (legacy/scraper value)"),
                expected=resolved,
                severity=SEVERITY_NOTICE,
            )
        )
        return
    allowed = subcategories_for(resolved_cat)
    if status == "ambiguous":
        violations.append(
            Violation(
                "subCategory",
                subcategory,
                "invented value spanning several canonical subcategories — pick ONE specific value",
                expected=(
                    f"one specific subCategory from the '{resolved_cat}' list "
                    f"({len(allowed)} allowed — call get_taxonomy(category='{resolved_cat}'))"
                ),
            )
        )
        return
    # unknown
    violations.append(
        Violation(
            "subCategory",
            subcategory,
            f"not in the canonical CH taxonomy for '{resolved_cat}'",
            expected=(
                f"one of the {len(allowed)} allowed subCategories — call "
                f"get_taxonomy(category='{resolved_cat}'); never invent values"
            ),
        )
    )


def _check_category(violations: List[Violation], value: Any, accept_legacy: bool = True) -> None:
    from tools.contest_migration import ACCEPTED_CATEGORIES, LEGACY_CATEGORY_ALIASES

    if value is None or not isinstance(value, str):
        return
    if value in ACCEPTED_CATEGORIES:
        if value in LEGACY_CATEGORY_ALIASES and accept_legacy:
            violations.append(
                Violation(
                    "category",
                    value,
                    "legacy category name",
                    expected=LEGACY_CATEGORY_ALIASES[value],
                    severity=SEVERITY_NOTICE,
                )
            )
        return
    violations.append(
        Violation(
            "category",
            value,
            "not a canonical category",
            expected="one of the CH Taxonomy v2 categories (e.g. 'Creative Arts & Design')",
        )
    )


# ─────────────────────────────────────────────────────────────────────────
# Top-level entry points
# ─────────────────────────────────────────────────────────────────────────

# Dot-path → (validator function, applies-to)
_FIELD_VALIDATORS: Dict[str, Any] = {
    "type": _check_type,
    "flags": _check_flags,
    "audience": _check_audience,
    "location": _check_location,
    "participationGeography": _check_participation_geography,
    "entry": _check_entry,
    "timeline": _check_timeline_dates,
    "edition": _check_edition,
}


def validate_contest_document(
    doc: Dict[str, Any],
    accept_legacy_category: bool = True,
) -> List[Violation]:
    """Validate a full contest document. Returns a list of Violations."""
    violations: List[Violation] = []
    if not isinstance(doc, dict):
        return [Violation("", doc, "expected an object")]

    _check_category(violations, doc.get("category"), accept_legacy=accept_legacy_category)
    _check_category_pair(violations, doc.get("category"), doc.get("subCategory"))
    for key, checker in _FIELD_VALIDATORS.items():
        if key in doc:
            checker(violations, doc[key])
    return violations


def validate_update_payload(
    update: Dict[str, Any],
    accept_legacy_category: bool = True,
) -> List[Violation]:
    """
    Validate an update payload against the canonical schema.

    Handles BOTH shapes agents actually send:
      - nested:      {"audience": {"mode": "hybrid"}}
      - dot-notation: {"audience.mode": "hybrid"}
      - operators:   {"$set": {...}, "$unset": {...}}

    Only the fields present in the payload are checked.
    """
    violations: List[Violation] = []

    def check_fragment(frag: Dict[str, Any]) -> None:
        if not isinstance(frag, dict):
            return
        # Rebuild a nested view of the fragment: full-object keys and
        # dot-notation keys are merged so both shapes validate identically.
        nested: Dict[str, Any] = {}
        for key, value in frag.items():
            if "." in key:
                top, rest = key.split(".", 1)
                if top not in _FIELD_VALIDATORS:
                    continue
                parts = rest.split(".")
                cursor = nested.setdefault(top, {})
                if not isinstance(cursor, dict):
                    continue
                for part in parts[:-1]:
                    nxt = cursor.setdefault(part, {})
                    if not isinstance(nxt, dict):
                        break
                    cursor = nxt
                else:
                    cursor[parts[-1]] = value
            else:
                existing = nested.get(key)
                if isinstance(existing, dict) and isinstance(value, dict):
                    # A dot-notation view of this object was already built —
                    # merge rather than clobber it.
                    existing.update(value)
                else:
                    nested[key] = value

        for key, checker in _FIELD_VALIDATORS.items():
            if key in nested:
                checker(violations, nested[key])

        # Category/subCategory pair check (spec item 12) — scalar keys, so
        # they are not covered by the _FIELD_VALIDATORS loop above.
        if "category" in frag or "subCategory" in frag:
            _check_category_pair(
                violations, frag.get("category"), frag.get("subCategory")
            )

    # Operator form ($set / $unset / $push / $pull at the top level)
    if isinstance(update, dict) and update and all(
        isinstance(k, str) and k.startswith("$") for k in update
    ):
        for _op, frag in update.items():
            if isinstance(frag, dict):
                check_fragment(frag)
    else:
        check_fragment(update)

    return violations


def violations_to_dicts(violations: List[Violation]) -> List[Dict[str, Any]]:
    return [v.to_dict() for v in violations]


def violations_to_strings(violations: List[Violation]) -> List[str]:
    return [_violation_to_str(v) for v in violations]


def split_violations(
    violations: List[Violation],
) -> Tuple[List[Violation], List[Violation]]:
    """Split into (errors, notices)."""
    errors = [v for v in violations if v.severity == SEVERITY_ERROR]
    notices = [v for v in violations if v.severity == SEVERITY_NOTICE]
    return errors, notices


def summarize_violations(violations: List[Violation]) -> Dict[str, Any]:
    """Compact summary used in tool responses."""
    errors, notices = split_violations(violations)
    return {
        "error_count": len(errors),
        "notice_count": len(notices),
        "errors": violations_to_dicts(errors),
        "notices": violations_to_dicts(notices),
    }
