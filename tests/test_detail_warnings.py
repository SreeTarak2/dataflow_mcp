"""Unit tests for ContestDetailGenerator.validate() warning semantics (spec
item 3) and DataManager._expand_set_paths merge semantics (spec item 2).

readingTime must be a non-blocking notice (the server computes it anyway) and
the first-person check must not flag 'US'/'USA' country references.

Pure-function tests: no MongoDB — the generator is instantiated via __new__
and the DataManager helper is a static method.

Run with pytest:      pytest tests/test_detail_warnings.py
Run standalone:      python tests/test_detail_warnings.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.contest_detail_generator import ContestDetailGenerator  # noqa: E402
from tools.data_manager import DataManager  # noqa: E402

GEN = ContestDetailGenerator.__new__(ContestDetailGenerator)


def _base_content(**overrides) -> dict:
    content = {
        "whyJoin": "Students gain real-world experience, mentorship, and exposure "
        "to industry partners while building a portfolio project that matters. " * 2,
        "whoShouldApply": "Undergraduate and graduate students in engineering or design.",
        "benefits": ["Cash prizes", "Mentorship", "Industry exposure"],
        "tips": ["Read the rules early", "Start with the rubric"],
        "readingTime": 1,
    }
    content.update(overrides)
    return content


def _validate(content: dict) -> dict:
    return GEN.validate({"content": content, "seo": {}}, {"link": "https://example.com"})


# ── readingTime (item 3) ────────────────────────────────────────────────


def test_reading_time_wrong_value_is_notice_not_warning():
    content = _base_content(readingTime=99)
    result = _validate(content)
    assert not any("readingTime" in w for w in result["warnings"])
    assert any("readingTime" in n and "no resubmission needed" in n for n in result["notices"])
    # computed value stored in place
    assert content["readingTime"] == 1


def test_reading_time_missing_is_notice_not_warning():
    content = _base_content()
    del content["readingTime"]
    result = _validate(content)
    assert not any("readingTime" in w for w in result["warnings"])
    assert any("auto-set" in n for n in result["notices"])
    assert content["readingTime"] >= 1


def test_reading_time_correct_produces_no_notice():
    content = _base_content(readingTime=1)
    result = _validate(content)
    assert not any("readingTime" in n for n in result["notices"])


def test_reading_time_not_counted_in_warning_count():
    with_bad_rt = _base_content(readingTime=99)
    r1 = _validate(with_bad_rt)
    without_rt = _base_content()
    del without_rt["readingTime"]
    r2 = _validate(without_rt)
    # warning_count identical whether readingTime is wrong or missing
    assert r1["warning_count"] == r2["warning_count"]


# ── First-person check (item 3) ─────────────────────────────────────────


def test_us_country_reference_not_flagged():
    content = _base_content(
        whyJoin="US students and USA teams can apply for the United States track. " * 4
    )
    result = _validate(content)
    assert not any("First-person" in n for n in result["notices"]), result["notices"]


def test_genuine_first_person_still_flagged_as_notice():
    content = _base_content(whyJoin="We love this contest and our team welcomes you. " * 4)
    result = _validate(content)
    assert any("First-person" in n for n in result["notices"])
    # style issue → notice, never a warning
    assert not any("First-person" in w for w in result["warnings"])


def test_first_person_check_runs_in_list_sections():
    content = _base_content(
        benefits=["You get mentorship", "Our partners offer internships", "Cash prizes"]
    )
    result = _validate(content)
    assert any("First-person" in n and "benefits[1]" in n for n in result["notices"])


def test_us_in_list_section_not_flagged():
    content = _base_content(benefits=["Open to US applicants", "Mentorship", "Cash prizes"])
    result = _validate(content)
    assert not any("First-person" in n for n in result["notices"])


# ── Notices don't gate quality ──────────────────────────────────────────


def test_notices_do_not_fail_validation():
    content = _base_content(readingTime=99, whyJoin="We and our team welcome you. " * 6)
    result = _validate(content)
    assert result["valid"] is True
    assert result["notice_count"] >= 2


def test_real_warnings_still_fail_validation():
    content = _base_content(
        benefits=["only one"],
        tips=[],
        whoShouldApply="x",
        faq=[{"question": "no answer"}],
        submissionGuide=[{"answer": "no step"}],
    )
    seo = {"metaTitle": "T" * 80, "metaDescription": "D" * 200}
    result = GEN.validate({"content": content, "seo": seo}, {"link": "https://example.com"})
    assert result["warning_count"] >= 5
    assert result["valid"] is False


# ── DataManager deep-merge expansion (item 2) ───────────────────────────


def test_expand_set_paths_deep_merges_nested_objects():
    flat = DataManager._expand_set_paths({"audience": {"mode": "hybrid"}})
    assert flat == {"audience.mode": "hybrid"}


def test_expand_set_paths_preserves_siblings_semantics():
    # The dotted path means $set only touches that sub-field — sibling
    # fields (audience.eligibilityLabel etc.) are never sent to Mongo.
    flat = DataManager._expand_set_paths(
        {"audience": {"mode": "hybrid"}, "title": "T"}
    )
    assert flat == {"audience.mode": "hybrid", "title": "T"}


def test_expand_set_paths_arrays_are_leaves():
    flat = DataManager._expand_set_paths({"audience": {"skillLevels": ["beginner"]}})
    assert flat == {"audience.skillLevels": ["beginner"]}


def test_expand_set_paths_empty_dict_is_leaf():
    flat = DataManager._expand_set_paths({"prize": {}})
    assert flat == {"prize": {}}


def test_expand_set_paths_null_is_leaf():
    flat = DataManager._expand_set_paths({"timeline": {"eventEndUTC": None}})
    assert flat == {"timeline.eventEndUTC": None}


def test_expand_set_paths_deep_nesting():
    flat = DataManager._expand_set_paths(
        {"audience": {"constraints": {"teamSize": {"max": 4}}}}
    )
    assert flat == {"audience.constraints.teamSize.max": 4}


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
