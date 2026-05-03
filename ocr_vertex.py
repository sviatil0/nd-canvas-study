"""High-quality OCR via Vertex AI Gemini 2.5 Pro using ADC.

Auth setup (one-time):
    brew install --cask google-cloud-sdk
    gcloud auth application-default login
    gcloud config set project YOUR_PROJECT_ID
    gcloud auth application-default set-quota-project YOUR_PROJECT_ID

Usage:
    python ocr_vertex.py --course-dir downloads/128781_statistics --project YOUR_PROJECT
    python ocr_vertex.py --course-dir <dir> --model gemini-2.5-pro --workers 8

Per-page sharding so interrupted runs resume page-by-page.
Cost: ~$0.007/page on Gemini 2.5 Pro.
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
TIMEOUT = 120
DEFAULT_RPM = 60

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
    def __init__(self, rpm: int):
        self.min_interval = 60.0 / max(rpm, 1)
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


def transcribe_page(model, png: Path, limiter: RateLimiter, retries: int = 3) -> str:
    from vertexai.generative_models import Part
    img_bytes = png.read_bytes()
    image_part = Part.from_data(data=img_bytes, mime_type="image/png")
    last_err = ""
    for attempt in range(retries):
        limiter.wait()
        try:
            resp = model.generate_content([image_part, PROMPT])
            text = (resp.text or "").strip()
            if len(text) < 10:
                last_err = "empty response"
                time.sleep(2)
                continue
            return text
        except Exception as e:
            last_err = str(e)
            if "429" in last_err or "quota" in last_err.lower() or "rate" in last_err.lower():
                time.sleep(min(60, 2 ** attempt * 5))
            else:
                time.sleep(3)
    return f"[vertex error after {retries} tries: {last_err[:200]}]"


def shard_path(cache: Path, rel: str, page: int) -> Path:
    return cache / "_shards" / rel.replace("/", "__") / f"p{page:03}.txt"


def stitch_if_complete(cache: Path, rel: str, total_pages: int) -> bool:
    base = cache / "_shards" / rel.replace("/", "__")
    if not base.exists():
        return False
    parts = []
    for i in range(1, total_pages + 1):
        f = base / f"p{i:03}.txt"
        if not f.exists():
            return False
        parts.append(PAGE_SEP.format(n=i) + f.read_text())
    out = cache / (rel.replace("/", "__") + ".txt")
    out.write_text("".join(parts))
    return True


def process(course_dir: Path, project: str, location: str, model_name: str,
            limit: int | None, force: bool, categories: set[str] | None,
            match: str | None, workers: int, rpm: int) -> None:
    import vertexai
    from vertexai.generative_models import GenerativeModel
    vertexai.init(project=project, location=location)
    model = GenerativeModel(model_name)

    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first.")
    manifest = json.loads(manifest_file.read_text())
    cache = course_dir / "_ocr"
    cache.mkdir(exist_ok=True)

    cats = categories or ALL_PROBLEM_CATEGORIES
    targets = [r for r in manifest if r["category"] in cats]
    if match:
        targets = [r for r in targets if match.lower() in r["path"].lower()]
    if limit:
        targets = targets[:limit]

    work_items: list[tuple[str, Path, int, int]] = []
    pdf_pages: dict[str, list[Path]] = {}
    for row in targets:
        rel = row["path"]
        final = cache / (rel.replace("/", "__") + ".txt")
        if final.exists() and not force:
            sample = final.read_text()[:500]
            if all(err not in sample for err in ("[vertex error", "[gemini error", "[claude error")) and len(sample) > 50:
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
            shard = shard_path(cache, rel, i)
            if shard.exists() and not force:
                txt = shard.read_text()
                if len(txt) > 10 and "[vertex error" not in txt:
                    continue
            work_items.append((rel, png, i, len(pages)))

    print(f"\nTranscribing {len(work_items)} pages from {len(pdf_pages)} PDFs "
          f"using {model_name} on Vertex AI ({location}), {workers} workers @ {rpm} RPM cap...")
    print(f"Estimated cost: ~${0.007 * len(work_items):.2f}")

    limiter = RateLimiter(rpm)
    t_start = time.time()
    completed = 0
    failed = 0

    def task(item):
        rel, png, page_num, total = item
        t0 = time.time()
        text = transcribe_page(model, png, limiter)
        return rel, png, page_num, total, text, time.time() - t0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, it) for it in work_items]
        for fut in as_completed(futures):
            rel, png, page_num, total, text, dt = fut.result()
            completed += 1
            ok = text and "[vertex error" not in text and len(text) > 50
            shard = shard_path(cache, rel, page_num)
            shard.parent.mkdir(parents=True, exist_ok=True)
            shard.write_text(text)
            if ok:
                stitch_if_complete(cache, rel, total)
            else:
                failed += 1
            avg = (time.time() - t_start) / completed
            eta = avg * (len(work_items) - completed)
            tag = "✓" if ok else "✗"
            print(f"  {tag} [{completed}/{len(work_items)}] {rel} p{page_num} "
                  f"({len(text)}c, {dt:.1f}s) — ETA {eta/60:.1f}m, failed={failed}")

    print(f"\nDone. {completed - failed}/{completed} pages transcribed in "
          f"{(time.time()-t_start)/60:.1f}m. failed={failed}")
    if failed:
        print("Re-run the same command to retry failed pages.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--project", required=True, help="GCP project id")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-pro")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--categories")
    ap.add_argument("--match")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rpm", type=int, default=DEFAULT_RPM)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    cats = set(args.categories.split(",")) if args.categories else None
    process(cdir, args.project, args.location, args.model, args.limit, args.force,
            cats, args.match, args.workers, args.rpm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
