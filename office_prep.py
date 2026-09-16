"""Render Office documents in a course folder into PDF + plain text.

Canvas serves .docx/.pptx straight from Files, and the rest of the pipeline
only understands PDFs and .txt. This converts each Office file once, in place,
so bundle.py categorizes it, ocr_claude.py can transcribe it, and vectorize.py
indexes it.

Usage:
    python office_prep.py --course-dir downloads/139705_fa26-cse-40113-01-algorithms
    python office_prep.py --course-dir <dir> --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from email_ingest import OFFICE_EXT, docx_text, office_to_pdf

SKIP_DIRS = {"_ocr", "_pages", "_shards", "_snippets", "_attempts", "chroma",
             "bundles", "_email"}  # _email is already converted at ingest time


def prep(course_dir: Path, force: bool) -> dict:
    txt_dir = course_dir / "_office_text"
    txt_dir.mkdir(exist_ok=True)
    converted, extracted = 0, 0

    for src in sorted(course_dir.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in OFFICE_EXT:
            continue
        if any(part in SKIP_DIRS for part in src.parts):
            continue
        pdf = src.with_suffix(".pdf")
        if force or not pdf.exists():
            out = office_to_pdf(src, src.parent)
            if out:
                converted += 1
                print(f"  pdf   {out.relative_to(course_dir)}")
            else:
                print(f"  FAIL  {src.relative_to(course_dir)}")
        if src.suffix.lower() in {".docx", ".doc"}:
            t = txt_dir / (src.stem + ".txt")
            if force or not t.exists():
                t.write_text(docx_text(src))
                extracted += 1
                print(f"  text  {t.relative_to(course_dir)}")

    print(f"\n{converted} PDFs rendered, {extracted} text extractions → {course_dir}")
    return {"pdfs": converted, "texts": extracted}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    prep(cdir, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
