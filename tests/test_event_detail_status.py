"""Unit tests for the get_event_detail_status MCP tool wrapper.

The wrapper surfaces EventDetailGenerator.get_status() coverage metrics.
We monkeypatch the generator method so the tests run without a database.

Run with pytest:      pytest tests/test_event_detail_status.py
Run standalone:      python tests/test_event_detail_status.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataflow_mcp.tools.events import get_event_detail_status  # noqa: E402

FAKE_STATUS = {
    "success": True,
    "total_events": 100,
    "total_with_details": 58,
    "total_without_details": 42,
    "by_status": {"completed": 58, "failed": 4},
    "coverage_pct": 58.0,
}


def test_surfaces_coverage_metrics():
    with patch(
        "dataflow_mcp.tools.events.EventDetailGenerator.get_status",
        return_value=FAKE_STATUS,
    ):
        result = get_event_detail_status()

    assert result["success"] is True
    assert result["collection"] == "event_details"
    assert result["total_events"] == 100
    assert result["total_with_details"] == 58
    assert result["total_without_details"] == 42
    assert result["by_status"] == {"completed": 58, "failed": 4}
    assert result["coverage_pct"] == 58.0
    # Chatbots get a hint on what to do next
    assert "get_events_for_detail_generation" in result["usage"]["purpose"]


def test_propagates_generator_failure():
    with patch(
        "dataflow_mcp.tools.events.EventDetailGenerator.get_status",
        return_value={"success": False, "error": "aggregation exploded"},
    ):
        result = get_event_detail_status()

    assert result["success"] is False
    assert result["error"] == "aggregation exploded"


def test_handles_partial_status_dict():
    # Generator always returns all keys, but the wrapper should not crash
    # even if a future version omits one.
    partial = {"success": True, "total_events": 3}
    with patch(
        "dataflow_mcp.tools.events.EventDetailGenerator.get_status",
        return_value=partial,
    ):
        result = get_event_detail_status()

    assert result["success"] is True
    assert result["total_events"] == 3
    assert result["total_with_details"] == 0
    assert result["total_without_details"] == 0
    assert result["by_status"] == {}
    assert result["coverage_pct"] == 0.0


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
