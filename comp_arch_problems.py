"""Extract problems from comp arch exam OCR text + Gradescope rubric.

Output: bundles/problems.json with same shape as stats version, so the
existing topic_detail / mistakes / Solve UI all "just work".

Problem identification:
  * Source 1: Gradescope summary.json — gives per-question scores + rubric
    (already cached as `_gradescope/asgn_<aid>.json` per assignment).
  * Source 2: OCR text of exam PDFs — gives full question stem.

We pair Gradescope-question titles (e.g. "1.iii") with text spans in the
OCR transcript that immediately follow the matching label header
(e.g. "1.3\\n1.iii\\n3 / 4 pts\\n* ✓ - 0 pts Correct ...").

Topic mapping uses comp_arch_topic_map by lecture context — but exam
questions cross topics, so we classify each problem by content keywords
(reuse analyze.TOPICS comp arch dict).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from analyze import topics_for_course
from topic_graph import for_course

CHAPTER_FALLBACK = 0  # comp arch uses lecture index, not chapters


def classify_by_keywords(text: str, topics: dict) -> str | None:
    """Return topic with most keyword hits, or None."""
    counts: dict[str, int] = {}
    for topic, patterns in topics.items():
        n = 0
        for pat in patterns:
            try:
                n += len(re.findall(pat, text, re.I))
            except re.error:
                continue
        if n:
            counts[topic] = n
    if not counts:
        return None
    return max(counts, key=counts.get)


def extract_problems_from_doc(text: str, source_label: str) -> list[dict]:
    """Generic problem splitter for HW gdocs / practice docs.

    Splits on:
      - "Problem N:" / "Problem N (..):"
      - "Part N:"
      - Numbered top-level "1.", "2." at start of line followed by capital
    """
    out = []
    # Split markers
    pat = re.compile(
        r"(?:^|\n)\s*(?:"
        r"(?:Problem|Question)\s+(\d{1,2})[:.]?\s+(?P<title>[^\n]{2,140})"
        r"|(\d{1,2})\.\s+(?P<title2>[A-Z][^\n]{4,140})"
        r")",
        re.I,
    )
    matches = list(pat.finditer(text))
    if not matches:
        return out
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if len(body) < 80 or len(body) > 8000:
            continue
        num = m.group(1) or m.group(2) or "?"
        title = (m.group("title") or m.group("title2") or "").strip()
        out.append({
            "label": f"{num}. {title[:80]}",
            "body": body,
        })
    return out


def extract_questions_from_ocr(ocr_text: str, asgn_name: str) -> list[dict]:
    """Find question blocks in graded-exam OCR.

    Each block starts with line like "1.iii" / "2.iv" (the Gradescope
    sub-question label) followed by score "X / Y pts". Body extends until
    next label or "Question N" header.
    """
    out = []
    # Match labels like 1.i 2.iii 10.iv (digit, dot, lowercase roman)
    label_re = re.compile(
        r"^\s*(\d{1,2}\.\s*([ivxIVX]+))\s*\n\s*([\d.]+)\s*/\s*([\d.]+)\s*pts",
        re.M,
    )
    matches = list(label_re.finditer(ocr_text))
    for i, m in enumerate(matches):
        label_full = m.group(1).replace(" ", "")  # "1.iii"
        score = float(m.group(3))
        max_score = float(m.group(4))
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(ocr_text)
        body = ocr_text[body_start:body_end].strip()
        # Trim "Question N" / "Problem N" headers from end of body
        body = re.split(r"\n\s*(?:Question|Problem)\s+\d+\b", body)[0].strip()
        if len(body) < 30 or len(body) > 6000:
            continue
        out.append({
            "label": label_full,
            "score": score,
            "max_score": max_score,
            "body": body,
        })
    return out


def build_for_course(course_dir: Path) -> dict:
    """Walk exam OCR + emit problems.json compatible with topic_detail UI."""
    cdir = course_dir
    cid = None
    for part in cdir.name.split("_"):
        if part.isdigit():
            cid = int(part); break
    if cid is None:
        raise SystemExit(f"can't sniff cid from {cdir.name}")

    topics = topics_for_course(cid)
    graph = for_course(cid)

    by_topic: dict[str, list[dict]] = defaultdict(list)

    # Walk OCR cache files for exam_solutions / exams / practice
    ocr_dir = cdir / "_ocr"
    if not ocr_dir.exists():
        raise SystemExit("no _ocr cache — run ocr_vertex.py first")

    # Read manifest to identify which OCR files are exam_solutions
    manifest_file = cdir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        raise SystemExit("no bundles/manifest.json — run bundle.py first")
    manifest = json.loads(manifest_file.read_text())
    exam_paths = [r for r in manifest if r.get("category") in
                  ("exam_solutions", "exams", "practice")]

    print(f"Scanning {len(exam_paths)} exam/practice PDFs for problems...")

    for r in exam_paths:
        rel = r["path"]
        cat = r["category"]
        ocr_file = ocr_dir / (rel.replace("/", "__") + ".txt")
        if not ocr_file.exists():
            print(f"  [skip] no OCR for {rel}")
            continue
        text = ocr_file.read_text()
        # Use Gradescope-style label extraction for exam_solutions
        if cat == "exam_solutions":
            qs = extract_questions_from_ocr(text, Path(rel).stem)
            print(f"  [{cat:14}] {Path(rel).name}: {len(qs)} questions")
            for q in qs:
                topic = classify_by_keywords(q["body"], topics)
                if not topic:
                    continue
                key = f"{rel}#{q['label']}"
                # Mark wrong if score < max
                wrong = q["score"] < q["max_score"]
                lost = q["max_score"] - q["score"]
                # Likelihood: real exam questions = highest
                likelihood = round(80 + (10 if wrong else 0) + (5 if "midterm" in rel.lower() else 0), 1)
                # Difficulty heuristic from body length
                d_label = "hard" if len(q["body"]) > 1500 else (
                    "medium" if len(q["body"]) > 600 else "easy")
                d_score = 9 if d_label == "hard" else (6 if d_label == "medium" else 3)
                by_topic[topic].append({
                    "source": rel, "category": cat,
                    "source_weight": 5,
                    "page": 0, "problem": q["label"],
                    "stem": q["body"][:300],
                    "full_body": q["body"],
                    "answer": None,
                    "chapter": graph.get(topic, {}).get("ch", 0),
                    "difficulty": d_score,
                    "difficulty_label": d_label,
                    "likelihood": likelihood,
                    "score_received": q["score"],
                    "max_score": q["max_score"],
                    "you_got_wrong": wrong,
                    "key": key,
                })
        else:
            # Practice / lectures: use generic problem splitter.
            problems = extract_problems_from_doc(text, Path(rel).stem)
            print(f"  [{cat:14}] {Path(rel).name}: {len(problems)} problems")
            for p in problems:
                topic = classify_by_keywords(p["body"], topics)
                if not topic:
                    continue
                body = p["body"]
                d_label = "hard" if len(body) > 1500 else ("medium" if len(body) > 600 else "easy")
                d_score = 9 if d_label == "hard" else (6 if d_label == "medium" else 3)
                by_topic[topic].append({
                    "source": rel, "category": cat,
                    "source_weight": 4 if cat == "practice" else 2,
                    "page": 0, "problem": p["label"],
                    "stem": body[:300],
                    "full_body": body,
                    "answer": None,
                    "chapter": graph.get(topic, {}).get("ch", 0),
                    "difficulty": d_score,
                    "difficulty_label": d_label,
                    "likelihood": 75.0 if cat == "practice" else 60.0,
                })

    # Also scan HW gdoc-extracted text + practice .txt files (not in manifest)
    hw_dir = cdir / "_external/google_drive_local/hw"
    if hw_dir.exists():
        for txt in sorted(hw_dir.glob("*.gdoc.browser.txt")):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            problems = extract_problems_from_doc(content, txt.stem)
            print(f"  [hw_gdoc       ] {txt.stem[:60]}: {len(problems)} problems")
            for p in problems:
                topic = classify_by_keywords(p["body"], topics)
                if not topic:
                    continue
                body = p["body"]
                d_label = "hard" if len(body) > 1500 else ("medium" if len(body) > 600 else "easy")
                d_score = 9 if d_label == "hard" else (6 if d_label == "medium" else 3)
                rel = str(txt.relative_to(cdir))
                by_topic[topic].append({
                    "source": rel, "category": "homeworks",
                    "source_weight": 2,
                    "page": 0, "problem": p["label"],
                    "stem": body[:300],
                    "full_body": body,
                    "answer": None,
                    "chapter": graph.get(topic, {}).get("ch", 0),
                    "difficulty": d_score,
                    "difficulty_label": d_label,
                    "likelihood": 50.0,
                })
    practice_dir = cdir / "_external/google_drive_local/practice"
    if practice_dir.exists():
        for txt in sorted(practice_dir.glob("*.txt")):
            try:
                content = txt.read_text(errors="ignore")
            except Exception:
                continue
            problems = extract_problems_from_doc(content, txt.stem)
            print(f"  [practice_txt  ] {txt.stem[:60]}: {len(problems)} problems")
            for p in problems:
                topic = classify_by_keywords(p["body"], topics)
                if not topic:
                    continue
                body = p["body"]
                d_label = "hard" if len(body) > 1500 else ("medium" if len(body) > 600 else "easy")
                d_score = 9 if d_label == "hard" else (6 if d_label == "medium" else 3)
                rel = str(txt.relative_to(cdir))
                by_topic[topic].append({
                    "source": rel, "category": "practice",
                    "source_weight": 5,
                    "page": 0, "problem": p["label"],
                    "stem": body[:300],
                    "full_body": body,
                    "answer": None,
                    "chapter": graph.get(topic, {}).get("ch", 0),
                    "difficulty": d_score,
                    "difficulty_label": d_label,
                    "likelihood": 80.0,
                })

    # Write
    bundles = cdir / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    out_file = bundles / "problems.json"
    # Sort each topic by likelihood desc
    for t in by_topic:
        by_topic[t].sort(key=lambda r: -r.get("likelihood", 0))
    out_file.write_text(json.dumps(by_topic, indent=2))
    total = sum(len(v) for v in by_topic.values())
    print(f"\nWrote {out_file} ({len(by_topic)} topics, {total} problems)")
    return by_topic


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    build_for_course(cdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
