"""
Canonical CH taxonomy — single source of truth for categories & subcategories
(spec item 12).

Sourced verbatim from the client's "CH Subcategories for Main categories"
document (2026-09). Enforced on every write path:

  - ``get_taxonomy`` MCP tool          (dataflow_mcp/tools/migration.py)
  - ``validate_contest_document``      (tools/schema_validation.py)
  - ``PatchValidator`` cross-field     (tools/contest_migration.py)
  - ingestion auto-mapping             (dataflow_mcp/core.py)

Design decisions (documented, per spec item 12):
  - NEVER allow category creation from record writes: the taxonomy here is
    code — it changes only via a maintainer commit, never as a side effect of
    writing a record.
  - Scraper/AI inventions are mapped to canonical values via SUBCATEGORY_ALIASES
    when the mapping is unambiguous; ambiguous inventions are flagged, never
    stored.
  - The client doc's section 10 gives "Open & Multidisciplinary" subcategories
    while its conclusion says to avoid it as a main category. The platform
    currently uses the category, so it is kept WITH its list; the doc's own
    suggested format attribute ("Multidisciplinary") is preserved for the
    platform team in ``OPEN_CATEGORY_NOTE``.

Import safety: pure data + pure functions — no MongoDB, no config imports.
"""

from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────
# Canonical categories (verbatim from the client document)
# ─────────────────────────────────────────────────────────────────────────

CANONICAL_CATEGORIES: List[str] = [
    "AI & Technology",
    "Engineering & Innovation",
    "Business & Entrepreneurship",
    "Science & Research",
    "Creative Arts & Design",
    "Writing & Media",
    "Environment & Sustainability",
    "Education & Learning",
    "Social Impact & Leadership",
    "Open & Multidisciplinary",
]

# The client doc's conclusion recommends handling multidisciplinary via a
# format attribute rather than a category. Kept for platform-team reference;
# does not affect validation.
OPEN_CATEGORY_NOTE = (
    "Client doc suggests multidisciplinary opportunities may be better served "
    "by a format attribute ('Multidisciplinary') than a main category. The "
    "category is kept until the platform team decides otherwise."
)

# ─────────────────────────────────────────────────────────────────────────
# Canonical subcategories, verbatim per category
# ─────────────────────────────────────────────────────────────────────────

