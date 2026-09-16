"""Re-extract problem PDFs via Tesseract OCR.

Heuristic: pages whose pypdf text has a low ratio of letters-to-total-chars
(< 0.55) or contains many replacement chars are re-OCR'd. Output cached to
downloads/<course>/_ocr/<rel>.txt as page-separated text.

Usage:
    python ocr.py --course-dir downloads/128781_statistics
    python ocr.py --course-dir <dir> --force      # re-OCR everything
    python ocr.py --course-dir <dir> --all        # include lectures/other, not just problems
    python ocr.py --course-dir <dir> --every-page # OCR every page, ignore the text-quality heuristic
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pdf2image import convert_from_path
from pypdf import PdfReader

import pytesseract

EXAM_CATEGORIES = {"exams", "exam_solutions", "practice"}
ALL_PROBLEM_CATEGORIES = {
    "exams", "exam_solutions", "practice",
    "homeworks", "hw_keys", "in_class",
}
LETTER_RATIO_MIN = 0.55
PAGE_SEP = "\n\n--- page {n} ---\n\n"


def letter_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for c in text if c.isalnum() or c in " .,;:?!()-")
    return letters / max(len(text), 1)


def needs_ocr(text: str) -> bool:
    if not text or len(text.strip()) < 50:
        return True
    if "�" in text:
        return True
    return letter_ratio(text) < LETTER_RATIO_MIN


def ocr_pdf(pdf: Path, dpi: int = 250, lang: str = "eng") -> list[str]:
    pages = convert_from_path(str(pdf), dpi=dpi)
    out = []
    for img in pages:
        text = pytesseract.image_to_string(img, lang=lang)
        out.append(text or "")
    return out


def process(course_dir: Path, force: bool = False, all_categories: bool = False,
            every_page: bool = False) -> None:
    import json
    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first.")
    manifest = json.loads(manifest_file.read_text())
    cache = course_dir / "_ocr"
    cache.mkdir(exist_ok=True)

    for row in manifest:
        if not all_categories and row["category"] not in ALL_PROBLEM_CATEGORIES:
            continue
        rel = row["path"]
        pdf = course_dir / rel
        if not pdf.exists():
            continue
        cache_file = cache / (rel.replace("/", "__") + ".txt")
        if cache_file.exists() and not force:
            print(f"  cached {rel}")
            continue

        try:
            reader = PdfReader(str(pdf))
        except Exception as e:
            print(f"  read fail {rel}: {e}")
            continue

        # Decide per-page: keep pypdf text if quality fine, else OCR
        rendered_pages: list[str] | None = None
        out_pages = []
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if every_page or needs_ocr(t):
                if rendered_pages is None:
                    print(f"  OCR {rel}…")
                    try:
                        rendered_pages = ocr_pdf(pdf)
                    except Exception as e:
                        print(f"    OCR fail: {e}")
                        rendered_pages = ["" for _ in reader.pages]
                t = rendered_pages[i] if i < len(rendered_pages) else t
            out_pages.append(t)

        text = "".join(PAGE_SEP.format(n=i + 1) + t for i, t in enumerate(out_pages))
        cache_file.write_text(text)
        print(f"  wrote {cache_file.relative_to(course_dir)}")


def load_text(course_dir: Path, rel: str) -> str | None:
    cache = course_dir / "_ocr" / (rel.replace("/", "__") + ".txt")
    if cache.exists():
        return cache.read_text()
    return None


def split_to_pages(text: str) -> list[str]:
    parts = re.split(r"\n\n--- page \d+ ---\n\n", text)
    return [p for p in parts if p.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all", dest="all_categories", action="store_true",
                    help="OCR every PDF in the manifest, including lectures and other")
    ap.add_argument("--every-page", dest="every_page", action="store_true",
                    help="OCR every page instead of only pages whose embedded text looks bad "
                         "(slide decks embed jammed text that passes the heuristic)")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    process(cdir, force=args.force, all_categories=args.all_categories,
            every_page=args.every_page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
