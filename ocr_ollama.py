"""High-quality local OCR via Ollama (Qwen2.5-VL or similar vision model).

Free, no rate limits, runs on Apple Silicon. Transcribes statistics PDFs
preserving math as LaTeX and problem numbering.

Usage:
    ollama pull qwen2.5vl:7b
    python ocr_ollama.py --course-dir downloads/128781_statistics
    python ocr_ollama.py --course-dir <dir> --workers 2 --model qwen2.5vl:7b
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pdf2image import convert_from_path

OLLAMA = shutil.which("ollama") or "ollama"
ALL_PROBLEM_CATEGORIES = {
    "exams", "exam_solutions", "practice",
    "homeworks", "hw_keys", "in_class",
}
PAGE_SEP = "\n\n--- page {n} ---\n\n"
DPI = 200
TIMEOUT = 240

PROMPT = (
    "Transcribe this page of a college statistics document EXACTLY as it appears. "
    "Rules: preserve numbered problems (1., 2., (a), (b), etc.) on their own lines; "
    "convert math to LaTeX inline $...$ or block $$...$$; preserve tables as Markdown; "
    "keep ANOVA, regression, and statistical tables intact; for handwritten work transcribe "
    "what is written and mark unclear chars as [?]; do NOT add commentary, headings, or "
    "summaries — output only the transcribed page text."
)


def render_pages(pdf: Path, work: Path) -> list[Path]:
    work.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    images = convert_from_path(str(pdf), dpi=DPI)
    for i, img in enumerate(images):
        p = work / f"p{i+1:03}.png"
        if not p.exists():
            img.save(p, "PNG")
        out.append(p)
    return out


def transcribe_page(model: str, png: Path) -> str:
    proc = subprocess.run(
        [OLLAMA, "run", model, f"{PROMPT} {png}"],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if proc.returncode != 0:
        return f"[ollama error: {proc.stderr[:200]}]"
    return proc.stdout.strip()


def process(course_dir: Path, model: str, limit: int | None, force: bool,
            categories: set[str] | None, match: str | None, workers: int) -> None:
    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first.")
    manifest = json.loads(manifest_file.read_text())
    cache = course_dir / "_ocr"
    cache.mkdir(exist_ok=True)
    done_marker = cache / "_ollama_done"
    done_set = set(done_marker.read_text().splitlines()) if done_marker.exists() else set()

    cats = categories or ALL_PROBLEM_CATEGORIES
    targets = [r for r in manifest if r["category"] in cats]
    if match:
        targets = [r for r in targets if match.lower() in r["path"].lower()]
    if limit:
        targets = targets[:limit]

    work_items: list[tuple[str, Path, int]] = []
    pdf_pages: dict[str, list[Path]] = {}
    for row in targets:
        rel = row["path"]
        if rel in done_set and not force:
            print(f"  cached {rel}")
            continue
        pdf = course_dir / rel
        if not pdf.exists():
            continue
        work = cache / "_pages" / rel.replace("/", "__")
        try:
            pages = render_pages(pdf, work)
        except Exception as e:
            print(f"  render fail {rel}: {e}")
            continue
        pdf_pages[rel] = pages
        for i, png in enumerate(pages, 1):
            work_items.append((rel, png, i))

    print(f"\nTranscribing {len(work_items)} pages from {len(pdf_pages)} PDFs "
          f"with {workers} workers using {model}...")

    page_text: dict[tuple[str, int], str] = {}
    t_start = time.time()

    def task(item):
        rel, png, page_num = item
        t0 = time.time()
        text = transcribe_page(model, png)
        return rel, page_num, text, time.time() - t0

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, it) for it in work_items]
        for fut in as_completed(futures):
            rel, page_num, text, dt = fut.result()
            completed += 1
            page_text[(rel, page_num)] = text
            avg = (time.time() - t_start) / completed
            eta = avg * (len(work_items) - completed)
            print(f"  [{completed}/{len(work_items)}] {rel} p{page_num} "
                  f"({len(text)}c, {dt:.1f}s) — ETA {eta/60:.1f}m")

    for rel, pages in pdf_pages.items():
        chunks = []
        for i in range(1, len(pages) + 1):
            chunks.append(PAGE_SEP.format(n=i) + page_text.get((rel, i), ""))
        out_file = cache / (rel.replace("/", "__") + ".txt")
        out_file.write_text("".join(chunks))
        done_set.add(rel)
    done_marker.write_text("\n".join(sorted(done_set)))
    print(f"\nDone. {len(work_items)} pages in {(time.time()-t_start)/60:.1f}m.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--categories")
    ap.add_argument("--match")
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel ollama calls (1 model, 2 reqs ~= 2x throughput on M-series)")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    cats = set(args.categories.split(",")) if args.categories else None
    process(cdir, args.model, args.limit, args.force, cats, args.match, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