SUBCATEGORIES_BY_CATEGORY: Dict[str, List[str]] = {
    "AI & Technology": [
        "Artificial Intelligence",
        "Machine Learning",
        "Deep Learning",
        "Generative AI",
        "Natural Language Processing",
        "Computer Vision",
        "Data Science",
        "Data Analytics",
        "Big Data",
        "Software Development",
        "Web Development",
        "Mobile App Development",
        "Cloud Computing",
        "Cybersecurity",
        "Ethical Hacking",
        "Blockchain & Web3",
        "Internet of Things",
        "AR / VR / XR",
        "Quantum Computing",
        "Automation",
        "Robotics Software",
        "Algorithms & Programming",
        "Open Source",
        "Developer Tools",
        "Software Engineering",
        "Digital Transformation",
        "Information Technology",
        "Technology Innovation",
    ],
    "Engineering & Innovation": [
        "Mechanical Engineering",
        "Electrical Engineering",
        "Electronics",
        "Civil Engineering",
        "Chemical Engineering",
        "Aerospace Engineering",
        "Aeronautical Engineering",
        "Automotive Engineering",
        "Robotics",
        "Mechatronics",
        "Embedded Systems",
        "Hardware Design",
        "Drones & UAVs",
        "Rocketry",
        "Space Technology",
        "Rovers & Space Robotics",
        "Satellites",
        "Electric Vehicles",
        "Autonomous Vehicles",
        "Formula & Racing",
        "3D Printing",
        "Additive Manufacturing",
        "CAD & 3D Modelling",
        "Manufacturing",
        "Industrial Engineering",
        "Structural Engineering",
        "Construction Technology",
        "Renewable Energy Technology",
        "Battery Technology",
        "Semiconductor & Chip Design",
        "Nanotechnology",
        "Smart Devices",
        "Product Engineering",
        "Deep Tech",
        "Prototype Development",
        "Engineering Design",
        "Industrial Innovation",
    ],
    "Business & Entrepreneurship": [
        "Entrepreneurship",
        "Startups",
        "Startup Pitching",
        "Business Plans",
        "Business Strategy",
        "Business Case Studies",
        "Product Management",
        "Product Strategy",
        "Marketing",
        "Digital Marketing",
        "Advertising",
        "Branding",
        "Sales",
        "Business Development",
        "Finance",
        "Investment",
        "Venture Capital",
        "FinTech",
        "Banking",
        "Insurance",
        "Economics",
        "Consulting",
        "Market Research",
        "Consumer Research",
        "E-commerce",
        "Retail",
        "Supply Chain",
        "Operations",
        "Human Resources",
        "Leadership & Management",
        "Business Analytics",
        "Social Entrepreneurship",
        "Innovation Management",
        "Corporate Strategy",
        "Entrepreneurship Fellowships",
        "Incubation & Acceleration",
    ],
    "Science & Research": [
        "Biology",
        "Biotechnology",
        "Microbiology",
        "Genetics & Genomics",
        "Molecular Biology",
        "Chemistry",
        "Biochemistry",
        "Physics",
        "Astronomy",
        "Astrophysics",
        "Space Science",
        "Earth Science",
        "Geology",
        "Ocean Science",
        "Life Sciences",
        "Neuroscience",
        "Medical Science",
        "Medical Research",
        "Biomedical Research",
        "Public Health",
        "Epidemiology",
        "Pharmaceutical Research",
        "Drug Discovery",
        "Clinical Research",
        "Nutrition & Food Science",
        "Agriculture Science",
        "Plant Science",
        "Animal Science",
        "Materials Science",
        "Environmental Science",
        "Scientific Computing",
        "Scientific Experiments",
        "Laboratory Research",
        "Research Papers",
        "Research Posters",
        "Science Communication",
        "Scientific Innovation",
        "Research Fellowships",
    ],
    "Creative Arts & Design": [
        "Graphic Design",
        "UI/UX Design",
        "Product Design",
        "Industrial Design",
        "Illustration",
        "Digital Art",
        "Fine Arts",
        "Drawing",
        "Painting",
        "Photography",
        "Photo Editing",
        "Videography",
        "Video Editing",
        "Animation",
        "2D Animation",
        "3D Animation",
        "Motion Graphics",
        "Film Making",
        "Short Films",
        "Visual Storytelling",
        "Fashion Design",
        "Interior Design",
        "Architecture",
        "Spatial Design",
        "Poster Design",
        "Logo Design",
        "Brand Identity",
        "Typography",
        "Comics",
        "Character Design",
        "3D Modelling",
        "Game Art",
        "Game Design",
        "Creative Campaigns",
        "Creative Technology",
        "Craft & Handicrafts",
    ],
    "Writing & Media": [
        "Creative Writing",
        "Essay Writing",
        "Article Writing",
        "Blog Writing",
        "Poetry",
        "Fiction",
        "Short Stories",
        "Script Writing",
        "Screenwriting",
        "Journalism",
        "Investigative Journalism",
        "News Writing",
        "Content Writing",
        "Copywriting",
        "Technical Writing",
        "Editorial Writing",
        "Speech Writing",
        "Storytelling",
        "Public Speaking",
        "Debate",
        "Podcasting",
        "Radio",
        "Audio Production",
        "Documentary",
        "Digital Media",
        "Newsletters",
        "Publishing",
        "Books & Manuscripts",
        "Literary Competitions",
        "Content Creation",
        "Social Media Content",
        "Video Storytelling",
        "Media Production",
    ],
    "Environment & Sustainability": [
        "Climate Change",
        "Climate Action",
        "Sustainability",
        "Environmental Protection",
        "Renewable Energy",
        "Solar Energy",
        "Wind Energy",
        "Clean Energy",
        "Energy Efficiency",
        "Carbon Reduction",
        "Carbon Capture",
        "Net Zero",
        "Green Technology",
        "Waste Management",
        "Recycling",
        "Plastic Waste",
        "E-waste",
        "Water Conservation",
        "Water Management",
        "Clean Water & Sanitation",
        "Air Quality",
        "Air Pollution",
        "Soil Conservation",
        "Sustainable Agriculture",
        "Biodiversity",
        "Wildlife Conservation",
        "Forest Conservation",
        "Marine Conservation",
        "Ocean Sustainability",
        "Sustainable Cities",
        "Sustainable Transportation",
        "Circular Economy",
        "Sustainable Products",
        "Sustainable Construction",
        "Disaster Management",
        "Flood Management",
        "Drought Management",
        "Environmental Monitoring",
        "Nature-Based Solutions",
        "Green Buildings",
        "Climate Innovation",
        "ESG & Sustainability",
    ],
    "Education & Learning": [
        "Educational Innovation",
        "EdTech",
        "Teaching & Learning",
        "E-Learning",
        "Online Education",
        "STEM Education",
        "Digital Education",
        "Curriculum Design",
        "Instructional Design",
        "Teaching Methodology",
        "Educational Content",
        "Learning Resources",
        "Educational Games",
        "Gamification",
        "Inclusive Education",
        "Accessibility in Education",
        "Teacher Development",
        "Faculty Development",
        "Student Development",
        "Academic Competitions",
        "Quizzes",
        "Olympiads",
        "Aptitude & Reasoning",
        "Learning Analytics",
        "Personalized Learning",
        "Language Learning",
        "Literacy",
        "Skill Development",
        "Vocational Education",
        "Career Education",
        "Education Policy",
        "School Innovation",
        "College Innovation",
        "Educational Research",
        "Teaching Fellowships",
        "Education Scholarships",
    ],
    "Social Impact & Leadership": [
        "Social Impact",
        "Social Innovation",
        "Community Development",
        "Community Service",
        "Volunteering",
        "Leadership",
        "Youth Leadership",
        "Student Leadership",
        "Civic Engagement",
        "Social Entrepreneurship",
        "Rural Development",
        "Urban Development",
        "Poverty & Livelihoods",
        "Women Empowerment",
        "Child Welfare",
        "Disability Inclusion",
        "Accessibility",
        "Gender Equality",
        "Human Rights",
        "Public Awareness",
        "Community Health",
        "Digital Inclusion",
        "Financial Inclusion",
        "Public Policy",
        "Governance",
        "Civic Innovation",
        "Legal Awareness",
        "Humanitarian Challenges",
        "NGO & Non-Profit",
        "Community Problem Solving",
        "Social Campaigns",
        "Youth Development",
        "Peace & Conflict",
        "Diplomacy",
        "Model United Nations",
        "Public Administration",
        "Community Innovation",
    ],
    "Open & Multidisciplinary": [
        "Open Innovation",
        "Open Challenges",
        "Open Hackathons",
        "Multidisciplinary Challenges",
        "Interdisciplinary Challenges",
        "Grand Challenges",
        "Idea Challenges",
        "Problem-Solving Challenges",
        "Innovation Challenges",
        "Industry Challenges",
        "Government Challenges",
        "National Challenges",
        "Global Challenges",
        "Cross-Domain Challenges",
        "Inter-College Competitions",
        "University-wide Competitions",
        "Any-Domain Competitions",
        "Open Call for Ideas",
        "Open Call for Solutions",
        "Design + Technology",
        "Science + Engineering",
        "Business + Technology",
        "AI + Healthcare",
        "AI + Education",
        "AI + Sustainability",
        "Engineering + Sustainability",
        "Business + Social Impact",
        "Technology + Social Impact",
        "Science + Healthcare",
        "Other Multidisciplinary Challenges",
    ],
}

