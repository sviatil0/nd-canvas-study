"""Generate concept summaries for each topic using local Claude CLI.

For each topic, queries the vector index for relevant in_class chunks +
formula sheet, then asks Claude to produce: definition, when-to-use,
key formulas, common pitfalls, prerequisite links.

Output: downloads/<course>/bundles/topics/<topic>.md (cached).

Usage:
    python summarize_topics.py --course-dir downloads/128781_statistics
    python summarize_topics.py --course-dir <dir> --topic anova --rebuild
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from topic_graph import GRAPH, prereqs_of

CLAUDE = shutil.which("claude") or "claude"
TIMEOUT = 180

PROMPT_TEMPLATE = """You are an expert statistics tutor for ACMS 30440 at Notre Dame.
Write a THOROUGH study guide for the topic: **{label}** (Chapter {ch}).

This is the student's primary learning resource — be detailed, not brief.
Aim for 1500–3000 words. Use the course reference material below extensively.

Structure your answer EXACTLY with these sections in order:

## Definition
2–4 sentences. What it is, what it solves, why it matters.

## Intuition
A paragraph or two building intuition. Use analogies. Why does this work?
What's the underlying mechanism? Don't just state — explain.

## When to use it
Bullet list of scenarios on the exam where this applies. Include the
identifying clues in problem wording that should trigger this method.
At least 5–8 bullets.

## Key formulas
For EACH formula:
- Display in LaTeX `$$...$$`
- One sentence on what each variable means
- One sentence on when to use this specific formula vs alternatives

Cover ALL the formulas a student needs for this topic, not just the main one.

## Step-by-step procedure
A numbered 5–10 step recipe for solving a typical problem of this type.
Include decision points (e.g., "if n < 30, use t-table; otherwise z-table").

## Common pitfalls
6–10 bullets of mistakes students make on this topic, each with the fix.

## Worked examples
At least TWO fully-worked examples with all calculations shown step-by-step
in LaTeX. Vary difficulty (one straightforward, one trickier with subparts).

## Connection to other topics
How does this relate to the prereqs and the topics it builds toward?
What's the bigger picture?

## Prerequisites
List the prereqs needed. Format each as
`- [topic_key](TOPIC_LINK_PLACEHOLDER:topic_key) — short reason it's needed`.
Available prereq keys: {prereq_keys}

## Quick recall sheet
A compact bullet reference of just the formulas + when-to-use, suitable for
last-minute review. ~10 lines max.

Use Markdown with $...$ inline math and $$...$$ block math throughout.
Keep tables as Markdown tables.

--- COURSE REFERENCE (use as needed; mine it for specific examples and notation) ---
{context}
--- END REFERENCE ---
"""


def gather_context(course_dir: Path, topic: str, max_chars: int = 120000) -> str:
    """Pull formula sheet + ALL chapter PDFs for the topic's chapter (full OCR text)
    + top-N vector-retrieved chunks from any other relevant material."""
    parts: list[str] = []
    seen_paths: set[str] = set()

    # 1. Formula sheet (always)
    formulas = course_dir / "modules/final-exam-materials/30440feformulas.pdf"
    if formulas.exists():
        from pypdf import PdfReader
        try:
            ftxt = "\n".join((p.extract_text() or "") for p in PdfReader(str(formulas)).pages)
            parts.append("=== FORMULA SHEET ===\n" + ftxt)
        except Exception:
            pass

    # 2. Full OCR text of every PDF in the topic's chapter
    info = GRAPH.get(topic, {})
    ch = info.get("ch")
    if ch:
        ocr_dir = course_dir / "_ocr"
        modules_root = course_dir / "modules"
        if modules_root.exists():
            chapter_dirs = [
                d for d in modules_root.iterdir()
                if d.is_dir() and (d.name.startswith(f"chapter-{ch}-") or
                                   d.name.startswith(f"chapters-{ch}-") or
                                   d.name == f"chapter-{ch}")
            ]
            for cdir in chapter_dirs:
                for pdf in sorted(cdir.rglob("*.pdf")):
                    if any(part in {"_ocr", "_pages", "_shards"} for part in pdf.parts):
                        continue
                    rel = str(pdf.relative_to(course_dir))
                    if rel in seen_paths:
                        continue
                    seen_paths.add(rel)
                    cache = ocr_dir / (rel.replace("/", "__") + ".txt")
                    if cache.exists():
                        parts.append(f"=== {rel} (OCR) ===\n{cache.read_text()}")

    # 3. Vector-retrieved chunks from in-class notes (skip if Chroma broken)
    chroma_dir = course_dir / "chroma"
    if chroma_dir.exists() and any(chroma_dir.iterdir()):
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from vectorize import get_collection
            coll = get_collection(course_dir)
            label = info.get("label", topic)
            res = coll.query(
                query_texts=[f"{topic} {label}"],
                n_results=20,
                where={"category": "in_class"},
            )
            for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
                tag = f"{meta['source']}#p{meta['page']}"
                if tag in seen_paths:
                    continue
                seen_paths.add(tag)
                parts.append(f"=== {meta['source']} p{meta['page']} (chunk) ===\n{doc}")
        except Exception:
            # Chroma broken — silently skip; chapter PDFs above provide enough context
            pass

    full = "\n\n".join(parts)
    return full[:max_chars]


def summarize_one(course_dir: Path, topic: str) -> str:
    import os
    info = GRAPH.get(topic)
    if not info:
        return f"# {topic}\n\nUnknown topic."
    context = gather_context(course_dir, topic)
    prompt = PROMPT_TEMPLATE.format(
        label=info["label"],
        ch=info["ch"],
        prereq_keys=", ".join(prereqs_of(topic)) or "(none)",
        context=context,
    )
    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "gemini":
        try:
            from gemini_client import generate
            return generate(prompt, max_output_tokens=16384)
        except Exception as e:
            return f"# {info['label']}\n\nGemini generation failed: {e}"
    proc = subprocess.run(
        [CLAUDE, "-p", prompt],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    if proc.returncode != 0:
        return f"# {info['label']}\n\nGeneration failed: {proc.stderr[:300]}"
    return proc.stdout.strip()


def build_all(course_dir: Path, only: str | None = None,
              rebuild: bool = False, workers: int = 4) -> None:
    out_dir = course_dir / "bundles" / "topics"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [only] if only else list(GRAPH.keys())
    # Filter to those needing build
    to_build = []
    for t in targets:
        f = out_dir / f"{t}.md"
        if f.exists() and not rebuild:
            print(f"  skip cached: {t}")
            continue
        to_build.append(t)
    if not to_build:
        return
    print(f"\nGenerating {len(to_build)} topic summaries with {workers} parallel workers...")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time
    t_start = _time.time()
    completed = 0

    def task(topic):
        t0 = _time.time()
        text = summarize_one(course_dir, topic)
        f = out_dir / f"{topic}.md"
        f.write_text(text)
        return topic, len(text), _time.time() - t0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, t) for t in to_build]
        for fut in as_completed(futures):
            topic, n, dt = fut.result()
            completed += 1
            avg = (_time.time() - t_start) / completed
            eta = avg * (len(to_build) - completed)
            print(f"  ✓ [{completed}/{len(to_build)}] {topic}: {n} chars in {dt:.1f}s (ETA {eta/60:.1f}m)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic", help="just this topic")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1
    build_all(cdir, only=args.topic, rebuild=args.rebuild, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
