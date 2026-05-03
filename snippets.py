"""Render a PDF page to PNG (cached). Used by Django to embed page snippets."""
from __future__ import annotations

from pathlib import Path

from pdf2image import convert_from_path

CACHE_DIRNAME = "_snippets"
DPI = 130


def render_page(pdf_path: Path, page: int, course_dir: Path) -> Path:
    """Return path to PNG of the given 1-indexed page; cache under course_dir/_snippets/."""
    cache = course_dir / CACHE_DIRNAME
    cache.mkdir(exist_ok=True)
    rel = pdf_path.relative_to(course_dir)
    safe_rel = str(rel).replace("/", "__").replace(".pdf", "")
    out = cache / f"{safe_rel}__p{page}.png"
    if out.exists():
        return out
    images = convert_from_path(str(pdf_path), dpi=DPI, first_page=page, last_page=page)
    if not images:
        raise FileNotFoundError(f"page {page} not found in {pdf_path}")
    images[0].save(out, "PNG")
    return out
