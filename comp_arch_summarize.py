"""Generate per-topic deep study summaries for CSE 30321 (Comp Arch).

Mirrors summarize_topics.py but uses comp_arch_topic_map.LECTURE_TO_TOPIC to
gather context per topic (slide PDFs by lecture number) instead of stats
chapter folders.

Output: <course_dir>/bundles/topics/<topic>.md
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from comp_arch_topic_map import LECTURE_TO_TOPIC, lecture_num_from_name
from topic_graph import _COMP_ARCH, prereqs_of, dependents_of, topo_sort

PROMPT = """You are an expert Computer Architecture tutor for CSE 30321 at Notre Dame.

Generate a comprehensive, exam-ready study summary for the topic below.
Use the lecture slides and homework material provided as the source of truth.
Match the professor's notation, vocabulary, and example style exactly when possible.

Topic: {label}
Lecture order index: {ch}
Prerequisites: {prereqs}

Write a Markdown summary with these sections (in order):

## 1. Definition & intuition
2-4 sentences. What it is, why it matters, what problem it solves.

## 2. When to use it
Bullet list of typical exam-question cues that mean "use this concept".

## 3. Key formulas / state diagrams / encodings
LaTeX (`$...$` inline, `$$...$$` block). Include EVERY formula or rule from
the slides. For state machines (e.g. MESI), draw with a Markdown table.
For RISC-V instructions, show the encoding fields.

## 4. Step-by-step procedure
Numbered steps the student would follow to answer a typical exam question
on this topic.

## 5. Worked examples
Pull 2-3 worked examples directly from the slide material below. Show
inputs, intermediate steps, final answer. Cite the source slide.

## 6. Common pitfalls / exam traps
What students get wrong. Off-by-one in cache index/tag, sign-extending
wrong field, forgetting WB stage, etc.

## 7. Connections
Link to prerequisite topics (`[topic_key]` style) and downstream topics.

## 8. Quick recall sheet
1-paragraph TL;DR you'd want to glance at the morning of the exam.

--- SOURCE MATERIAL (slide OCR + HW notes) ---
{context}
--- END SOURCE MATERIAL ---
"""


def gather_context(course_dir: Path, topic: str, max_chars: int = 100000) -> str:
    """Pull OCR text of all slide PDFs whose lecture number maps to this topic.
    Plus any HW .docx-extracted .txt files mentioning the topic by keyword.
    """
    parts: list[str] = []
    ocr_dir = course_dir / "_ocr"
    slides_dir = course_dir / "_external/google_drive_local/slides"

    # Slide PDFs whose lecture # maps to this topic
    if slides_dir.exists() and ocr_dir.exists():
        for pdf in sorted(slides_dir.glob("*.pdf")):
            n = lecture_num_from_name(pdf.name)
            if n is None or LECTURE_TO_TOPIC.get(n) != topic:
                continue
            rel = str(pdf.relative_to(course_dir))
            ocr_file = ocr_dir / (rel.replace("/", "__") + ".txt")
            if ocr_file.exists():
                parts.append(f"=== Lecture {n}: {pdf.stem} (OCR) ===\n"
                             + ocr_file.read_text())

    # HW .txt files (extracted from .docx earlier) — append all, model can
    # cherry-pick relevant ones.
    hw_dir = course_dir / "_external/google_drive_local/hw"
    if hw_dir.exists():
        for txt in sorted(hw_dir.glob("*.txt")):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            if len(content) < 100:
                continue
            parts.append(f"=== HW: {txt.stem} ===\n{content[:8000]}")

    # Panopto lecture transcripts — match by topic keywords.
    panopto_dir = course_dir / "_panopto"
    if panopto_dir.exists():
        sys.path.insert(0, str(Path(__file__).parent))
        from analyze import _TOPICS_COMP_ARCH
        import re as _re
        patterns = _TOPICS_COMP_ARCH.get(topic, [])
        compiled = [_re.compile(p, _re.I) for p in patterns]
        # Score each transcript by # of topic keyword matches; pick top 3
        scored: list[tuple[int, Path]] = []
        for txt in sorted(panopto_dir.glob("*.txt")):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            if len(content) < 500:
                continue
            score = sum(len(p.findall(content)) for p in compiled)
            if score >= 3:
                scored.append((score, txt))
        scored.sort(key=lambda r: -r[0])
        for score, txt in scored[:3]:
            content = txt.read_text(errors="ignore")
            parts.append(f"=== Lecture transcript {txt.stem[:8]}… "
                         f"(topic-relevance score {score}) ===\n{content[:12000]}")

    full = "\n\n".join(parts)
    return full[:max_chars]


def summarize_one(course_dir: Path, topic: str) -> tuple[str, str]:
    info = _COMP_ARCH.get(topic, {})
    if not info:
        return (topic, f"# {topic}\n\nUnknown topic.\n")
    context = gather_context(course_dir, topic)
    if not context.strip():
        return (topic, f"# {info.get('label', topic)}\n\n_No source material found yet (OCR may still be running)._\n")
    prereqs = ", ".join(prereqs_of(topic, _COMP_ARCH)) or "(none)"
    prompt = PROMPT.format(label=info["label"], ch=info["ch"],
                           prereqs=prereqs, context=context)
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    try:
        text = generate(prompt, max_output_tokens=16384)
    except Exception as e:
        return (topic, f"# {info['label']}\n\n_Generation failed: {e}_\n")
    return (topic, text)


def build_all(course_dir: Path, workers: int = 4, only: list[str] | None = None,
              skip_existing: bool = True) -> None:
    out_dir = course_dir / "bundles" / "topics"
    out_dir.mkdir(parents=True, exist_ok=True)
    topics = topo_sort(_COMP_ARCH) if not only else only
    pending = []
    for t in topics:
        out = out_dir / f"{t}.md"
        if skip_existing and out.exists() and out.stat().st_size > 500:
            print(f"  [skip] {t} (cached)")
            continue
        pending.append(t)
    print(f"Generating {len(pending)} summaries with {workers} workers...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(summarize_one, course_dir, t): t for t in pending}
        for fut in as_completed(futures):
            topic = futures[fut]
            try:
                _, text = fut.result()
                (out_dir / f"{topic}.md").write_text(text)
                print(f"  ✓ {topic} ({len(text)} chars)", flush=True)
            except Exception as e:
                print(f"  ✗ {topic}: {e}", flush=True)
    print(f"Done in {time.time()-t0:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic", action="append", default=[],
                    help="Specific topic key(s). Repeat. Default: all.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rebuild", action="store_true",
                    help="Regenerate even if cached file exists.")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    build_all(cdir, workers=args.workers,
              only=args.topic or None,
              skip_existing=not args.rebuild)
    return 0


if __name__ == "__main__":
    sys.exit(main())
