"""Unit tests for the EventDetailGenerator validate() quality checks.

Run with pytest:      pytest tests/test_event_detail_generator.py
Run standalone:      python tests/test_event_detail_generator.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.event_detail_generator import EventDetailGenerator  # noqa: E402

EVENT_DATA = {
    "_id": "abc",
    "title": "Women in Tech Summit 2026",
    "link": "https://example.com/summit",
    "source": {"name": "women_opp", "url": "https://example.com/summit"},
    "eventType": "summit",
    "status": "published",
    "eventDates": {"start": "2026-11-01T09:00:00Z"},
}


def _valid_details() -> dict:
    return {
        "content": {
            "hero": {"subheadline": "A summit for women in tech", "valueProposition": "Real"},
            "whyAttend": (
                "This three-day summit brings together engineers, product managers, and "
                "founders for keynotes, hands-on workshops, and networking sessions. "
                "Past editions have featured executives from major technology firms and "
                "attendees report valuable career connections. The agenda includes a "
                "mentorship track and an investor office-hours session."
            ),
            "whoShouldAttend": (
                "Engineers, product managers, founders, and students early in their "
                "technology careers who want to grow their network."
            ),
            "benefits": [
                "Access to keynote talks from industry executives",
                "Hands-on workshops on career growth",
                "Networking sessions with recruiters",
                "Mentorship track",
            ],
            "tips": [
                "Book travel early — the venue is in a busy conference district",
                "Bring printed resumes for the career fair",
            ],
            "shouldYouAttend": {
                "idealFor": "Women in tech at any career stage",
                "goodFit": ["Early-career engineers", "Career switchers"],
                "notIdealFor": ["Those seeking job offers only"],
            },
            "faq": [
                {
                    "question": "Is the summit in person?",
                    "answer": "Yes, it is an in-person event in Lisbon.",
                }
            ],
            "readingTime": 3,
        },
        "seo": {
            "metaTitle": "Women in Tech Summit 2026",
            "metaDescription": "A three-day summit for women in tech with keynotes, workshops, and networking.",
            "keywords": ["women in tech", "summit", "career growth"],
        },
    }


def test_valid_details_passes():
    gen = EventDetailGenerator()
    result = gen.validate(_valid_details(), EVENT_DATA)
    assert result["valid"] is True
    assert result["total_words"] > 50


def test_short_why_attend_warns():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["content"]["whyAttend"] = "It is a conference."
    result = gen.validate(details, EVENT_DATA)
    assert any("whyAttend" in w for w in result["warnings"])


def test_first_person_language_warns():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["content"]["whyAttend"] += " We believe this is a great event for us."
    result = gen.validate(details, EVENT_DATA)
    assert any("First-person" in w for w in result["warnings"])


def test_unverified_url_warns():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["content"]["whyAttend"] += " See https://suspicious-example-xyz.com for details."
    result = gen.validate(details, EVENT_DATA)
    assert any("Unverified URL" in w for w in result["warnings"])


def test_official_domain_url_allowed():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["content"]["whyAttend"] += " See https://example.com/summit/tickets for tickets."
    result = gen.validate(details, EVENT_DATA)
    assert not any("Unverified URL" in w for w in result["warnings"])


def test_faq_structure_warns():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["content"]["faq"] = [{"question": "no answer here"}]
    result = gen.validate(details, EVENT_DATA)
    assert any("faq[0]" in w for w in result["warnings"])


def test_seo_length_warns():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["seo"]["metaTitle"] = "x" * 80
    result = gen.validate(details, EVENT_DATA)
    assert any("metaTitle" in w for w in result["warnings"])


def test_reading_time_recomputed():
    gen = EventDetailGenerator()
    details = _valid_details()
    details["content"]["readingTime"] = 99  # wrong on purpose
    result = gen.validate(details, EVENT_DATA)
    expected = max(1, -(-result["total_words"] // 200))
    assert result["content"]["readingTime"] == expected
    assert any("readingTime" in w for w in result["warnings"])


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
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    print("---")
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
