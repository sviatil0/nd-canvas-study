"""Map paradigms slide PDFs (downloaded as Drive presentation exports) → topic.

Slides on prof's site are linked by L<n>: <title>. We map by content keywords
since filenames are doc IDs.
"""
from __future__ import annotations

import re
from pathlib import Path

# Mapping by simple keyword detection in slide content.
# Updated as we OCR slides — for now use file-by-file scoring against analyze.TOPICS.

LECTURE_TO_TOPIC: dict[int, str] = {
    1: "course_overview",
    2: "javascript_basics",
    3: "javascript_basics",
    4: "javascript_closures",
    5: "javascript_closures",
    6: "javascript_objects",
    7: "javascript_objects",
    8: "javascript_async",
    9: "frontend_dom",
    10: "frontend_mvc",
    11: "exam1_review",
    12: "python_basics",
    13: "python_basics",
    14: "django_intro",
    15: "django_intro",
    16: "django_models",
    17: "django_views_templates",
    18: "django_forms_auth",
    19: "rest_apis",
    20: "exam2_review",
    21: "java_basics",
    22: "java_oop",
    23: "java_collections",
    24: "java_concurrency",
    25: "clojure_intro",
    26: "clojure_immutability",
    27: "functional_higher_order",
    28: "paradigms_comparison",
}


def lecture_num_from_text(text: str) -> int | None:
    """Look at first 2KB for 'L<n>:' pattern."""
    m = re.search(r"\bL\s*(\d{1,2})\s*[:.]", text[:2000])
    if m:
        return int(m.group(1))
    return None
