"""Generate synthetic practice problems for comp arch topics.

For each topic that has < N real problems in problems.json, generate K
synthetic exam-style problems matching prof's style. Saves into problems.json
with `synthetic: true` flag.

Style is grounded by midterm-1, midterm-2 OCR + final exam review slides.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from topic_graph import _COMP_ARCH

PROMPT = """You are generating practice problems for the CSE 30321 Computer
Architecture FINAL EXAM (Notre Dame, Spring 2026). Match the professor's
question style exactly.

Topic: {label}
Topic key: {topic}

Below are (a) the final exam REVIEW slide deck content, (b) actual Midterm 1 +
Midterm 2 question OCR, and (c) this topic's summary content.

Generate {n} NEW practice problems for this topic. STRICT requirements:

1. Match prof's tone, vocabulary, formatting EXACTLY.
2. Mix question types proportional to actual exams:
   - Conceptual short-answer
   - True/False with justification
   - Multiple choice (4-5 options)
   - Quantitative calculation (CPI, AMAT, cache bits, page table size, etc.)
   - RISC-V coding / pipeline tracing
3. Problems MUST be answerable from the topic's material.
4. Provide complete numerical answer + working steps.
5. Cover different aspects — don't repeat the same concept.

Return JSON only (no markdown fence). Schema:

[
  {{
    "type": "conceptual" | "true_false" | "multiple_choice" | "calculation" | "code_writing",
    "question": "<full question text in prof's style, with code blocks/diagrams if relevant>",
    "options": ["A. ...", "B. ...", ...] | null,
    "answer": "<complete answer with working steps>",
    "difficulty": "easy" | "medium" | "hard",
    "concepts_tested": ["specific concept 1", ...]
  }},
  ...
]

--- (a) FINAL EXAM REVIEW SLIDES ---
{review}
--- (b) MIDTERM 1 + MIDTERM 2 ACTUAL QUESTIONS ---
{exam_text}
--- (c) TOPIC SUMMARY ---
{topic_summary}
"""


def load_exam_text(course_dir: Path) -> tuple[str, str]:
    review = ""
    review_f = course_dir / "_ocr" / "_external__google_drive_local__slides__CSE 30321 SP26 Computer Architecture - 29 - Final Exam Review (marked).pdf.txt"
    if review_f.exists():
        review = review_f.read_text(errors="ignore")[:12000]
    parts = []
    ocr_dir = course_dir / "_ocr"
    for name in ("_external__google_drive_local__exams__midterm-1-graded.pdf.txt",
                 "_external__google_drive_local__exams__midterm-2-graded.pdf.txt"):
        f = ocr_dir / name
        if f.exists():
            parts.append(f.read_text(errors="ignore"))
    exam = "\n\n".join(parts)[:18000]
    return review, exam


def gen_problems_for_topic(course_dir: Path, topic: str,
                           review: str, exam_text: str, n: int = 4) -> dict:
    info = _COMP_ARCH.get(topic, {})
    summary_file = course_dir / "bundles" / "topics" / f"{topic}.md"
    topic_summary = summary_file.read_text() if summary_file.exists() else ""
    prompt = PROMPT.format(
        label=info.get("label", topic),
        topic=topic, n=n,
        review=review,
        exam_text=exam_text,
        topic_summary=topic_summary[:18000],
    )
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    last_err = None
    for attempt in range(2):
        try:
            text = generate(prompt, max_output_tokens=16384, temperature=0.4)
        except Exception as e:
            return {"topic": topic, "problems": [], "error": str(e)[:200]}
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```\s*$", "", text)
        try:
            problems = json.loads(text)
            return {"topic": topic, "problems": problems}
        except json.JSONDecodeError as e:
            # If truncated, try to salvage closing ] by trimming to last complete object
            try:
                # Find last "}" then close array
                last_close = text.rfind("}")
                if last_close > 0:
                    salvaged = text[:last_close+1].rstrip().rstrip(",") + "\n]"
                    problems = json.loads(salvaged)
                    return {"topic": topic, "problems": problems, "salvaged": True}
            except Exception:
                pass
            last_err = f"JSON parse: {e}"
    return {"topic": topic, "problems": [], "error": last_err, "raw": text[:500]}


def merge_into_problems_json(course_dir: Path, results: list[dict]) -> int:
    pfile = course_dir / "bundles" / "problems.json"
    by_topic = json.loads(pfile.read_text()) if pfile.exists() else {}
    added = 0
    for r in results:
        t = r["topic"]
        info = _COMP_ARCH.get(t, {})
        for i, p in enumerate(r.get("problems", [])):
            if not isinstance(p, dict) or "question" not in p:
                continue
            q = p["question"]
            opts = p.get("options") or []
            if opts and isinstance(opts, list):
                opts_str = []
                for o in opts:
                    if isinstance(o, str):
                        opts_str.append(o)
                    elif isinstance(o, dict):
                        label = o.get("label") or o.get("key") or ""
                        text = o.get("text") or o.get("value") or ""
                        opts_str.append(f"{label}. {text}".strip(". "))
                    else:
                        opts_str.append(str(o))
                if opts_str:
                    q = q + "\n\n" + "\n".join(opts_str)
            ans = p.get("answer", "")
            full_body = q + (f"\n\n**Answer (synthetic):**\n{ans}" if ans else "")
            problem_label = f"SYNTH-{i+1}"
            stem = q[:280]
            d_label = p.get("difficulty", "medium")
            d_score = {"easy": 3, "medium": 6, "hard": 9}.get(d_label, 6)
            by_topic.setdefault(t, []).append({
                "source": f"synthetic/{t}.md",
                "category": "practice",
                "source_weight": 3,
                "page": 0,
                "problem": problem_label,
                "stem": stem,
                "full_body": full_body,
                "answer": ans,
                "chapter": info.get("ch", 0),
                "difficulty": d_score,
                "difficulty_label": d_label,
                "likelihood": 65.0,
                "synthetic": True,
                "synth_type": p.get("type", "conceptual"),
                "concepts_tested": p.get("concepts_tested", []),
            })
            added += 1
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps(by_topic, indent=2))
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic", action="append", default=[])
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-existing", type=int, default=2)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")

    pfile = cdir / "bundles" / "problems.json"
    existing = json.loads(pfile.read_text()) if pfile.exists() else {}

    targets = args.topic or list(_COMP_ARCH.keys())
    needed = []
    for t in targets:
        real = sum(1 for p in existing.get(t, []) if not p.get("synthetic"))
        if args.min_existing == 0 or real < args.min_existing:
            needed.append(t)
        else:
            print(f"  [skip] {t}: has {real} real problems")
    print(f"Generating {args.n} synthetic problems each for {len(needed)} topics...")

    review, exam_text = load_exam_text(cdir)
    print(f"  review: {len(review)} chars, exam_text: {len(exam_text)} chars")

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(gen_problems_for_topic, cdir, t, review,
                             exam_text, args.n): t for t in needed}
        for fut in as_completed(futures):
            topic = futures[fut]
            try:
                r = fut.result()
                if r.get("error"):
                    print(f"  ✗ {topic}: {r['error']}")
                else:
                    print(f"  ✓ {topic}: {len(r['problems'])} problems")
                results.append(r)
            except Exception as e:
                print(f"  ✗ {topic}: {e}")
    added = merge_into_problems_json(cdir, results)
    print(f"\nMerged {added} synthetic problems in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
