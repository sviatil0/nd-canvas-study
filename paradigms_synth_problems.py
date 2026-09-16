"""Generate synthetic practice problems for paradigms topics.

For each topic that has < N real problems in problems.json, generate K
synthetic exam-style problems matching the prof's style (T/F, MCQ, free
response, output prediction, code writing). Saves into problems.json with
`synthetic: true` flag so UI can distinguish.

Style is grounded by the final-exam-guidelines doc + Exam 1 + Exam 2 OCR.
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

from topic_graph import _PARADIGMS, topo_sort

PROMPT = """You are generating practice problems for the CSE 30332 Programming
Paradigms FINAL EXAM (May 6, 2026, Notre Dame). Match Prof. Joanna Cecilia
da Silva Santos' question style exactly.

Topic: {label}
Topic key: {topic}

Below is (a) the prof's official sample-question list from the final exam
guidelines, (b) actual Exam 1 + Exam 2 question text, and (c) the topic's
summary content.

Generate {n} NEW practice problems for this topic. STRICT requirements:

1. Match the prof's tone, vocabulary, and formatting EXACTLY.
2. Mix question types proportional to what appears in actual exams:
   - Conceptual short-answer ("Explain the difference between X and Y")
   - True/False with justification
   - Multiple choice with 4-5 options
   - Output prediction ("What does this code print?")
   - Code-writing tasks
3. Each problem MUST be answerable from the topic's material — don't
   require knowledge from other topics.
4. Provide a complete answer key for each problem (numbered, terse).
5. Cover different aspects of the topic — don't repeat the same concept.

Return JSON only (no markdown fence, no commentary). Schema:

[
  {{
    "type": "conceptual" | "true_false" | "multiple_choice" | "output_prediction" | "code_writing",
    "question": "<full question text in prof's style, with code blocks if relevant>",
    "options": ["A. ...", "B. ...", ...] | null,
    "answer": "<complete answer/justification>",
    "difficulty": "easy" | "medium" | "hard",
    "concepts_tested": ["specific concept 1", "specific concept 2"]
  }},
  ...
]

--- (a) PROF'S OFFICIAL SAMPLE-QUESTION LIST + EXAM GUIDELINES ---
{guidelines}
--- (b) EXAM 1 + EXAM 2 ACTUAL QUESTIONS ---
{exam_text}
--- (c) TOPIC SUMMARY ---
{topic_summary}
"""


def load_exam_text(course_dir: Path) -> str:
    """Pull OCR'd Exam 1 + Exam 2 text + final exam guidelines."""
    parts = []
    g = course_dir / "_external/google_drive_remote/document/16w-Wm7rCsRCH6QUiSdMwRirSe-wM71-BOpmjORygakg.txt"
    guidelines = ""
    if g.exists():
        guidelines = g.read_text()[:9000]
    ocr_dir = course_dir / "_ocr"
    for name in ("exam-1-graded.pdf.txt", "exam-2-graded.pdf.txt"):
        for f in ocr_dir.glob(f"_external__google_drive_local__exams__{name}"):
            parts.append(f.read_text(errors="ignore"))
    exam = "\n\n".join(parts)[:18000]
    return guidelines, exam


def gen_problems_for_topic(course_dir: Path, topic: str,
                           guidelines: str, exam_text: str, n: int = 4) -> dict:
    info = _PARADIGMS.get(topic, {})
    summary_file = course_dir / "bundles" / "topics" / f"{topic}.md"
    topic_summary = summary_file.read_text() if summary_file.exists() else ""
    prompt = PROMPT.format(
        label=info.get("label", topic),
        topic=topic, n=n,
        guidelines=guidelines,
        exam_text=exam_text,
        topic_summary=topic_summary[:18000],
    )
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    try:
        text = generate(prompt, max_output_tokens=8192, temperature=0.4)
    except Exception as e:
        return {"topic": topic, "problems": [], "error": str(e)[:200]}
    # Extract JSON (strip ```json fences if any)
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        problems = json.loads(text)
    except json.JSONDecodeError as e:
        return {"topic": topic, "problems": [], "error": f"JSON parse: {e}",
                "raw": text[:500]}
    return {"topic": topic, "problems": problems}


def merge_into_problems_json(course_dir: Path, results: list[dict]) -> int:
    pfile = course_dir / "bundles" / "problems.json"
    by_topic = json.loads(pfile.read_text()) if pfile.exists() else {}
    added = 0
    for r in results:
        t = r["topic"]
        info = _PARADIGMS.get(t, {})
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
                        # Common schemas: {label,text} or {key,value}
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
    ap.add_argument("--n", type=int, default=4, help="problems per topic")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-existing", type=int, default=2,
                    help="generate only for topics with < N real problems")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")

    pfile = cdir / "bundles" / "problems.json"
    existing = json.loads(pfile.read_text()) if pfile.exists() else {}

    targets = args.topic or list(_PARADIGMS.keys())
    # Filter to under-supplied topics. min-existing = threshold; if a topic
    # already has >= this many real problems, skip it. min-existing=0 means
    # "always generate" (skip nothing). min-existing=2 means "skip if >=2".
    needed = []
    for t in targets:
        real = sum(1 for p in existing.get(t, []) if not p.get("synthetic"))
        if args.min_existing == 0 or real < args.min_existing:
            needed.append(t)
        else:
            print(f"  [skip] {t}: has {real} real problems")
    print(f"Generating {args.n} synthetic problems each for {len(needed)} topics...")

    guidelines, exam_text = load_exam_text(cdir)
    print(f"  guidelines: {len(guidelines)} chars, exam_text: {len(exam_text)} chars")

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(gen_problems_for_topic, cdir, t, guidelines,
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
