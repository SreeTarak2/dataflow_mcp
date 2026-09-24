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

The module also provides `classify_replacement`, the safety guard behind the
`replace_contest` MCP tool: it decides whether old records sharing a title
with a surviving record may be auto-deleted (exact same title only) or must
go to human review (reworded titles). It is a pure function — no Mongo — so
it is unit-testable in isolation.
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


def _exact_title_key(title: Any) -> str:
    """Collapse a title for EXACT-match comparison (word order matters).

    Lowercase, replace every run of non-alphanumerics with one space, and
    collapse whitespace — but do NOT sort words. "WOLDA 2026!" and
    "wolda  2026" produce the same key; "Climate Art Award" and
    "Art Climate Award" do not. Returns "" for empty/non-string input.
    """
    if not title or not isinstance(title, str):
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def classify_replacement(
    survivor: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    target_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide which old records may be deleted when a record is replaced.

    This is the safety guard behind the ``replace_contest`` MCP tool. It is
    PURE — no database access — so callers pass in the candidate list (e.g.
    the live docs sharing the survivor's normalized title, from
    ``build_title_index``).

    Args:
        survivor: The NEW record that should be kept (must have a usable
            ``title``).
        candidates: Live records sharing the survivor's normalized title
            (may include the survivor itself).
        target_id: The survivor's ``_id`` (as a string). Candidates with
            this id are skipped, never queued for deletion.

    Verdicts:
        - ``safe_replace``: every other candidate matches the survivor's
          EXACT title (case/punctuation/whitespace-insensitive, same word
          order). Those ids are returned in ``delete_ids``.
        - ``review_required``: at least one candidate matches only after
          word-sorting (a reworded title) — the group is ambiguous, so
          ``delete_ids`` is empty and the fuzzy matches are listed under
          ``review`` for a human.
        - ``blocked``: no usable title, or no other live record shares the
          title (deleting would orphan the survivor's content, not remove a
          duplicate).

    Returns:
        ``{"verdict", "reason", "delete_ids", "review", "kept_id"}``
        where ``delete_ids`` are strings safe to archive+delete, and
        ``review`` entries carry ``{_id, title, source, link, match_type}``.
    """
    kept_id = str(target_id) if target_id else None
    incoming_key = _exact_title_key(survivor.get("title"))

    def _result(verdict: str, reason: str, delete_ids: List[str], review: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "verdict": verdict,
            "reason": reason,
            "delete_ids": delete_ids,
            "review": review,
            "kept_id": kept_id,
        }

    if not incoming_key:
        return _result(
            "blocked",
            "Surviving record has no usable title; refusing to classify a deletion.",
            [],
            [],
        )

    delete_ids: List[str] = []
    review: List[Dict[str, Any]] = []

    for doc in candidates:
        doc_id = str(doc.get("_id", ""))
        if kept_id and doc_id == kept_id:
            continue  # the surviving record itself — never a deletion target
        exact = _exact_title_key(doc.get("title")) == incoming_key
        entry = {
            "_id": doc_id,
            "title": doc.get("title"),
            "source": source_name_of(doc) or None,
            "link": doc.get("link"),
            "match_type": "exact_title" if exact else "reworded_title",
        }
        if exact:
            delete_ids.append(doc_id)
        else:
            review.append(entry)

    if review:
        return _result(
            "review_required",
            (
                f"{len(review)} record(s) match only after word-sorting "
                "(reworded title); group is ambiguous, human review required."
            ),
            [],
            review,
        )

    if not delete_ids:
        return _result(
            "blocked",
            "No other live record shares this title — there is no duplicate to delete.",
            [],
            [],
        )

    return _result(
        "safe_replace",
        (
            f"{len(delete_ids)} record(s) share the exact same title and are "
            "duplicates of the surviving record."
        ),
        delete_ids,
        [],
    )


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
