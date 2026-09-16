"""High-quality OCR via local Claude vision.

For each problem-bearing PDF, renders pages to PNG (cached) and asks the
local `claude` CLI to transcribe each page verbatim, preserving math as LaTeX
and problem numbering. Output overwrites the Tesseract _ocr/ cache so the
rest of the pipeline picks it up automatically.

Usage:
    python ocr_claude.py --course-dir downloads/128781_statistics
    python ocr_claude.py --course-dir <dir> --limit 5      # test on N pdfs
    python ocr_claude.py --course-dir <dir> --force        # redo even if cached
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

CLAUDE = shutil.which("claude") or "claude"
ALL_PROBLEM_CATEGORIES = {
    "exams", "exam_solutions", "practice",
    "homeworks", "hw_keys", "in_class",
}
PAGE_SEP = "\n\n--- page {n} ---\n\n"
DPI = 200
TIMEOUT = 180

BASE_PROMPT = """Transcribe this page of a college {domain} document EXACTLY as it appears.

Rules:
- Preserve numbered problems (1., 2., (a), (b), etc.) on their own lines.
- Convert math to LaTeX: inline $...$, display $$...$$.
- Preserve tables as Markdown tables.
{domain_rules}
- For handwritten work, transcribe what is written; mark unclear chars as [?].
- Do NOT add commentary, headings, or summaries — output only the transcribed page text.
"""

# Per-domain extras. Wrong-domain hints cost accuracy: a statistics prompt tells
# the model to expect ANOVA tables on a page of pseudocode.
DOMAIN_RULES = {
    "statistics": "- Keep ANOVA tables, regression output, and statistical tables intact.",
    "algorithms": (
        "- Preserve pseudocode verbatim, including indentation, line numbers, and\n"
        "  loop/conditional structure; use a fenced code block for it.\n"
        "- Keep asymptotic notation exact: $O(n\\log n)$, $\\Theta(n^2)$, $\\Omega(n)$.\n"
        "- Preserve recurrences, summations, and induction steps as written.\n"
        "- Transcribe graph/tree figures as a short bracketed description, e.g.\n"
        "  [figure: directed graph, vertices a-e, edge weights labeled]."
    ),
    "generic": "- Keep figures as short bracketed descriptions, e.g. [figure: ...].",
}


def build_prompt(domain: str) -> str:
    rules = DOMAIN_RULES.get(domain, DOMAIN_RULES["generic"])
    return BASE_PROMPT.format(domain=domain, domain_rules=rules)


PROMPT = build_prompt("statistics")


def render_pages(pdf: Path, work: Path) -> list[Path]:
    work.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    images = convert_from_path(str(pdf), dpi=DPI)
    for i, img in enumerate(images):
        p = work / f"p{i+1:03}.png"
        img.save(p, "PNG")
        out.append(p)
    return out


def transcribe_page(png: Path, prompt_header: str = PROMPT) -> str:
    prompt = f"{prompt_header}\n\nImage path: {png}\n\nUse the Read tool to load and transcribe it."
    # Pass prompt via stdin so --allowedTools doesn't swallow it as a value.
    proc = subprocess.run(
        [CLAUDE, "-p", "--allowedTools", "Read"],
        input=prompt,
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if proc.returncode != 0:
        return f"[claude error: {proc.stderr[:200]}]"
    return proc.stdout.strip()


def process(course_dir: Path, limit: int | None, force: bool,
            categories: set[str] | None = None, match: str | None = None,
            workers: int = 8, domain: str = "statistics") -> None:
    prompt_header = build_prompt(domain)
    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first.")
    manifest = json.loads(manifest_file.read_text())
    cache = course_dir / "_ocr"
    cache.mkdir(exist_ok=True)
    done_marker = course_dir / "_ocr" / "_claude_done"
    done_set = set()
    if done_marker.exists():
        done_set = set(done_marker.read_text().splitlines())

    cats = categories or ALL_PROBLEM_CATEGORIES
    targets = [r for r in manifest if r["category"] in cats]
    if match:
        targets = [r for r in targets if match.lower() in r["path"].lower()]
    if limit:
        targets = targets[:limit]

    # Build a flat (rel, png_path, page_num, total_pages) work list across all PDFs
    work_items: list[tuple[str, Path, int, int]] = []
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
            work_items.append((rel, png, i, len(pages)))

    print(f"\nTranscribing {len(work_items)} pages from {len(pdf_pages)} PDFs "
          f"with {workers} workers (domain={domain})...")
    page_text: dict[tuple[str, int], str] = {}
    t_start = time.time()

    def task(item):
        rel, png, page_num, total = item
        t0 = time.time()
        text = transcribe_page(png, prompt_header)
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
            print(f"  [{completed}/{len(work_items)}] {rel} p{page_num} ({len(text)}c, {dt:.1f}s) — ETA {eta/60:.1f}m")

    # Stitch pages back per PDF and persist
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
    ap.add_argument("--limit", type=int, help="cap N pdfs (testing)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--categories", help="comma-separated subset, e.g. 'practice,exam_solutions'")
    ap.add_argument("--match", help="only PDFs whose path contains this substring")
    ap.add_argument("--workers", type=int, default=8, help="parallel claude invocations")
    ap.add_argument("--domain", default="statistics",
                    choices=sorted(DOMAIN_RULES), help="subject-specific transcription rules")
    ap.add_argument("--all", action="store_true",
                    help="OCR every categorized PDF, not just problem-bearing ones")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    cats = set(args.categories.split(",")) if args.categories else None
    if args.all:
        cats = ALL_PROBLEM_CATEGORIES | {"lectures", "tables", "textbook", "other"}
    process(cdir, args.limit, args.force, categories=cats, match=args.match,
            workers=args.workers, domain=args.domain)
    return 0


if __name__ == "__main__":
    sys.exit(main())
