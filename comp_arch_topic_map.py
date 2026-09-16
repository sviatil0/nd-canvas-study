"""Map CSE 30321 lecture slide numbers to topic_graph keys.

Slides are named like:
  CSE 30321 SP26 Computer Architecture - 14 - Direct-mapped and Associative Caches (marked).pdf

Lecture-number → topic from topic_graph._COMP_ARCH.
"""
from __future__ import annotations

import re
from pathlib import Path

# Lecture-number → topic key. Multiple lectures may map to same topic.
LECTURE_TO_TOPIC: dict[int, str] = {
    1:  "isa_basics",
    2:  "isa_basics",                  # ISA + Assembly Programming Review
    3:  "performance_metrics",
    4:  "riscv_instructions",
    5:  "riscv_encoding",
    6:  "riscv_control_procedures",
    7:  "pipelining_intro",
    8:  "pipeline_data_hazards",
    9:  "pipeline_data_hazards",       # Pipelining Hazard Wrap-up
    10: "pipeline_data_hazards",       # Exam 1 Review (pipeline-heavy)
    12: "pipeline_control_hazards",
    13: "memory_hierarchy_intro",      # Control Hazards Examples + Memory Hierarchy Intro
    14: "direct_mapped_cache",         # Direct-mapped + Associative Caches
    15: "associative_cache",
    16: "cache_performance",
    17: "virtual_memory_intro",        # Cache Wrap-up + VM Intro
    18: "vm_paging",
    19: "vm_tlb_performance",
    21: "branch_prediction",
    22: "scheduling_intro",            # Exam 2 Review Notes (covers scheduling intro)
    24: "scheduling_intro",
    25: "dynamic_scheduling",
    26: "dynamic_scheduling",
    27: "parallelism_intro",
    28: "cache_coherence",
    29: "cache_coherence",             # Final Exam Review
}


def lecture_num_from_name(name: str) -> int | None:
    """Extract '14' from 'CSE 30321 ... - 14 - Direct-mapped...'."""
    m = re.search(r"\bComputer Architecture\s*-\s*(\d{1,2})\s*-", name, re.I)
    if m:
        return int(m.group(1))
    return None


def topic_for_pdf(pdf_path: Path) -> str | None:
    n = lecture_num_from_name(pdf_path.name)
    if n is None:
        return None
    return LECTURE_TO_TOPIC.get(n)


def map_pdfs_to_topics(course_dir: Path) -> dict[str, list[str]]:
    """Walk course_dir, return {topic: [relative_pdf_paths]}."""
    out: dict[str, list[str]] = {}
    for pdf in (course_dir / "_external/google_drive_local/slides").glob("*.pdf"):
        topic = topic_for_pdf(pdf)
        if topic:
            out.setdefault(topic, []).append(str(pdf.relative_to(course_dir)))
    return out
