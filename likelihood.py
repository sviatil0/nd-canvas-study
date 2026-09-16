"""Rank exam-side problems by likelihood of appearing on the upcoming exam.

Heuristic score per problem (higher = more likely):
  - topic frequency in PRACTICE PDFs (weight 3) — instructor-curated final review
  - topic frequency in past EXAM PDFs (weight 2) — recurring patterns
  - source-document boost: practice PDF (weight 4); past final/exam (weight 2)
  - chapter recency (later chapters get +1 — usually emphasized for finals)
  - share-gap (under-prepared topics get small bump)

Output: bundles/likelihood.json (sorted desc).

    python likelihood.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from analyze import TOPICS, detect_course_id, topics_for_course

EXAM_CATEGORIES = {"exams", "exam_solutions", "practice"}
PREP_CATEGORIES = {"homeworks", "hw_keys", "in_class"}


def topic_hits(text: str) -> Counter:
    c: Counter = Counter()
    for topic, aliases in TOPICS.items():
        n = sum(len(re.findall(p, text, re.I)) for p in aliases)
        if n:
            c[topic] = n
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    cdir = Path(args.course_dir)

    global TOPICS
    TOPICS = topics_for_course(detect_course_id(cdir) or 0)

    bundles = cdir / "bundles"
    problems_file = bundles / "problems.json"
    if not problems_file.exists():
        print("Run problems.py first.")
        return 1
    problems_by_topic: dict[str, list[dict]] = json.loads(problems_file.read_text())

    # Per-category corpora to count topic frequency
    def load(name: str) -> str:
        f = bundles / f"{name}.md"
        return f.read_text(errors="ignore") if f.exists() else ""

    practice_freq = topic_hits(load("practice"))
    exams_freq = topic_hits(load("exams") + "\n" + load("exam_solutions"))

    # Formula-sheet weights (heaviest signal — prof curates this for the exam)
    sys.path.insert(0, str(Path(__file__).parent))
    from problems import formula_topic_weights
    fweights = formula_topic_weights(cdir)

    from topic_graph import for_course as _graph_for_course
    GRAPH = _graph_for_course(detect_course_id(cdir) or 0) or __import__('topic_graph').GRAPH

    # share gap (if available)
    gap_file = bundles / "topic_gap.json"
    gap_by_topic = {}
    if gap_file.exists():
        for r in json.loads(gap_file.read_text()):
            gap_by_topic[r["topic"]] = r["share_gap_pp"]

    ranked: list[dict] = []
    for topic, problems in problems_by_topic.items():
        ch = GRAPH.get(topic, {}).get("ch", 0)
        for p in problems:
            score = 0.0
            score += 3.0 * practice_freq.get(topic, 0) / max(sum(practice_freq.values()), 1) * 100
            score += 2.0 * exams_freq.get(topic, 0) / max(sum(exams_freq.values()), 1) * 100
            # Formula-sheet boost — strongest exam-likelihood proxy.
            score += 4.0 * fweights.get(topic, 0.0) * 100
            src = p["source"].lower()
            if "practice" in src or "extra" in src:
                score += 4.0
            elif "exam" in src or "final" in src:
                score += 2.0
            if ch >= 10:
                score += 1.0
            score += max(gap_by_topic.get(topic, 0), 0) * 0.05
            ranked.append({
                "score": round(score, 3),
                "topic": topic,
                "topic_label": GRAPH.get(topic, {}).get("label", topic),
                "chapter": ch,
                "source": p["source"],
                "page": p["page"],
                "problem": p["problem"],
                "stem": p["stem"],
            })

    ranked.sort(key=lambda r: -r["score"])

    out = bundles / "likelihood.json"
    out.write_text(json.dumps(ranked, indent=2))
    print(f"wrote {out} ({len(ranked)} problems)")
    print("\nTop 15 most likely:")
    for r in ranked[:15]:
        print(f"  {r['score']:6.2f}  [{r['topic']:22}] {r['source']} p{r['page']} #{r['problem']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
