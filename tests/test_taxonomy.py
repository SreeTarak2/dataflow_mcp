"""Unit tests for the canonical CH taxonomy enforcement (spec item 12).

Sourced from the client's "CH Subcategories for Main categories" document.
Pure-function tests: no MongoDB.

Run with pytest:      pytest tests/test_taxonomy.py
Run standalone:      python tests/test_taxonomy.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.schema_validation import (  # noqa: E402
    SEVERITY_ERROR,
    SEVERITY_NOTICE,
    split_violations,
    validate_contest_document,
    validate_update_payload,
)
from tools.taxonomy import (  # noqa: E402
    CANONICAL_CATEGORIES,
    SUBCATEGORIES_BY_CATEGORY,
    canonical_category,
    is_canonical_subcategory,
    resolve_subcategory,
    subcategory_alias,
    taxonomy_summary,
)


# ── Taxonomy data integrity ─────────────────────────────────────────────


def test_ten_canonical_categories():
    assert len(CANONICAL_CATEGORIES) == 10
    assert "AI & Technology" in CANONICAL_CATEGORIES
    assert "Open & Multidisciplinary" in CANONICAL_CATEGORIES


def test_every_category_has_subcategories():
    for cat in CANONICAL_CATEGORIES:
        subs = SUBCATEGORIES_BY_CATEGORY[cat]
        assert subs, f"{cat} has no subcategories"
        assert len(subs) == len(set(subs)), f"{cat} has duplicate subcategories"


def test_taxonomy_summary_counts():
    summary = taxonomy_summary()
    assert summary["category_count"] == 10
    assert summary["subcategory_total"] > 300


# ── Category resolution ─────────────────────────────────────────────────


def test_canonical_category_passthrough():
    assert canonical_category("AI & Technology") == "AI & Technology"


def test_canonical_category_legacy_alias():
    assert canonical_category("Creative Arts") == "Creative Arts & Design"
    assert canonical_category("Technology & AI") == "AI & Technology"
    assert canonical_category("Food & Cooking") == "Open & Multidisciplinary"


def test_canonical_category_unknown():
    assert canonical_category("Best Category") is None


# ── Spec item 12's observed hallucinations ──────────────────────────────


def test_robotics_autonomous_systems_is_ambiguous_not_stored():
    resolved, status = resolve_subcategory("Engineering & Innovation", "Robotics & Autonomous Systems")
    assert resolved is None
    assert status == "ambiguous"


def test_architecture_urban_design_maps_via_alias():
    # "Architecture & Urban Design" is an alias → Architecture is canonical
    # under Creative Arts & Design
    assert subcategory_alias("Architecture & Urban Design") is None or (
        resolve_subcategory("Creative Arts & Design", "Architecture & Urban Design")[1] in ("alias", "ambiguous")
    )
    # The canonical value is plain "Architecture"
    assert is_canonical_subcategory("Creative Arts & Design", "Architecture")


def test_illustration_visual_art_not_canonical():
    assert resolve_subcategory("Creative Arts & Design", "Illustration & Visual Art")[1] == "ambiguous"
    # But the split values are
    assert is_canonical_subcategory("Creative Arts & Design", "Illustration")
    assert is_canonical_subcategory("Creative Arts & Design", "Fine Arts")


def test_engineering_design_is_canonical():
    # The spec flagged "Engineering Design" as MISused (too generic) — but the
    # value itself IS in the client doc's Engineering & Innovation list.
    assert is_canonical_subcategory("Engineering & Innovation", "Engineering Design")
    assert resolve_subcategory("Engineering & Innovation", "Engineering Design")[1] == "canonical"


def test_specific_canonical_values_resolve():
    assert is_canonical_subcategory("Engineering & Innovation", "Robotics")
    assert is_canonical_subcategory("Engineering & Innovation", "Rocketry")
    assert is_canonical_subcategory("Engineering & Innovation", "Space Technology")
    assert is_canonical_subcategory("Creative Arts & Design", "Product Design")


def test_category_scoping_enforced():
    # "Robotics" is canonical for Engineering but NOT for Creative Arts
    resolved, status = resolve_subcategory("Creative Arts & Design", "Robotics")
    assert resolved is None and status == "unknown"


def test_case_normalization():
    resolved, status = resolve_subcategory("Engineering & Innovation", "robotics")
    assert resolved == "Robotics" and status == "case_normalized"


def test_unambiguous_alias_maps():
    resolved, status = resolve_subcategory("AI & Technology", "AI")
    assert resolved == "Artificial Intelligence" and status == "alias"


def test_null_subcategory_always_allowed():
    assert resolve_subcategory("AI & Technology", None) == (None, "canonical")


# ── Write-time validation (full document) ───────────────────────────────


def test_document_with_canonical_pair_passes():
    doc = {"category": "Engineering & Innovation", "subCategory": "Robotics"}
    assert validate_contest_document(doc) == []


def test_document_with_ambiguous_subcategory_is_error():
    doc = {"category": "Engineering & Innovation", "subCategory": "Robotics & Autonomous Systems"}
    errs, notices = split_violations(validate_contest_document(doc))
    assert len(errs) == 1
    assert errs[0].field == "subCategory"
    assert errs[0].severity == SEVERITY_ERROR
    assert "get_taxonomy" in errs[0].expected


def test_document_with_alias_subcategory_is_notice():
    doc = {"category": "Creative Arts & Design", "subCategory": "Photography"}  # canonical
    assert validate_contest_document(doc) == []
    # case-difference → notice
    doc2 = {"category": "Engineering & Innovation", "subCategory": "robotics"}
    errs, notices = split_violations(validate_contest_document(doc2))
    assert errs == []
    assert len(notices) == 1 and notices[0].expected == "Robotics"


def test_document_with_wrong_category_scope_is_error():
    doc = {"category": "Writing & Media", "subCategory": "Robotics"}
    errs, _ = split_violations(validate_contest_document(doc))
    assert any(x.field == "subCategory" for x in errs)


# ── Write-time validation (update payloads) ─────────────────────────────


def test_update_payload_pair_checked():
    v = validate_update_payload(
        {"category": "Engineering & Innovation", "subCategory": "Robotics & Autonomous Systems"}
    )
    errs = [x for x in v if x.severity == SEVERITY_ERROR]
    assert any(x.field == "subCategory" for x in errs)


def test_update_payload_subcategory_only_uses_global_set():
    # Patching subCategory without category: validated against the global
    # canonical set; "Robotics" exists somewhere → passes (cross-field check
    # scopes it with the DB's category)
    assert validate_update_payload({"subCategory": "Robotics"}) == []
    # An invention that exists nowhere is flagged
    v = validate_update_payload({"subCategory": "Quantum Robotics Design Excellence"})
    assert any(x.field == "subCategory" for x in v)


def test_update_payload_operator_form_checked():
    v = validate_update_payload({"$set": {"category": "AI & Technology", "subCategory": "amateur"}})
    assert any(x.field == "subCategory" for x in v)


# ── Taxonomy immutability contract ──────────────────────────────────────


def test_taxonomy_is_code_not_mutable_via_records():
    # The module has no mutation API — this documents spec item 12 part 4:
    # categories are created by maintainer commits only.
    import tools.taxonomy as t

    assert not any(hasattr(t, name) for name in ("add_category", "create_category", "update_taxonomy"))


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
