"""Extract critical class policies/dates/logistics from Canvas content via Gemini.

Walks pages + announcements + assignment descriptions for syllabus-like text,
then asks Gemini to summarize into structured Markdown. Cached in
bundles/CLASS_INFO.md so the UI loads it instantly.

Usage:
    python class_info.py --course-dir downloads/128781_statistics
    python class_info.py --course-dir <dir> --rebuild
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from gemini_client import generate

PROMPT = """You are reading raw HTML content from a college course's Canvas pages,
announcements, and assignment descriptions. Extract everything a student must
know about the **course itself** (not subject-matter content).

Output Markdown with EXACTLY these sections (omit a section only if absolutely
no info exists for it):

## Exam logistics
- Dates, times, locations, room assignments by name letter, room split rules.
- What's allowed: calculator, formula sheet, scratch paper, etc.
- Exam structure (# questions, MCQ vs free-response, time limit, point breakdown).

## Grading
- Weights of HW, exams, quizzes, attendance.
- Drop policies (lowest HW dropped, etc.).
- Grade scale.

## Attendance / Participation
- Required? penalties? makeup?

## Policies
- Late HW, makeup exams, regrade requests, academic integrity, AI usage.
- Office hours, contact, communication channels.

## Important dates
- Bullet list of every concrete date mentioned (HW due, exam, withdrawal, etc.).
- Format: `YYYY-MM-DD: <event>` (or `MM/DD: <event>` if year unclear).

## Watch out for
- Anything unusual the student should be wary of (e.g., "no makeup", "wrong room
  forfeits exam", "late = 0", "must email 24 hours before").

Use $...$ for any math. Be concise and direct. If a section is empty say
"_no info found_".

--- COURSE CONTENT ---
{content}
--- END ---
"""

MAX_INPUT_CHARS = 80000


def collect_text(course_dir: Path) -> str:
    chunks: list[str] = []

    def add(label: str, text: str) -> None:
        text = re.sub(r"<[^>]+>", " ", text or "")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            chunks.append(f"=== {label} ===\n{text}")

    # Announcements
    ann_dir = course_dir / "announcements"
    if ann_dir.exists():
        for f in sorted(ann_dir.glob("*.html")):
            add(f"announcement: {f.stem}", f.read_text(errors="ignore"))

    # Pages whose names look syllabus-y
    for f in (course_dir / "pages").rglob("*.html") if (course_dir / "pages").exists() else []:
        name = f.stem.lower()
        if any(k in name for k in ("syllab", "policy", "polic", "info", "schedule",
                                   "grading", "calendar", "welcome", "overview", "honor")):
            add(f"page: {f.stem}", f.read_text(errors="ignore"))

    # Module pages too
    for f in (course_dir / "modules").rglob("pages/*.html") if (course_dir / "modules").exists() else []:
        name = f.stem.lower()
        if any(k in name for k in ("syllab", "policy", "info", "details", "calendar",
                                   "welcome", "overview", "honor", "grading")):
            add(f"module page: {f.parent.parent.name}/{f.stem}",
                f.read_text(errors="ignore"))

    # Assignment descriptions in modules
    for f in (course_dir / "modules").rglob("assignments/*.html") if (course_dir / "modules").exists() else []:
        add(f"assignment: {f.stem}", f.read_text(errors="ignore"))

    # All "details" PDFs aren't useful directly here (binary), but their OCR text is
    ocr_dir = course_dir / "_ocr"
    if ocr_dir.exists():
        for f in ocr_dir.glob("*details*.txt"):
            add(f"ocr: {f.stem}", f.read_text(errors="ignore"))

    text = "\n\n".join(chunks)
    return text[:MAX_INPUT_CHARS]


def build(course_dir: Path, rebuild: bool = False) -> Path:
    out = course_dir / "bundles" / "CLASS_INFO.md"
    if out.exists() and not rebuild:
        print(f"  cached {out}")
        return out
    print(f"  collecting text...")
    content = collect_text(course_dir)
    if not content.strip():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("_no Canvas content found yet — run sync first._")
        return out
    print(f"  asking Gemini ({len(content)} chars)...")
    md = generate(PROMPT.format(content=content), max_output_tokens=6144)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"  wrote {out} ({len(md)} chars)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    build(cdir, rebuild=args.rebuild)
    return 0


if __name__ == "__main__":
    sys.exit(main())
