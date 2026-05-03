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


def gather_context(course_dir: Path, topic: str, max_chars: int = 80000) -> str:
    parts: list[str] = []
    formulas = course_dir / "modules/final-exam-materials/30440feformulas.pdf"
    if formulas.exists():
        from pypdf import PdfReader
        try:
            ftxt = "\n".join((p.extract_text() or "") for p in PdfReader(str(formulas)).pages)
            parts.append("=== FORMULA SHEET ===\n" + ftxt)
        except Exception:
            pass
    if (course_dir / "chroma").exists():
        sys.path.insert(0, str(Path(__file__).parent))
        from vectorize import get_collection
        coll = get_collection(course_dir)
        label = GRAPH.get(topic, {}).get("label", topic)
        try:
            res = coll.query(
                query_texts=[f"{topic} {label}"],
                n_results=20,
                where={"category": "in_class"},
            )
            for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
                parts.append(f"=== {meta['source']} p{meta['page']} ===\n{doc}")
        except Exception as e:
            parts.append(f"[context fail: {e}]")
    return "\n\n".join(parts)[:max_chars]


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


def build_all(course_dir: Path, only: str | None = None, rebuild: bool = False) -> None:
    out_dir = course_dir / "bundles" / "topics"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [only] if only else list(GRAPH.keys())
    for t in targets:
        f = out_dir / f"{t}.md"
        if f.exists() and not rebuild:
            print(f"  skip cached: {t}")
            continue
        print(f"  generating: {t} …")
        text = summarize_one(course_dir, t)
        f.write_text(text)
        print(f"    wrote {f} ({len(text)} chars)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic", help="just this topic")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1
    build_all(cdir, only=args.topic, rebuild=args.rebuild)
    return 0


if __name__ == "__main__":
    sys.exit(main())
