"""Unit tests for tools/schema_validation.py — write-time enum & schema checks.

Pure-function tests: no MongoDB, no network. These encode the exact invalid
values that reached production before write-time validation existed
(spec item 1 of the CustomMCP upgrade spec).

Run with pytest:      pytest tests/test_schema_validation.py
Run standalone:      python tests/test_schema_validation.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.schema_validation import (  # noqa: E402
    SEVERITY_ERROR,
    SEVERITY_NOTICE,
    split_violations,
    summarize_violations,
    region_violation,
    validate_contest_document,
    validate_update_payload,
)


def _errors(violations):
    return [v for v in violations if v.severity == SEVERITY_ERROR]


def _notices(violations):
    return [v for v in violations if v.severity == SEVERITY_NOTICE]


# ── The production bugs from the spec ────────────────────────────────────


def test_amateur_skill_level_rejected():
    v = validate_contest_document(
        {"audience": {"skillLevels": ["amateur", "professional"]}}
    )
    errs = _errors(v)
    assert any("amateur" in str(x.value) for x in errs)
    assert any("beginner" in (x.expected or "") for x in errs)


def test_onsite_location_scope_rejected():
    v = validate_contest_document({"location": {"scope": "onsite"}})
    errs = _errors(v)
    assert any("onsite" in str(x.value) for x in errs)
    # actionable: lists the allowed values
    assert any("multi_location" in (x.expected or "") for x in errs)


def test_non_canonical_regions_flagged():
    v = validate_contest_document(
        {
            "participationGeography": {
                "scope": "region",
                "allowedRegions": ["EU", "Oklahoma", "neighboring states"],
            }
        }
    )
    values = [str(x.value) for x in v]
    assert "EU" in values  # alias → notice with canonical expected
    assert "Oklahoma" in values  # sub-national → error
    assert "neighboring states" in values  # sub-national → error
    eu = next(x for x in v if str(x.value) == "EU")
    assert eu.severity == SEVERITY_NOTICE
    assert eu.expected == "European Union"


def test_restricted_scope_with_empty_lists_rejected():
    v = validate_contest_document(
        {
            "participationGeography": {
                "scope": "countries",
                "allowedCountries": [],
                "allowedRegions": [],
            }
        }
    )
    assert any("restricted scope with empty allowed lists" in x.reason for x in v)


def test_unknown_audience_mode_rejected():
    v = validate_contest_document({"audience": {"mode": "virtual"}})
    assert any(x.field == "audience.mode" for x in _errors(v))


def test_clean_record_passes():
    doc = {
        "type": "contest",
        "category": "AI & Technology",
        "audience": {
            "mode": "hybrid",
            "skillLevels": ["beginner", "intermediate"],
            "primarySkillLevel": "intermediate",
        },
        "location": {"scope": "hybrid", "precision": "city"},
        "participationGeography": {
            "scope": "countries",
            "allowedCountries": [{"name": "India", "code": "IN"}],
        },
        "timeline": {
            "submissionDeadlineUTC": "2027-01-17T23:59:59",
            "tiers": [
                {"type": "early", "deadlineUTC": "2026-09-30", "entryFee": {"amount": 10, "currency": "EUR"}},
                {"type": "regular", "deadlineUTC": "2026-10-31", "entryFee": {"amount": 15, "currency": "EUR"}},
            ],
        },
        "edition": {"label": "2027", "ordinal": 13, "cycleStart": "2026-10", "cycleEnd": "2027-04"},
        "flags": ["all"],
    }
    assert validate_contest_document(doc) == []


def test_legacy_category_is_notice_not_error():
    errs, notices = split_violations(
        validate_contest_document({"category": "Creative Arts"})
    )
    assert errs == []
    assert len(notices) == 1
    assert notices[0].expected == "Creative Arts & Design"


def test_unknown_category_is_error():
    v = validate_contest_document({"category": "Best Contest Ever"})
    assert any(x.field == "category" for x in _errors(v))


# ── Item 9: tier block ──────────────────────────────────────────────────


def test_valid_tiers_pass():
    tiers = [{"type": "early", "deadlineUTC": "2026-09-30", "entryFee": {"amount": 10, "currency": "EUR"}}]
    assert validate_contest_document({"timeline": {"tiers": tiers}}) == []


def test_invalid_tier_type_rejected():
    v = validate_contest_document(
        {"timeline": {"tiers": [{"type": "banana", "entryFee": {"amount": 5}}]}}
    )
    errs = _errors(v)
    assert any(x.field == "timeline.tiers[0].type" for x in errs)
    assert any("early" in (x.expected or "") for x in errs)


def test_tier_bad_amount_and_date_rejected():
    v = validate_contest_document(
        {
            "timeline": {
                "tiers": [
                    {"type": "late", "deadlineUTC": "sometime soon", "entryFee": {"amount": "free?"}}
                ]
            }
        }
    )
    fields = {x.field for x in _errors(v)}
    assert "timeline.tiers[0].deadlineUTC" in fields
    assert "timeline.tiers[0].entryFee.amount" in fields


# ── Item 10: edition block ──────────────────────────────────────────────


def test_valid_edition_passes():
    edition = {"label": "2027", "ordinal": 13, "cycleStart": "2026-10", "cycleEnd": "2027-04"}
    assert validate_contest_document({"edition": edition}) == []


def test_edition_bad_cycle_format_rejected():
    v = validate_contest_document({"edition": {"cycleStart": "October 2026"}})
    assert any(x.field == "edition.cycleStart" for x in _errors(v))


def test_edition_end_before_start_rejected():
    v = validate_contest_document(
        {"edition": {"cycleStart": "2027-04", "cycleEnd": "2026-10"}}
    )
    assert any(x.field == "edition.cycleEnd" for x in _errors(v))


def test_edition_ordinal_must_be_int():
    v = validate_contest_document({"edition": {"ordinal": "thirteenth"}})
    assert any(x.field == "edition.ordinal" for x in _errors(v))


# ── Update payload validation (all agent shapes) ────────────────────────


def test_update_payload_nested_object():
    v = validate_update_payload({"audience": {"mode": "onsite"}})
    assert [x.field for x in v] == ["audience.mode"]


def test_update_payload_dot_notation():
    v = validate_update_payload({"audience.mode": "virtual"})
    assert [x.field for x in v] == ["audience.mode"]


def test_update_payload_operator_form():
    v = validate_update_payload({"$set": {"location.scope": "onsite"}})
    assert [x.field for x in v] == ["location.scope"]


def test_update_payload_mixed_shapes_merge():
    v = validate_update_payload(
        {"audience": {"skillLevels": ["pro"]}, "audience.mode": "onsite"}
    )
    assert {x.field for x in v} == {"audience.skillLevels", "audience.mode"}


def test_update_payload_clean_passes():
    assert validate_update_payload({"audience": {"mode": "hybrid"}}) == []
    assert validate_update_payload({"$unset": {"eventEndUTC": ""}}) == []


def test_update_payload_ignores_untracked_fields():
    # Only canonical-schema fields are validated; arbitrary fields pass through
    assert validate_update_payload({"description": "x", "prize": {"totalUSD": 5}}) == []


# ── region_violation helper (shared with the patch validator) ───────────


def test_region_violation_canonical_passes():
    assert region_violation("European Union") is None
    assert region_violation("MENA") is None
    assert region_violation("UK & Ireland") is None


def test_region_violation_alias():
    reason, expected = region_violation("EU")
    assert reason == "non-canonical region alias"
    assert expected == "European Union"


def test_region_violation_subnational():
    reason, _ = region_violation("Oklahoma")
    assert "sub-national" in reason


def test_region_violation_unknown():
    reason, _ = region_violation("The Greater Pan-European Area")
    assert reason == "not a canonical region name"


def test_region_violation_non_string():
    reason, _ = region_violation(42)
    assert "expected a string" in reason


# ── Response shaping ────────────────────────────────────────────────────


def test_summarize_violations_shape():
    summary = summarize_violations(
        validate_contest_document(
            {
                "category": "Creative Arts",  # notice
                "location": {"scope": "onsite"},  # error
            }
        )
    )
    assert summary["error_count"] == 1
    assert summary["notice_count"] == 1
    assert summary["errors"][0]["field"] == "location.scope"
    assert summary["errors"][0]["value"] == "onsite"
    assert "expected" in summary["errors"][0]
    assert summary["notices"][0]["field"] == "category"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print("---")
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
