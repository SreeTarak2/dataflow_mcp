"""Unit tests for the restructure path (spec: "AI missing fields on
restructuring").

Covers:
  - the extended ALLOWED_PATCH_FIELDS (v4.4 fields must be patchable)
  - PatchValidator checks for the new fields
  - _detect_legacy_gaps (field-gap scan used by get_contest_for_restructuring)

Pure-function tests: no MongoDB.

Run with pytest:      pytest tests/test_restructure_path.py
Run standalone:      python tests/test_restructure_path.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.contest_migration import (  # noqa: E402
    ALLOWED_PATCH_FIELDS,
    PatchValidator,
)


# ── Whitelist: v4.4 fields must be patchable ────────────────────────────


def test_v44_fields_are_whitelisted():
    required = {
        "flags",
        "audienceScope",
        "source",
        "source.name",
        "source.url",
        "source.type",
        "filterKeys",
        "filterKeys.domain",
        "filterKeys.format",
        "filterKeys.medium",
        "filterKeys.themes",
        "timeline.organizerTimeZone",
        "timeline.tiers",
        "edition",
        "edition.label",
        "edition.ordinal",
        "edition.cycleStart",
        "edition.cycleEnd",
    }
    missing = required - ALLOWED_PATCH_FIELDS
    assert not missing, f"v4.4 fields missing from patch whitelist: {missing}"


def test_full_restructure_patch_passes_whitelist():
    """A complete restructured record must survive verification 1 intact —
    this is the exact patch shape the restructure flow produces."""
    patch = {
        "edition": {"label": "2027", "ordinal": 13, "cycleStart": "2026-10", "cycleEnd": "2027-04"},
        "timeline.tiers": [
            {"type": "early", "deadlineUTC": "2026-09-30", "entryFee": {"amount": 10, "currency": "EUR"}},
            {"type": "final", "deadlineUTC": "2027-01-17", "entryFee": {"amount": 25, "currency": "EUR"}},
        ],
        "timeline.organizerTimeZone": "Europe/Berlin",
        "filterKeys": {"domain": "creative-arts-design", "format": ["image"], "medium": ["digital"], "themes": ["photography"]},
        "flags": ["all"],
        "audienceScope": "all",
        "source": {"name": "Fine Art Photography Awards", "url": "https://fapa.com", "type": "official"},
        "location.scope": "worldwide",
        "participationGeography.scope": "worldwide",
    }
    result = PatchValidator.check_field_whitelist(patch)
    assert result.errors == [], result.errors


# ── Schema checks for the new fields ────────────────────────────────────


def test_source_type_enum_checked():
    result = PatchValidator.check_schema_compliance({"source.type": "blog"})
    assert result.errors and "source.type" in result.errors[0]
    result_ok = PatchValidator.check_schema_compliance({"source.type": "official"})
    assert result_ok.errors == []


def test_audience_scope_enum_checked():
    result = PatchValidator.check_schema_compliance({"audienceScope": "everyone"})
    assert result.errors and "audienceScope" in result.errors[0]
    assert PatchValidator.check_schema_compliance({"audienceScope": "women"}).errors == []
    assert PatchValidator.check_schema_compliance({"audienceScope": None}).errors == []


def test_flags_enum_checked():
    result = PatchValidator.check_schema_compliance({"flags": ["women", "vip"]})
    assert result.errors and "vip" in result.errors[0]
    assert PatchValidator.check_schema_compliance({"flags": ["hero", "all"]}).errors == []


def test_filter_keys_shape_checked():
    result = PatchValidator.check_schema_compliance({"filterKeys.themes": "not-a-list"})
    assert result.errors and "filterKeys.themes" in result.errors[0]
    assert PatchValidator.check_schema_compliance({"filterKeys": {"domain": "x"}}).errors == []


def test_organizer_timezone_checked():
    result = PatchValidator.check_schema_compliance({"timeline.organizerTimeZone": 42})
    assert result.errors
    assert PatchValidator.check_schema_compliance(
        {"timeline.organizerTimeZone": "Asia/Kolkata"}
    ).errors == []


# ── Legacy-gap detection ────────────────────────────────────────────────


def _contest(**overrides) -> dict:
    base = {
        "_id": "6aae",
        "title": "Old-schema contest",
        "timeline": {"submissionDeadlineUTC": "2027-01-17"},
        "audience": {},
        "location": {},
        "prize": {},
        "entry": {},
    }
    base.update(overrides)
    return base


def test_gap_detection_on_old_schema_record():
    gaps = _detect_legacy_gaps_safe(
        _contest(
            audience={"location": "Online"},  # deprecated string
            prizeSummary="$10k",  # legacy top-level
            feeConfidence="unknown",  # legacy top-level
        )
    )
    assert "edition" in gaps["missing"]
    assert "timeline.tiers" in gaps["missing"]
    assert "filterKeys" in gaps["missing"]
    assert "audience.skillLevels" in gaps["missing"]
    assert any("audience.location" in g for g in gaps["legacy"])
    assert any("top-level prizeSummary" in g for g in gaps["legacy"])
    assert any("top-level feeConfidence" in g for g in gaps["legacy"])


def test_gap_detection_clean_v44_record_has_no_missing():
    contest = _contest(
        edition={"label": "2027", "cycleStart": "2026-10", "cycleEnd": "2027-04"},
        timeline={
            "submissionDeadlineUTC": "2027-01-17",
            "tiers": [{"type": "final", "deadlineUTC": "2027-01-17", "entryFee": {"amount": 0}}],
        },
        filterKeys={"domain": "x"},
        flags=["all"],
        descriptionDetailed="x" * 100,
        audience={
            "skillLevels": ["beginner"],
            "eligibilityLabel": "Open to students",
        },
        location={"scope": "worldwide"},
        participationGeography={"scope": "worldwide"},
        prize={"prizeSummary": "Cash prizes"},
        entry={"feeConfidence": "confirmed"},
    )
    gaps = _detect_legacy_gaps_safe(contest)
    assert gaps["missing"] == [], gaps["missing"]
    assert gaps["legacy"] == []


def test_gap_detection_legacy_tier_keys():
    gaps = _detect_legacy_gaps_safe(
        _contest(timeline={"earlyBirdDeadlineUTC": "2026-09-30", "lateDeadlineUTC": "2027-01-17"})
    )
    assert any("timeline.tiers" in g or "ad-hoc tier" in g for g in gaps["legacy"])


def _detect_legacy_gaps_safe(contest: dict) -> dict:
    # Import here so the test file also works when only tools/ is importable
    from dataflow_mcp.tools.migration import _detect_legacy_gaps

    return _detect_legacy_gaps(contest)


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
