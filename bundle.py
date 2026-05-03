"""Convert downloaded course material into LLM-injectable text bundles.

Categorizes files by name into: lectures, homeworks, hw_keys, in_class, exams,
exam_solutions, practice, tables, other. Writes per-category bundle and a
master MASTER.md with all extracted text.

Usage:
    python bundle.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pypdf import PdfReader

CATEGORY_PATTERNS = [
    ("hw_keys",        re.compile(r"hw[\w-]*[_ -]*(key|solution)", re.I)),
    ("homeworks",      re.compile(r"hw[\w-]*|homework", re.I)),
    ("exam_solutions", re.compile(r"exam[\w -]*(solution|key)|e\d+[\w -]*solution", re.I)),
    ("practice",       re.compile(r"practice|extra.*problem|review", re.I)),
    ("exams",          re.compile(r"\bexam\b|e\d+_|final", re.I)),
    ("in_class",       re.compile(r"inclass|in[-_ ]class", re.I)),
    ("tables",         re.compile(r"z-?table|t-?table|f-?table|chisq|studentized", re.I)),
    ("lectures",       re.compile(r"lecture|chapter|ch\d+|notes|slides", re.I)),
]


def categorize(name: str) -> str:
    for cat, pat in CATEGORY_PATTERNS:
        if pat.search(name):
            return cat
    return "other"


def extract_pdf(path: Path, course_dir: Path | None = None) -> str:
    # Prefer OCR cache when available (much cleaner for scanned solutions)
    if course_dir is not None:
        rel = str(path.relative_to(course_dir))
        ocr_file = course_dir / "_ocr" / (rel.replace("/", "__") + ".txt")
        if ocr_file.exists():
            return ocr_file.read_text()
    try:
        reader = PdfReader(str(path))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            parts.append(f"\n--- page {i+1} ---\n{t}")
        return "".join(parts).strip()
    except Exception as e:
        return f"[pdf extract failed: {e}]"


def walk_pdfs(course_dir: Path):
    for p in course_dir.rglob("*.pdf"):
        yield p


def build(course_dir: Path) -> None:
    out_dir = course_dir / "bundles"
    out_dir.mkdir(exist_ok=True)
    by_cat: dict[str, list[tuple[Path, str]]] = {}
    manifest: list[dict] = []

    for pdf in sorted(walk_pdfs(course_dir)):
        rel = pdf.relative_to(course_dir)
        cat = categorize(pdf.name)
        text = extract_pdf(pdf, course_dir)
        manifest.append({
            "path": str(rel),
            "category": cat,
            "size": pdf.stat().st_size,
            "chars": len(text),
        })
        by_cat.setdefault(cat, []).append((rel, text))
        print(f"  [{cat:15}] {rel}")

    for cat, items in by_cat.items():
        f = out_dir / f"{cat}.md"
        with f.open("w") as fh:
            fh.write(f"# {cat}\n\n")
            for rel, text in items:
                fh.write(f"\n\n## SOURCE: {rel}\n\n{text}\n")
        print(f"wrote {f} ({len(items)} files)")

    master = out_dir / "MASTER.md"
    with master.open("w") as fh:
        fh.write(f"# Course bundle: {course_dir.name}\n\n")
        for cat in by_cat:
            fh.write(f"- [{cat}](./{cat}.md): {len(by_cat[cat])} docs\n")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote bundles → {out_dir}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    course_dir = Path(args.course_dir)
    if not course_dir.is_dir():
        print(f"Not a directory: {course_dir}")
        return 1
    build(course_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