# Flat lookup: subcategory → categories containing it (a few values are
# intentionally reachable from more than one category, e.g. Social
# Entrepreneurship; validation is category-scoped, this is for reporting).
_SUB_TO_CATEGORIES: Dict[str, List[str]] = {}
for _cat, _subs in SUBCATEGORIES_BY_CATEGORY.items():
    for _sub in _subs:
        _SUB_TO_CATEGORIES.setdefault(_sub, []).append(_cat)

# ─────────────────────────────────────────────────────────────────────────
# Legacy / hallucinated values → canonical mappings
# ─────────────────────────────────────────────────────────────────────────

# The server's OLD hardcoded subcategory lists (pre-taxonomy doc) were coarse
# compounds. Every one maps to a specific canonical value (or to None when the
# invention spans several canonical values — then it's flagged, never guessed).
SUBCATEGORY_ALIASES: Dict[str, Optional[str]] = {
    # Old server lists (observed stored in production)
    "Robotics & Autonomous Systems": None,  # spans Robotics / Autonomous Vehicles / Rovers & Space Robotics
    "Aerospace, Drones & Space": None,      # spans Aerospace Engineering / Drones & UAVs / Space Technology
    "Hardware, Embedded & IoT": None,       # spans Embedded Systems / Hardware Design / Internet of Things
    "Automotive, EV & Formula": None,       # spans Automotive Engineering / Electric Vehicles / Formula & Racing
    "Manufacturing, 3D Printing & CAD": None,
    "Illustration & Visual Art": None,      # spans Illustration / Digital Art / Fine Arts
    "Physical & Space Sciences": None,      # spans Physics / Astronomy / Space Science
    "Software Development": "Software Development",
    "AI & Machine Learning": None,          # spans Artificial Intelligence / Machine Learning
    "Fiction & Creative Writing": None,     # spans Fiction / Creative Writing
    "Film & Video": None,                   # spans Film Making / Videography
    "Music & Audio": None,                  # not in the doc as a pair; see AUDIO aliases
    "Journalism & Nonfiction": None,        # spans Journalism / several non-fiction subs
    "Teaching & Curriculum": None,          # spans Teaching & Learning / Curriculum Design
    "Spelling & Vocabulary": None,
    "Entrepreneurship & Startups": None,    # spans Entrepreneurship / Startups
    "Conservation & Ecology": None,         # spans Wildlife Conservation / Biodiversity / Environmental Protection
    "Climate & Clean Energy": None,         # spans Climate Action / Clean Energy
    "Sustainable Food & Agriculture": None,  # spans Sustainable Agriculture / Nutrition & Food Science
    "Community & Equity": None,             # spans Community Development / Gender Equality / Human Rights
    "Graphic Design": "Graphic Design",
    "Fashion Design": "Fashion Design",
    "Architecture & Urban Design": None,    # spans Architecture / Urban Development is in Social Impact
    # Spec item 12's observed hallucinations
    "Engineering Design (generic misuse)": None,  # the value itself IS canonical; misuse is a specificity issue
    # Scraper phrasings
    "robotics": "Robotics",
    "AI": "Artificial Intelligence",
    "A.I.": "Artificial Intelligence",
    "ML": "Machine Learning",
    "photography": "Photography",
    "web dev": "Web Development",
    "webdev": "Web Development",
    "blockchain": "Blockchain & Web3",
    "IoT": "Internet of Things",
    "cyber security": "Cybersecurity",
    "UI/UX": "UI/UX Design",
    "UX Design": "UI/UX Design",
    "game dev": "Game Design",
    "data science & analytics": "Data Science",
}

