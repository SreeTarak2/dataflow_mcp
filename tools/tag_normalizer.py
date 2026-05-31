"""Tag normalization utilities for contest documents.

Rules implemented (conservative):
- Trim, lowercase, replace whitespace with hyphens
- Remove generic/deny-list tags
- Collapse known variants and normalize common plurals
- Remove duplicates and limit to 3 tags
"""
from typing import List, Optional

# Lightweight deny list — keep in sync with TAG_NORMALIZATION_PLAN.md
DENY_LIST = {
    'general', 'open', 'annual', 'monthly', 'weekly', 'competition', 'awards',
    'prizes', 'recognition', 'global', 'international', 'talent', 'excellence',
    'creative', 'design', 'art', 'photography', 'video', 'digital', 'emerging',
    'festival', 'exhibition', 'showcase', 'contest', 'prize'
}

# Known plural -> singular canonical forms
KNOWN_PLURALS = {
    'awards': 'award',
    'grants': 'grant',
    'prizes': 'prize',
    'students': 'student',
    'startups': 'startup',
    'artists': 'artist',
}

# Variant collapse map (common noisy variants -> canonical)
VARIANT_COLLAPSE = {
    'short film': 'short-film',
    'short-film': 'short-film',
    'social impact': 'social-impact',
    'street photography': 'street-photography',
    'mobile photography': 'mobile-photography',
    'graphic design': 'graphic-design',
}


def _clean(tag: str) -> str:
    t = tag.strip().lower()
    # replace whitespace with single hyphen
    t = "-".join(t.split())
    # collapse duplicate hyphens
    while "--" in t:
        t = t.replace("--", "-")
    # strip leading/trailing hyphens
    t = t.strip("-")
    return t


def normalize_tag(tag: str) -> Optional[str]:
    if not isinstance(tag, str):
        return None
    t = _clean(tag)
    if not t:
        return None

    # Collapse common variants
    if t in VARIANT_COLLAPSE:
        t = VARIANT_COLLAPSE[t]

    # Map known plurals
    if t in KNOWN_PLURALS:
        t = KNOWN_PLURALS[t]

    # Remove deny-list items
    if t in DENY_LIST:
        return None

    return t


def normalize_tags_array(tags: List[str], max_tags: int = 3) -> List[str]:
    seen = set()
    out: List[str] = []
    if not tags:
        return out
    for raw in tags:
        n = normalize_tag(raw)
        if not n:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= max_tags:
            break
    return out
