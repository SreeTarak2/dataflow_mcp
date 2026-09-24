"""Unit tests for classify_replacement — the replace_contest safety guard.

Pure-function tests: no MongoDB, no network. Mirrors the style of
tests/test_events_normalization.py (pytest, with a standalone runner).

Run with pytest:      pytest tests/test_replace_guard.py
Run standalone:      python tests/test_replace_guard.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dedup_gate import classify_replacement  # noqa: E402


def _doc(_id: str, title: str) -> dict:
    return {
        "_id": _id,
        "title": title,
        "source": {"name": "wolda"},
        "link": f"https://example.com/{_id}",
    }


def test_safe_replace_exact_title_duplicates():
    survivor = _doc("new1", "WOLDA 2026")
    candidates = [
        _doc("new1", "WOLDA 2026"),
        _doc("old1", "WOLDA 2026!"),
        _doc("old2", "wolda  2026"),
    ]
    result = classify_replacement(survivor, candidates, target_id="new1")
    assert result["verdict"] == "safe_replace"
    assert sorted(result["delete_ids"]) == ["old1", "old2"]
    assert result["review"] == []


def test_survivor_self_is_never_a_deletion_target():
    survivor = _doc("new1", "WOLDA 2026")
    result = classify_replacement(survivor, [survivor], target_id="new1")
    assert result["verdict"] == "blocked"  # no duplicate, not "delete itself"
    assert result["delete_ids"] == []


def test_reworded_title_requires_review():
    survivor = _doc("new1", "Climate Art Award 2026")
    candidates = [
        _doc("new1", "Climate Art Award 2026"),
        _doc("old1", "Art Climate Award 2026"),  # same words, reordered
        _doc("old2", "Climate Art Award 2026 — Global Edition"),  # extra words
    ]
    result = classify_replacement(survivor, candidates, target_id="new1")
    assert result["verdict"] == "review_required"
    assert result["delete_ids"] == []  # nothing deleted when ambiguous
    assert {e["_id"] for e in result["review"]} == {"old1", "old2"}
    assert all(e["match_type"] == "reworded_title" for e in result["review"])


def test_no_duplicate_is_blocked():
    survivor = _doc("new1", "Unique Brand New Contest")
    result = classify_replacement(survivor, [survivor], target_id="new1")
    assert result["verdict"] == "blocked"
    assert result["delete_ids"] == []


def test_missing_survivor_title_is_blocked():
    survivor = {"_id": "new1", "title": ""}
    result = classify_replacement(survivor, [_doc("old1", "Anything")], target_id="new1")
    assert result["verdict"] == "blocked"
    assert result["delete_ids"] == []


def test_no_target_id_still_classifies():
    survivor = _doc("new1", "WOLDA 2026")
    old = _doc("old1", "WOLDA 2026")
    result = classify_replacement(survivor, [survivor, old], target_id=None)
    # Without target_id the survivor itself would be queued — acceptable for
    # the pure classifier, but the MCP tool always passes target_id.
    assert result["verdict"] == "safe_replace"
    assert "old1" in result["delete_ids"]


def test_archived_candidates_are_callers_responsibility():
    # The guard is pure: it never sees archived docs because build_title_index
    # already excludes them. Documenting the contract here.
    survivor = _doc("new1", "WOLDA 2026")
    result = classify_replacement(survivor, [], target_id="new1")
    assert result["verdict"] == "blocked"


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