# Case-insensitive alias lookup is provided by subcategory_alias().

# The old coarse canonical categories (v1/v2 server lists) also exist as
# legacy spellings on documents. They are NOT in the client doc, but map
# cleanly for convergence:
CATEGORY_ALIASES: Dict[str, str] = {
    # pre-taxonomy server lists
    "Creative Arts": "Creative Arts & Design",
    "Technology & AI": "AI & Technology",
    "Business & Innovation": "Business & Entrepreneurship",
    "Open / Multidisciplinary": "Open & Multidisciplinary",
    "Food & Cooking": "Open & Multidisciplinary",  # retired; food goes by activity
}

# filterKeys.domain derivation (kebab-case) — mirrors the structuring prompt.
DOMAIN_BY_CATEGORY: Dict[str, str] = {
    "AI & Technology": "ai-technology",
    "Engineering & Innovation": "engineering-innovation",
    "Business & Entrepreneurship": "business-entrepreneurship",
    "Science & Research": "science-research",
    "Creative Arts & Design": "creative-arts-design",
    "Writing & Media": "writing-media",
    "Environment & Sustainability": "environment-sustainability",
    "Education & Learning": "education-learning",
    "Social Impact & Leadership": "social-impact-leadership",
    "Open & Multidisciplinary": "open-multidisciplinary",
}


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────

