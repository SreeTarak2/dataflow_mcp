"""
Duplicate-title gate — shared between the MCP ingestion tools.

Prevents the same contest from being inserted twice under slightly different
titles or from different scrapers. Mirrors the backend's
`backend/scripts/dedupeContests.js` normalization so the gate and the backend
dedup jobs agree on what "the same title" means:

  - lowercase, keep only alphanumerics, then SORT the words
    ("Art on Climate International..." == "International Illustration
     Art on Climate...") while keeping years ("2026") in the key.

The gate treats a same-source + exact-title (case-insensitive) match as the
*intended re-submit / update* path of the existing upsert key
(`{source, title}`) — that is NOT flagged as a duplicate. Any other live
contest with the same normalized title (different source, or a reworded
title from the same source) IS flagged.
"""

import re
from typing import Any, Dict, List, Optional


def normalize_title(title: Any) -> str:
    """Normalize a title for duplicate grouping.

    Lowercase, keep only alphanumerics, split on whitespace and sort the
    words so word-reordered titles produce the same key. Returns "" for
    empty / non-string input (callers skip empty keys).
    """
    if not title or not isinstance(title, str):
        return ""
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip().split()
    return " ".join(sorted(words))


def source_name_of(doc: Dict[str, Any]) -> str:
    """Extract the source name from a stored contest document.

    The raw ingestion path stores `source` as a plain string; the structured
    submit path stores `source.name` (dict). Handles both, plus missing.
    """
    src = doc.get("source")
    if isinstance(src, dict):
        name = src.get("name")
        return name if isinstance(name, str) else ""
    return src if isinstance(src, str) else ""


def _source_matches(doc: Dict[str, Any], source_name: str, source_nested: bool) -> bool:
    """True when the existing doc's stored `source` has the SAME SHAPE the
    caller's upsert filter targets AND the same name.

    The raw ingestion path stores `source` as a plain string
    (filter: {"source": name}); the structured submit path stores
    `source.name` (filter: {"source.name": name}). The update-path exemption
    must only apply when the caller's filter would actually match the existing
    doc — otherwise the upsert would INSERT a new document instead of updating.
    """
    src = doc.get("source")
    if source_nested:
        return isinstance(src, dict) and src.get("name") == source_name
    return isinstance(src, str) and src == source_name


def build_title_index(collection) -> Dict[str, List[Dict[str, Any]]]:
    """Load all contests and group the LIVE ones by normalized title.

    Archived detection is done in Python (`if doc.get("archivedAt")`) so it
    also excludes legacy records whose archivedAt is a string timestamp or a
    boolean — Mongo's `{archivedAt: null}` only matches null/missing.

    Only _id/title/source/link/archivedAt are fetched (title-only projection
    keeps the scan cheap at the current ~900-doc scale). Returns
    {normalized_title: [doc, ...]} containing only live docs.
    """
    index: Dict[str, List[Dict[str, Any]]] = {}
    for doc in collection.find(
        {"title": {"$exists": True, "$ne": ""}},
        {"_id": 1, "title": 1, "source": 1, "link": 1, "archivedAt": 1},
    ):
        if doc.get("archivedAt"):
            # Any non-null archivedAt (Date, string, bool) means archived.
            continue
        norm = normalize_title(doc.get("title"))
        if not norm:
            continue
        index.setdefault(norm, []).append(doc)
    return index


def find_near_duplicates(
    title_index: Dict[str, List[Dict[str, Any]]],
    title: Any,
    source_name: Optional[str] = None,
    source_nested: bool = True,
) -> List[Dict[str, Any]]:
    """Return existing live contests that look like duplicates of `title`.

    The update-path exemption only applies when the existing doc would ACTUALLY
    be matched by the caller's upsert filter: exact-title (case-insensitive)
    AND a `source` of the same shape (`source_nested=True` → `source.name`
    dict, as written by submit_structured_records/submit_full_generation;
    `source_nested=False` → plain string, as written by process_raw_data).
    Any other live contest with the same normalized title is flagged.

    Returns a list of:
        {"_id": str, "title": str, "source": str|None, "link": str|None}
    """
    norm = normalize_title(title)
    if not norm:
        return []
    matches = title_index.get(norm, [])
    incoming_source = source_name or ""
    incoming_title = str(title or "").strip().lower()

    dups: List[Dict[str, Any]] = []
    for doc in matches:
        same_exact_title = str(doc.get("title") or "").strip().lower() == incoming_title
        if same_exact_title and _source_matches(doc, incoming_source, source_nested):
            # The exact doc the caller's upsert filter would hit — an update, not a dup.
            continue
        dups.append(
            {
                "_id": str(doc["_id"]),
                "title": doc.get("title"),
                "source": source_name_of(doc) or None,
                "link": doc.get("link"),
            }
        )
    return dups
