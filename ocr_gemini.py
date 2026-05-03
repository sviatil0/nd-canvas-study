"""High-quality OCR via Gemini 2.0 Flash (free tier).

Free-tier limits (as of Jan 2026): 15 RPM, 1M TPM, 1500 RPD.
This script paces requests to stay under 15 RPM with up to N parallel
workers (default 4) using a token-bucket rate limiter.

Usage:
    export GEMINI_API_KEY=...
    python ocr_gemini.py --course-dir downloads/128781_statistics
    python ocr_gemini.py --course-dir <dir> --limit 5    # test
    python ocr_gemini.py --course-dir <dir> --workers 4  # parallel calls
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pdf2image import convert_from_path

ALL_PROBLEM_CATEGORIES = {
    "exams", "exam_solutions", "practice",
    "homeworks", "hw_keys", "in_class",
}
PAGE_SEP = "\n\n--- page {n} ---\n\n"
DPI = 200
RPM_LIMIT = 15  # Gemini 2.0 Flash free tier
MIN_INTERVAL = 60.0 / RPM_LIMIT  # seconds between requests

PROMPT = """Transcribe this page of a college statistics document EXACTLY as it appears.

Rules:
- Preserve numbered problems (1., 2., (a), (b), etc.) on their own lines.
- Convert math to LaTeX: inline $...$, display $$...$$.
- Preserve tables as Markdown tables.
- Keep ANOVA tables, regression output, and statistical tables intact.
- For handwritten work, transcribe what is written; mark unclear chars as [?].
- Do NOT add commentary, headings, or summaries — output only the transcribed page text.
"""


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            sleep = self.next_allowed - now
            if sleep > 0:
                time.sleep(sleep)
                now = time.time()
            self.next_allowed = max(self.next_allowed, now) + self.min_interval


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


def transcribe_page(model, png: Path, limiter: RateLimiter, retries: int = 2) -> str:
    import google.generativeai as genai  # noqa
    from PIL import Image
    img = Image.open(png)
    last_err = ""
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            resp = model.generate_content([PROMPT, img])
            return resp.text or ""
        except Exception as e:
            last_err = str(e)
            # Backoff for 429
            if "429" in last_err or "quota" in last_err.lower():
                time.sleep(min(60, 2 ** attempt * 5))
            else:
                time.sleep(2)
    return f"[gemini error after {retries+1} tries: {last_err[:200]}]"


def process(course_dir: Path, limit: int | None, force: bool,
            categories: set[str] | None = None, match: str | None = None,
            workers: int = 4) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Set GEMINI_API_KEY (get one at https://aistudio.google.com/apikey).")
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first.")
    manifest = json.loads(manifest_file.read_text())
    cache = course_dir / "_ocr"
    cache.mkdir(exist_ok=True)
    done_marker = cache / "_gemini_done"
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
          f"with {workers} workers (≤{RPM_LIMIT} req/min)...")
    if len(work_items) > 1500:
        print(f"WARNING: free tier daily cap is 1500 RPD, you have {len(work_items)} pages.")

    limiter = RateLimiter(MIN_INTERVAL)
    page_text: dict[tuple[str, int], str] = {}
    t_start = time.time()

    def task(item):
        rel, png, page_num = item
        t0 = time.time()
        text = transcribe_page(model, png, limiter)
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

    # Stitch back per PDF
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
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--categories")
    ap.add_argument("--match")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    cats = set(args.categories.split(",")) if args.categories else None
    process(cdir, args.limit, args.force, cats, args.match, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