def is_canonical_category(value: Any) -> bool:
    return isinstance(value, str) and value in CANONICAL_CATEGORIES


def canonical_category(value: Any) -> Optional[str]:
    """Resolve a category spelling to canonical, or None when unknown."""
    if not isinstance(value, str):
        return None
    if value in CANONICAL_CATEGORIES:
        return value
    return CATEGORY_ALIASES.get(value.strip())


def subcategories_for(category: Any) -> List[str]:
    """Verbatim subcategory list for a canonical category."""
    return SUBCATEGORIES_BY_CATEGORY.get(category, [])


def is_canonical_subcategory(category: Any, subcategory: Any) -> bool:
    if not isinstance(subcategory, str):
        return False
    return subcategory in subcategories_for(category)


def subcategory_alias(subcategory: Any) -> Optional[str]:
    """Map a legacy/hallucinated subcategory to canonical, None when unmappable.

    Returns:
        - The subcategory itself when already canonical (case-sensitive)
        - The canonical value for an unambiguous alias
        - None when the value is unknown or ambiguous (caller must flag,
          never guess)
    """
    if not isinstance(subcategory, str) or not subcategory.strip():
        return None
    raw = subcategory.strip()
    # Exact canonical (any category — caller still needs category-scoping)
    for subs in SUBCATEGORIES_BY_CATEGORY.values():
        if raw in subs:
            return raw
    mapped = SUBCATEGORY_ALIASES.get(raw)
    if mapped is not None:
        return mapped
    # Case-insensitive canonical fallback
    lowered = raw.lower()
    for subs in SUBCATEGORIES_BY_CATEGORY.values():
        for candidate in subs:
            if candidate.lower() == lowered:
                return candidate
    lowered_alias = {k.lower(): v for k, v in SUBCATEGORY_ALIASES.items()}
    mapped = lowered_alias.get(lowered)
    return mapped if mapped is not None else None


def resolve_subcategory(
    category: Any, subcategory: Any
) -> Tuple[Optional[str], str]:
    """Category-scoped subcategory resolution.

    Returns:
        (canonical_subcategory_or_None, status) where status is one of:
        "canonical"        — exact member of the category's list
        "alias"            — unambiguous legacy/scraper mapping
        "ambiguous"        — known invention spanning several canonical values
        "case_normalized"  — case-only difference from a canonical value
        "unknown"          — not in the taxonomy at all
    """
    if subcategory is None:
        return None, "canonical"  # null is always allowed
    if not isinstance(subcategory, str):
        return None, "unknown"
    raw = subcategory.strip()
    if is_canonical_subcategory(category, raw):
        return raw, "canonical"
    # Case-only difference from a canonical value in THIS category
    lowered = raw.lower()
    for candidate in subcategories_for(category):
        if candidate.lower() == lowered:
            return candidate, "case_normalized"
    alias = subcategory_alias(raw)
    if alias is not None:
        if is_canonical_subcategory(category, alias):
            return alias, "alias"
        # The alias resolves to a canonical value of a DIFFERENT category —
        # treat as unknown for this record's category.
        return None, "unknown"
    if raw in SUBCATEGORY_ALIASES and SUBCATEGORY_ALIASES[raw] is None:
        return None, "ambiguous"
    # case-insensitive alias hit
    lowered_alias = {k.lower(): v for k, v in SUBCATEGORY_ALIASES.items()}
    if lowered in lowered_alias:
        mapped = lowered_alias[lowered]
        if mapped is None:
            return None, "ambiguous"
        if is_canonical_subcategory(category, mapped):
            return mapped, "alias"
    return None, "unknown"


def domain_for_category(category: Any) -> Optional[str]:
    return DOMAIN_BY_CATEGORY.get(category)


def taxonomy_summary() -> Dict[str, Any]:
    """Compact summary for tool responses."""
    return {
        "category_count": len(CANONICAL_CATEGORIES),
        "subcategory_total": sum(len(s) for s in SUBCATEGORIES_BY_CATEGORY.values()),
        "subcategories_per_category": {
            cat: len(subs) for cat, subs in SUBCATEGORIES_BY_CATEGORY.items()
        },
    }
