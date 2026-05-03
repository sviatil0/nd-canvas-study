"""Extract individual exam/practice problems and link to topics + source material.

Walks the exam-side PDFs, splits into numbered problem stems, classifies each by
topic via the same dictionary used in analyze.py, and emits a study plan that
points back at the homework / in-class PDF that covers that topic.

Usage:
    python problems.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

from analyze import TOPICS

PROBLEM_HEAD = re.compile(r"^\s*(?:problem|question|q|#)?\s*(\d{1,2})[.\):]\s+", re.I | re.M)
SUBPART = re.compile(r"^\s*\([a-h]\)\s+", re.I | re.M)
EXAM_CATEGORIES = {"exams", "exam_solutions", "practice"}
PREP_CATEGORIES = {"homeworks", "hw_keys", "in_class"}

LOGISTICS_FILE_HINTS = ("details", "logistics", "info-", "syllab", "schedule")
LOGISTICS_PHRASES = (
    "dear ", "syllabus", "office hours", "academic integrity",
    "make-up", "honor code", "policy", "report to ", "you must submit",
    "please arrive", "no calculator", "bring your", "exam location",
)
PROBLEM_SIGNALS = (
    "?", "=", "compute", "calculate", "find", "test", "determine",
    "estimate", "construct", "what is", "p-value", "interval",
    " a.", " b.", " c.", " d.",
)


def is_logistics_file(rel_path: str) -> bool:
    name = rel_path.lower()
    return any(h in name for h in LOGISTICS_FILE_HINTS)


def looks_like_problem(stem: str) -> bool:
    s = stem.lower()
    if any(phrase in s for phrase in LOGISTICS_PHRASES):
        return False
    if not any(sig in s for sig in PROBLEM_SIGNALS):
        return False
    return True


def topic_hits(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for topic, aliases in TOPICS.items():
        n = sum(len(re.findall(p, text, re.I)) for p in aliases)
        if n:
            out[topic] = n
    return out


def split_problems(text: str) -> list[tuple[int, str]]:
    """Split a PDF text dump into (problem_num, body) tuples."""
    matches = list(PROBLEM_HEAD.finditer(text))
    if not matches:
        return [(1, text)]
    problems = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if 30 < len(body) < 6000:  # filter junk / headers
            problems.append((num, body))
    return problems


def extract_pdf_pages(path: Path, course_dir: Path | None = None) -> list[str]:
    if course_dir is not None:
        rel = str(path.relative_to(course_dir))
        ocr_file = course_dir / "_ocr" / (rel.replace("/", "__") + ".txt")
        if ocr_file.exists():
            text = ocr_file.read_text()
            parts = re.split(r"\n\n--- page \d+ ---\n\n", text)
            return [p for p in parts if p.strip()]
    try:
        return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
    except Exception:
        return []


def classify_problem(body: str) -> str | None:
    hits = topic_hits(body)
    if not hits:
        return None
    return max(hits, key=hits.get)


def collect_pdfs(course_dir: Path) -> dict[str, list[Path]]:
    """Group PDFs by category using the manifest written by bundle.py."""
    manifest = course_dir / "bundles" / "manifest.json"
    if not manifest.exists():
        raise SystemExit("Run bundle.py first.")
    rows = json.loads(manifest.read_text())
    grouped: dict[str, list[Path]] = defaultdict(list)
    for r in rows:
        grouped[r["category"]].append(course_dir / r["path"])
    return grouped


SOURCE_WEIGHTS = {
    "practice": 5,
    "exam_solutions": 4,
    "exams": 4,
    "hw_keys": 2,
    "homeworks": 2,
    "in_class": 1,
    "other": 0,
}


def build_study_plan(course_dir: Path) -> None:
    grouped = collect_pdfs(course_dir)
    prep_pdfs = [p for cat in PREP_CATEGORIES for p in grouped.get(cat, [])]

    prep_index: dict[str, list[str]] = defaultdict(list)
    for p in prep_pdfs:
        text = "\n".join(extract_pdf_pages(p, course_dir))
        for topic in topic_hits(text):
            prep_index[topic].append(str(p.relative_to(course_dir)))

    # Walk EVERY problem-bearing PDF, tag with source category
    by_topic: dict[str, list[dict]] = defaultdict(list)
    all_categories = EXAM_CATEGORIES | PREP_CATEGORIES
    for cat in all_categories:
        for p in grouped.get(cat, []):
            rel = str(p.relative_to(course_dir))
            if is_logistics_file(rel):
                continue
            pages = extract_pdf_pages(p, course_dir)
            joined = ""
            page_breaks = []
            for i, pg in enumerate(pages):
                page_breaks.append((len(joined), i + 1))
                joined += "\n" + pg + "\n"
            for num, body in split_problems(joined):
                topic = classify_problem(body)
                if not topic:
                    continue
                stem = re.sub(r"\s+", " ", body[:320]).strip()
                if not looks_like_problem(stem):
                    continue
                offset = joined.find(body)
                page = next(
                    (pn for off, pn in reversed(page_breaks) if off <= offset),
                    1,
                )
                by_topic[topic].append({
                    "source": rel,
                    "category": cat,
                    "source_weight": SOURCE_WEIGHTS.get(cat, 0),
                    "page": page,
                    "problem": num,
                    "stem": stem,
                })

    # Sort each topic's problems by source weight (likelihood proxy)
    for t in by_topic:
        by_topic[t].sort(key=lambda r: (-r["source_weight"], r["source"], r["page"], r["problem"]))

    # Sort topics by exam problem count desc
    ranked = sorted(by_topic.items(), key=lambda kv: -len(kv[1]))

    out = course_dir / "bundles" / "STUDY_PLAN.md"
    with out.open("w") as fh:
        fh.write(f"# Study plan: {course_dir.name}\n\n")
        fh.write(f"prep-side PDFs: {len(prep_pdfs)}\n\n")
        for topic, problems in ranked:
            fh.write(f"\n## {topic} — {len(problems)} problems\n\n")
            review = prep_index.get(topic, [])
            if review:
                fh.write("**Review these prep files:**\n")
                for f in sorted(set(review)):
                    fh.write(f"- `{f}`\n")
            else:
                fh.write("_(no matching prep material — likely under-prepared)_\n")
            fh.write("\n**Problems (sorted by source weight):**\n")
            for q in problems[:50]:
                fh.write(f"- [{q['category']}] `{q['source']}` p{q['page']} #{q['problem']}: {q['stem']}…\n")

    (course_dir / "bundles" / "problems.json").write_text(
        json.dumps({t: ps for t, ps in ranked}, indent=2)
    )
    print(f"Wrote {out} ({len(ranked)} topics, {sum(len(v) for v in by_topic.values())} problems)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1
    build_study_plan(cdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
