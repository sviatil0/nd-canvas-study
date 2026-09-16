"""Generate per-topic deep summaries for CSE 30332 (Paradigms).

Inputs:
  - Slide PDFs (OCR'd via ocr_vertex.py) under _external/google_drive_remote/presentation/
  - GDoc texts (extracted via gdoc_browser/fetch_drive_batch) under
    _external/google_drive_remote/document/
  - Panopto transcripts under _panopto/
  - Final exam guidelines + activity gdocs from prof site

Output: bundles/topics/<topic>.md
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from analyze import _TOPICS_PARADIGMS
from topic_graph import _PARADIGMS, prereqs_of, topo_sort

PROMPT = """You are an expert tutor for CSE 30332 Programming Paradigms at Notre Dame.

The student is studying for the FINAL EXAM (May 6, 2026, closed-book except for
ONE handwritten letter-sized double-sided cheatsheet). The exam may include
conceptual questions, programming output prediction, and code-writing tasks.

Generate a comprehensive, exam-ready study summary for the topic below.
Use the lecture material below as the source of truth — match the prof's
notation, vocabulary, and example style exactly.

Topic: {label}
Sequence index: {ch}
Prerequisites: {prereqs}

Write a Markdown summary with these sections:

## 1. Definition & intuition
2-4 sentences. What it is, why it matters in this paradigm.

## 2. When to use it / exam triggers
Bullet list of exam-question cues that signal this concept.

## 3. Key facts & syntax
Code blocks (```javascript / ```python / ```java / ```clojure) with the
EXACT syntax patterns the prof uses. Include every formula/rule from the
slides + activities.

## 4. Step-by-step procedure (for programming questions)
Numbered steps to attack a typical exam question on this topic.

## 5. Worked examples
2-3 examples directly from the source material below. Show inputs, code,
expected output. Cite the source.

## 6. Common pitfalls / exam traps
What students get wrong. Off-by-one bugs, scope confusion, == vs ===,
mutating shared state, etc.

## 7. Connections
Link to prerequisite topics (`[topic_key]`) and downstream topics.

## 8. Cheatsheet snippet (HARD-TO-REMEMBER, EXAM-LIKELY)
3-5 line cheatsheet entry the student would HANDWRITE on their cheatsheet.
Focus on: exact syntax that's easy to forget, edge cases, gotchas, table
of operators, distinguishing pairs (e.g. == vs ===, let vs var). Make it
DENSE — every word counts because their cheatsheet has limited space.

--- SOURCE MATERIAL ---
{context}
--- END SOURCE MATERIAL ---
"""


def gather_context(course_dir: Path, topic: str, max_chars: int = 100000) -> str:
    parts: list[str] = []
    ocr_dir = course_dir / "_ocr"
    docs_dir = course_dir / "_external/google_drive_remote/document"
    pres_dir = course_dir / "_external/google_drive_remote/presentation"
    panopto_dir = course_dir / "_panopto"
    import re as _re
    patterns = _TOPICS_PARADIGMS.get(topic, [])
    compiled = [_re.compile(p, _re.I) for p in patterns]

    # 1. Always include the FINAL EXAM GUIDELINES doc
    guidelines = docs_dir / "16w-Wm7rCsRCH6QUiSdMwRirSe-wM71-BOpmjORygakg.txt"
    if guidelines.exists():
        parts.append(f"=== FINAL EXAM GUIDELINES ===\n{guidelines.read_text()[:6000]}")

    # 2. OCR'd slide PDFs (ranked by topic match) — threshold 1, top 3
    if ocr_dir.exists():
        scored: list[tuple[int, Path]] = []
        for txt in ocr_dir.glob("*.txt"):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            score = sum(len(p.findall(content)) for p in compiled)
            if score >= 1:
                scored.append((score, txt))
        scored.sort(key=lambda r: -r[0])
        for score, txt in scored[:3]:
            parts.append(f"=== Slide deck (score={score}): {txt.stem[:30]} ===\n"
                         + txt.read_text(errors="ignore")[:18000])

    # 3. GDoc activities/HW (by relevance) — threshold 1
    if docs_dir.exists():
        scored = []
        for txt in docs_dir.glob("*.txt"):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            if len(content) < 200:
                continue
            score = sum(len(p.findall(content)) for p in compiled)
            if score >= 1:
                scored.append((score, txt))
        scored.sort(key=lambda r: -r[0])
        for score, txt in scored[:4]:
            parts.append(f"=== GDoc (score={score}): {txt.stem[:14]} ===\n"
                         + txt.read_text(errors="ignore")[:8000])

    # 4. Panopto transcripts (by relevance) — threshold 2
    if panopto_dir.exists():
        scored = []
        for txt in panopto_dir.glob("*.txt"):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            if len(content) < 500:
                continue
            score = sum(len(p.findall(content)) for p in compiled)
            if score >= 2:
                scored.append((score, txt))
        scored.sort(key=lambda r: -r[0])
        for score, txt in scored[:2]:
            parts.append(f"=== Lecture transcript (score={score}): {txt.stem[:8]}… ===\n"
                         + txt.read_text(errors="ignore")[:12000])

    # 5. If still starved (<5000 chars), pull biggest slide deck regardless
    current_len = sum(len(x) for x in parts)
    if current_len < 5000 and ocr_dir.exists():
        all_slides = sorted(
            [p for p in ocr_dir.glob("*.txt") if "presentation" in str(p)],
            key=lambda p: -p.stat().st_size,
        )
        for txt in all_slides[:2]:
            parts.append(f"=== Slide deck (fallback): {txt.stem[:30]} ===\n"
                         + txt.read_text(errors="ignore")[:15000])

    full = "\n\n".join(parts)
    return full[:max_chars]


def summarize_one(course_dir: Path, topic: str) -> tuple[str, str]:
    info = _PARADIGMS.get(topic, {})
    if not info:
        return (topic, f"# {topic}\n\nUnknown topic.\n")
    context = gather_context(course_dir, topic)
    if not context.strip():
        return (topic, f"# {info.get('label', topic)}\n\n_No source material._\n")
    prereqs = ", ".join(prereqs_of(topic, _PARADIGMS)) or "(none)"
    prompt = PROMPT.format(label=info["label"], ch=info["ch"],
                           prereqs=prereqs, context=context)
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    try:
        text = generate(prompt, max_output_tokens=16384)
    except Exception as e:
        return (topic, f"# {info['label']}\n\n_Generation failed: {e}_\n")
    return (topic, text)


def build_all(course_dir: Path, workers: int = 5, only: list[str] | None = None,
              skip_existing: bool = True) -> None:
    out_dir = course_dir / "bundles" / "topics"
    out_dir.mkdir(parents=True, exist_ok=True)
    topics = topo_sort(_PARADIGMS) if not only else only
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
    ap.add_argument("--topic", action="append", default=[])
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    build_all(cdir, workers=args.workers,
              only=args.topic or None,
              skip_existing=not args.rebuild)
    return 0


if __name__ == "__main__":
    sys.exit(main())
