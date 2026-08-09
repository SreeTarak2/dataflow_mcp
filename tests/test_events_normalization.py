"""Unit tests for the events-v1.1 normalization helper.

Run with pytest:      pytest tests/test_events_normalization.py
Run standalone:      python tests/test_events_normalization.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataflow_mcp.core import _build_normalized_event  # noqa: E402


def _sample() -> dict:
    return {
        "type": "event",
        "source": {
            "name": "women_opp",
            "url": "https://example.com/conf",
            "scrapedAt": "2026-08-07T00:00:00Z",
        },
        "title": "Women in Tech Summit 2026!",
        "eventType": "summit",
        "eventDates": {"start": "2026-11-01T09:00:00", "end": "2026-11-02T18:00:00"},
        "registration": {"status": "open", "fee": 100, "currency": "USD"},
        "venue": {"mode": "online", "venueName": "Zoom"},
        "speakers": [{"name": "Ada Lovelace"}],
        "eventInsights": {"overallValue": 4, "difficultyLevel": "intermediate", "reasoning": "x"},
        "status": "published",
        "featured": "false",
        "analytics": {"views": "5", "registrations": 2},
        "metadata": {"searchLog": ["QUERY: x"]},
    }


def test_type_slug_and_link():
    norm, warnings = _build_normalized_event(_sample(), "women_opp", "2026-08-07T12:00:00Z")
    assert norm["type"] == "event"
    assert norm["slug"] == "women-in-tech-summit-2026"
    assert norm["link"] == "https://example.com/conf"
    assert norm["source"]["name"] == "women_opp"
    assert warnings == []


def test_status_visibility_featured_analytics():
    norm, _ = _build_normalized_event(_sample(), "women_opp", "2026-08-07T12:00:00Z")
    assert norm["status"] == "published"  # valid enum preserved
    assert norm["visibility"] == "public"  # default
    assert norm["featured"] is False  # string "false" not coerced to True
    assert norm["analytics"] == {"views": 5, "registrations": 2, "shares": 0}


def test_invalid_enums_downgraded_with_warning():
    bad = _sample()
    bad["registration"]["status"] = "bogus"
    bad["venue"]["mode"] = "everywhere"
    bad["eventInsights"]["overallValue"] = 9
    bad["status"] = "weird"
    norm, warnings = _build_normalized_event(bad, "women_opp", "2026-08-07T12:00:00Z")
    assert norm["registration"]["status"] is None
    assert norm["venue"]["mode"] is None
    assert norm["eventInsights"]["overallValue"] is None
    assert norm["status"] == "draft"
    assert len(warnings) >= 3


def test_input_not_mutated():
    sample = _sample()
    _build_normalized_event(sample, "women_opp", "2026-08-07T12:00:00Z")
    assert sample["registration"]["status"] == "open"  # caller's dict untouched
    assert sample["venue"]["mode"] == "online"


def test_analytics_safe_int():
    bad = _sample()
    bad["analytics"] = {"views": "5.5", "registrations": "free", "shares": 3}
    norm, _ = _build_normalized_event(bad, "women_opp", "2026-08-07T12:00:00Z")
    assert norm["analytics"] == {"views": 0, "registrations": 0, "shares": 3}


def test_metadata_stripped_unless_kept():
    norm, _ = _build_normalized_event(_sample(), "women_opp", "2026-08-07T12:00:00Z")
    assert "metadata" not in norm
    norm2, _ = _build_normalized_event(_sample(), "women_opp", "2026-08-07T12:00:00Z", keep_metadata=True)
    assert "metadata" in norm2
    assert norm2["metadata"]["searchLog"] == ["QUERY: x"]


def test_empty_source_name_fallback():
    rec = {"title": "Lonely Summit", "eventDates": {"start": "2026-12-01T00:00:00"}}
    norm, _ = _build_normalized_event(rec, "", "2026-08-07T12:00:00Z")
    # source.name is always present so the dedup filter always has a key
    assert norm["source"].get("name") == ""
    # and a supplied name wins when the record has no source block
    norm2, _ = _build_normalized_event(rec, "my_scraper", "2026-08-07T12:00:00Z")
    assert norm2["source"]["name"] == "my_scraper"


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
