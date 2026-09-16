"""Extract individual exam/practice problems and link to topics + source material.

Walks the exam-side PDFs, splits into numbered problem stems, classifies each by
topic via the same dictionary used in analyze.py, and emits a study plan that
points back at the homework / in-class PDF that covers that topic.

Usage:
    python problems.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

from analyze import TOPICS, detect_course_id, topics_for_course

PROBLEM_HEAD = re.compile(
    r"^\s*(?:problem|question|q|exercise|ex|#)?\s*(\d{1,2})[.\):]\s+",
    re.I | re.M,
)
PROBLEM_HEAD_LOOSE = re.compile(
    r"(?:^|\n)\s*(?:problem|question|q|exercise|ex|#)?\s*(\d{1,2})[.\):]\s+(?=[A-Z(])",
    re.I,
)
SUBPART = re.compile(r"^\s*\([a-h]\)\s+", re.I | re.M)
EXAM_CATEGORIES = {"exams", "exam_solutions", "practice"}
PREP_CATEGORIES = {"homeworks", "hw_keys", "in_class"}

LOGISTICS_FILE_HINTS = ("details", "logistics", "info-", "syllab", "schedule")
LOGISTICS_PHRASES = (
    "dear ", "syllabus", "office hours", "academic integrity",
    "make-up", "honor code", "policy", "report to ", "you must submit",
    "please arrive", "no calculator", "bring your", "exam location",
)
PROBLEM_SIGNALS = (
    "?", "=", "compute", "calculate", "find", "test", "determine",
    "estimate", "construct", "what is", "p-value", "interval",
    " a.", " b.", " c.", " d.", "(5 points)", "(10 points)", "(1 point)",
    "(2 points)", "(3 points)", "(4 points)", "(6 points)", "(8 points)",
    "data on", "sample of", "frequencies", "table below", "consider",
    "suppose", "assume", "given that", "show that", "verify",
    "hypothesis", "anova", "regression", "distribution", "probability",
)


CHAPTER_TO_TOPIC: dict[int, list[str]] = {
    1: ["descriptive_statistics"],
    2: ["probability_basics", "conditional_probability", "independence"],
    3: ["discrete_distributions", "expected_value"],
    4: ["continuous_distributions"],
    5: ["joint_distributions"],
    6: ["point_estimation", "sampling_distributions"],
    7: ["confidence_intervals"],
    8: ["hypothesis_testing", "t_test", "z_test"],
    9: ["two_sample"],
    10: ["anova"],
    12: ["regression_simple", "correlation"],
    13: ["regression_multiple", "regression_assumptions"],
    14: ["categorical_data", "chi_squared", "nonparametric"],
}


# Strong tokens — if present in body, force this topic regardless of file
# location. Order matters: first match wins. More specific patterns first.
STRONG_TOPIC_PATTERNS: list[tuple[str, str]] = [
    (r"\bANOVA\b|\bMSTr\b|\bMSE\b|\bF\s*=\s*MS|sum of squares|treatment\s+mean", "anova"),
    (r"\bmultiple\s+(linear\s+)?regression\b|\bindicator\s+variable|interaction\s+term|adjusted\s+R\b|R\^?2|\bVIF\b", "regression_multiple"),
    (r"\bregression\b|least.squares|slope\s+intercept|\br\s*=\s*[-0-9]|\bcorrelation\s+coefficient\b|\bbeta_?[01]|\b\\hat\{?y\}?", "regression_simple"),
    (r"\bchi.squared?\b|\bchi-square|goodness.of.fit|contingency\s+table|expected\s+counts?", "chi_squared"),
    (r"\b(Wilcoxon|Mann.Whitney|Kruskal|sign\s+test|rank\s+sum)\b", "nonparametric"),
    (r"\bcategorical\s+data\b|two.way\s+table", "categorical_data"),
    (r"\b(paired|matched).*(t.test|t\s+statistic)|two[- ]sample\s+t|pooled\s+variance|two\s+population", "two_sample"),
    (r"\b(t.test|t\s+statistic|t\s+distribution|degrees of freedom)\b", "t_test"),
    (r"\b(z.test|z\s+statistic|z\s+score)\b", "z_test"),
    (r"\bnull hypothesis\b|\balternative hypothesis\b|\bp.value\b|\breject\s+H_?0|\bH_?0\b\s*:|\btype\s+I\s+error", "hypothesis_testing"),
    (r"\bconfidence\s+interval\b|\bCI\b\s*for|margin\s+of\s+error|\b95%\b|\b99%\b\s*(confidence|interval)", "confidence_intervals"),
    (r"\bsampling\s+distribution\b|central\s+limit\s+theorem|\bCLT\b|\bsample\s+mean\b\s*\\?bar", "sampling_distributions"),
    (r"\b(point\s+estimat|MLE|maximum\s+likelihood|method\s+of\s+moments|unbiased\s+estimator)\b", "point_estimation"),
    (r"\b(joint\s+(pdf|pmf|distribution|density)|marginal\s+(pdf|pmf|distribution)|covariance|Cov\(|conditional\s+density)\b", "joint_distributions"),
    (r"\b(Poisson|binomial|geometric|hypergeometric|Bernoulli|negative\s+binomial)\b", "discrete_distributions"),
    (r"\b(normal\s+distribution|gaussian|exponential\s+distribution|Weibull|lognormal|gamma\s+distribution|beta\s+distribution|uniform\s+distribution)\b", "continuous_distributions"),
    (r"\bE\s*[\(\[]\s*X\s*[\)\]]|\bexpected\s+value\b|\bE\s*\(\s*X\^2\s*\)|\bvariance\s+of\s+X\b", "expected_value"),
    (r"\bP\s*\(\s*[A-Z]\s*\|\s*[A-Z]\s*\)|\bconditional\s+probability\b|\bBayes", "conditional_probability"),
    (r"\b(independent\s+events|are\s+independent|mutually\s+independent)\b", "independence"),
    (r"\bsample\s+space\b|\bmutually\s+exclusive\b|\bcomplement\b|\bunion\b|\bintersection\b", "probability_basics"),
    (r"\b(stem.{0,3}leaf|histogram|boxplot|five.number\s+summary|quartile|IQR|standard\s+deviation\s+formula)\b", "descriptive_statistics"),
]
STRONG_COMPILED = [(re.compile(p, re.I), t) for p, t in STRONG_TOPIC_PATTERNS]


def strong_topic_match(body: str) -> str | None:
    for rx, topic in STRONG_COMPILED:
        if rx.search(body):
            return topic
    return None


def chapter_from_path(rel_path: str) -> int | None:
    m = re.search(r"chapter[s]?-(\d+)(?!\d)", rel_path.lower())
    return int(m.group(1)) if m else None


def chapters_from_filename(rel_path: str) -> list[int]:
    """Files named 'chapters-10-12-13-14' cover multiple chapters."""
    name = rel_path.lower()
    m = re.search(r"chapters?-(\d+(?:-\d+){1,})", name)
    if not m:
        return []
    return [int(x) for x in m.group(1).split("-") if x.isdigit() and 1 <= int(x) <= 20]


def classify_with_chapter_bias(body: str, rel_path: str) -> str | None:
    """Classify, with body-keyword override for unconstrained PDFs."""
    hits = topic_hits(body)
    strong = strong_topic_match(body)
    multi = chapters_from_filename(rel_path)
    ch = chapter_from_path(rel_path)
    # Multi-chapter filename: constrain to union; prefer strong match within set.
    if multi:
        allowed = set()
        for c in multi:
            allowed.update(CHAPTER_TO_TOPIC.get(c, []))
        if strong and strong in allowed:
            return strong
        if not hits:
            return strong
        constrained = {t: c for t, c in hits.items() if t in allowed}
        if constrained:
            return max(constrained, key=constrained.get)
        return strong or max(hits, key=hits.get)
    # Single-chapter folder: chapter constraint usually correct, but allow
    # strong override IF strong topic is in same chapter group.
    if ch is not None:
        allowed = set(CHAPTER_TO_TOPIC.get(ch, []))
        if strong and strong in allowed:
            return strong
        if not allowed:
            return strong or (max(hits, key=hits.get) if hits else None)
        if not hits:
            return strong if strong in allowed else None
        constrained = {t: c for t, c in hits.items() if t in allowed}
        if constrained:
            return max(constrained, key=constrained.get)
        return strong or max(hits, key=hits.get)
    # No chapter info: strong body match wins, then global token-max.
    if strong:
        return strong
    if not hits:
        return None
    return max(hits, key=hits.get)


def is_logistics_file(rel_path: str) -> bool:
    name = rel_path.lower()
    return any(h in name for h in LOGISTICS_FILE_HINTS)


_DATA_LATEX_RE = re.compile(r"\$\$?\s*[\d.\s\\quad\-]+\s*\$\$?")
_PAGE_MARKER_RE = re.compile(r"---\s*page\s+\d+\s*---", re.I)

# Solution-tail markers — once seen, everything after is the worked answer.
_SOLUTION_MARKERS = re.compile(
    r"(?:"
    r"\\boxed\{[^}]+\}"
    r"|\bSolution\s*[:.]"
    r"|\bAnswer\s*[:.]"
    r"|\bAns\s*[:.]"
    r"|\bSoln\s*[:.]"
    r")",
    re.I,
)
# Detect end of MC choice block: a line starting with last option letter D/E,
# then math/computation following.
_MC_LINE = re.compile(r"^\s*\(?[A-E]\)?\.\s+", re.M)


def clean_stem(text: str) -> str:
    """Strip OCR artifacts that render badly in the UI."""
    text = _PAGE_MARKER_RE.sub(" ", text)
    text = _DATA_LATEX_RE.sub(lambda m: " " + re.sub(r"\\quad", "  ", m.group(0).strip("$")).strip() + " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_question_answer(body: str) -> tuple[str, str | None]:
    """Split solution-document body into (question_stem, worked_answer).

    HIGH-CONFIDENCE ONLY. We previously used heuristic 'equation-after-MC'
    detection which produced false positives (next problem's preamble or
    unrelated math got tagged as 'the answer'). Now we ONLY split when:

    - Explicit marker present: `Solution:`, `Answer:`, `Soln:`, `Ans:`,
      `\\boxed{...}` near the END (not embedded in a data string).

    Anything else → no answer extracted, no green toggle shown. Better to
    show the question alone than to mislead the student with wrong "key".
    """
    m = _SOLUTION_MARKERS.search(body)
    if not m:
        return body, None
    # Reject if marker is in first 30 chars (likely matched the question stem
    # itself, e.g. "1. Answer the following...").
    if m.start() < 30:
        return body, None
    return body[:m.start()].rstrip(), body[m.start():].strip()


def looks_like_problem(stem: str) -> bool:
    s = stem.lower()
    if any(phrase in s for phrase in LOGISTICS_PHRASES):
        return False
    if not any(sig in s for sig in PROBLEM_SIGNALS):
        return False
    return True


def topic_hits(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for topic, aliases in TOPICS.items():
        n = sum(len(re.findall(p, text, re.I)) for p in aliases)
        if n:
            out[topic] = n
    return out


_PREAMBLE_RE = re.compile(
    r"(?:assume|use|consider|refer\s+to)\b[^.\n]{0,80}?\b(?:question|problem)s?\s+"
    r"(\d{1,2})\s*(?:-|–|—|to|through|thru)\s*(\d{1,2})",
    re.I,
)


def extract_preambles(text: str) -> list[tuple[int, int, str, int, int]]:
    """Find 'Assume the following for questions N-M:' blocks.

    Returns list of (lo, hi, preamble_text, region_start, region_end).
    region_end approximates end of the last problem in the range; preamble
    only applies to bodies whose offset falls inside [region_start, region_end].
    """
    out = []
    for m in _PREAMBLE_RE.finditer(text):
        try:
            n_lo, n_hi = int(m.group(1)), int(m.group(2))
        except ValueError:
            continue
        if n_hi < n_lo or n_hi - n_lo > 12:
            continue
        tail_start = m.end()
        next_q = re.search(r"\n\s*\d{1,2}[.\):]\s+", text[tail_start:])
        body_end = tail_start + next_q.start() if next_q else tail_start + 600
        pre_text = text[m.start():body_end].strip()
        # Region end: walk forward looking for problem-head numbered (n_hi + 1)
        # or, if not found, cap at +8000 chars from preamble start.
        end_pat = re.compile(rf"\n\s*{n_hi + 1}[.\):]\s+")
        em = end_pat.search(text, m.end())
        region_end = em.start() if em else min(len(text), m.start() + 8000)
        out.append((n_lo, n_hi, pre_text, m.start(), region_end))
    return out


def find_preamble_for(num: int, body_offset: int,
                      preambles: list[tuple[int, int, str, int, int]]) -> str | None:
    for lo, hi, txt, r_start, r_end in preambles:
        if lo <= num <= hi and r_start <= body_offset < r_end:
            return txt
    return None


def split_problems(text: str) -> list[tuple[int, str, int]]:
    """Split a PDF text dump into (problem_num, body, body_offset) tuples.

    Boundaries: next numbered problem head OR start of a preamble block
    ('Assume the following for questions N-M:'). Without the preamble
    boundary, a preamble at the END of one problem leaks into the next.
    """
    matches = list(PROBLEM_HEAD.finditer(text))
    if not matches:
        matches = list(PROBLEM_HEAD_LOOSE.finditer(text))
    if not matches:
        return [(1, text, 0)]
    preamble_starts = [m.start() for m in _PREAMBLE_RE.finditer(text)]
    problems = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        next_q = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Earliest boundary after start: next problem head OR next preamble.
        next_pre = next((p for p in preamble_starts if p > start), len(text))
        end = min(next_q, next_pre)
        body = text[start:end].strip()
        if 30 < len(body) < 6000:
            problems.append((num, body, start))
    return problems


def split_pages_as_problems(pages: list[str]) -> list[tuple[int, str, int]]:
    """Fallback: treat each page as one problem (for handwritten/scan-only PDFs)."""
    out = []
    offset = 0
    for i, p in enumerate(pages):
        body = p.strip()
        if 60 < len(body) < 6000:
            out.append((i + 1, body, offset))
        offset += len(p) + 1
    return out


def extract_pdf_pages(path: Path, course_dir: Path | None = None) -> list[str]:
    if course_dir is not None:
        rel = str(path.relative_to(course_dir))
        ocr_file = course_dir / "_ocr" / (rel.replace("/", "__") + ".txt")
        if ocr_file.exists():
            text = ocr_file.read_text()
            parts = re.split(r"\n\n--- page \d+ ---\n\n", text)
            return [p for p in parts if p.strip()]
    try:
        return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
    except Exception:
        return []


def classify_problem(body: str) -> str | None:
    hits = topic_hits(body)
    if not hits:
        return None
    return max(hits, key=hits.get)


def collect_pdfs(course_dir: Path) -> dict[str, list[Path]]:
    """Group PDFs by category using the manifest written by bundle.py."""
    manifest = course_dir / "bundles" / "manifest.json"
    if not manifest.exists():
        raise SystemExit("Run bundle.py first.")
    rows = json.loads(manifest.read_text())
    grouped: dict[str, list[Path]] = defaultdict(list)
    for r in rows:
        grouped[r["category"]].append(course_dir / r["path"])
    return grouped


SOURCE_WEIGHTS = {
    "practice": 5,
    "exam_solutions": 4,
    "exams": 4,
    "hw_keys": 2,
    "homeworks": 2,
    "in_class": 1,
    "other": 0,
}

# Difficulty heuristics: more parts, longer body, higher point values, more
# complex math = higher difficulty.
SUBPART_RE = re.compile(r"\([a-h]\)", re.I)
POINTS_RE = re.compile(r"\((\d+)\s*points?\)", re.I)
HARD_TOKENS = (
    "anova", "regression", "tukey", "interaction",
    "multiple", "indicator", "joint", "covariance",
    "likelihood", "moment", "estimator", "p-value",
    "chi-square", "non-parametric", "studentized",
)
EASY_TOKENS = ("mean", "median", "mode", "histogram", "stem-and-leaf",
               "range", "boxplot", "skew", "uniform")


def difficulty_score(stem: str, body_len: int = 0) -> tuple[int, str]:
    """Return (0-10 score, label). Pure heuristic."""
    s = stem.lower()
    score = 0
    sub_parts = len(SUBPART_RE.findall(s))
    score += min(sub_parts * 2, 4)  # up to +4 for many parts
    pm = POINTS_RE.search(s)
    if pm:
        try:
            pts = int(pm.group(1))
            score += min(pts // 2, 3)  # up to +3 for high-point Qs
        except ValueError:
            pass
    if body_len > 800:
        score += 1
    if body_len > 1500:
        score += 1
    score += sum(1 for t in HARD_TOKENS if t in s)
    score -= sum(1 for t in EASY_TOKENS if t in s)
    score = max(0, min(score, 10))
    label = "easy" if score <= 3 else "medium" if score <= 6 else "hard"
    return score, label


def formula_topic_weights(course_dir: Path) -> dict[str, float]:
    """Score topics by how heavily they appear in the formula sheet.

    Strong signal that the prof expects students to USE that topic on the
    exam (the formula sheet is curated for the exam). Returns {topic: 0..1}
    where weight = topic_hits / max_topic_hits.
    """
    weights: dict[str, float] = {t: 0.0 for t in TOPICS}
    candidates = list(course_dir.glob("modules/**/*formulas*.pdf"))
    if not candidates:
        return weights
    text_all = ""
    for p in candidates:
        ocr = course_dir / "_ocr" / (str(p.relative_to(course_dir)).replace("/", "__") + ".txt")
        if ocr.exists():
            text_all += "\n" + ocr.read_text()
        else:
            try:
                text_all += "\n" + "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
            except Exception:
                pass
    if not text_all.strip():
        return weights
    hits = topic_hits(text_all)
    if not hits:
        return weights
    max_h = max(hits.values())
    for t, n in hits.items():
        weights[t] = n / max_h
    return weights


def likelihood_score(category: str, source: str, chapter: int | None,
                     topic: str | None = None,
                     formula_weights: dict[str, float] | None = None) -> float:
    """Probability proxy of appearing on the final exam. 0-100."""
    score = 0.0
    score += SOURCE_WEIGHTS.get(category, 0) * 10  # up to 50
    src = source.lower()
    if "extra-practice" in src or "practice" in src:
        score += 30
    if "final" in src:
        score += 25
    if "e1" in src or "e2" in src or "exam-1" in src or "exam-2" in src:
        score += 15
    if chapter and chapter >= 10:
        score += 10
    elif chapter and chapter >= 6:
        score += 5
    # Formula-sheet boost: heaviest topic gets +25, scaled linearly.
    if topic and formula_weights:
        score += formula_weights.get(topic, 0.0) * 25
    return round(score, 1)


def build_study_plan(course_dir: Path) -> None:
    global TOPICS
    TOPICS = topics_for_course(detect_course_id(course_dir) or 0)
    grouped = collect_pdfs(course_dir)
    prep_pdfs = [p for cat in PREP_CATEGORIES for p in grouped.get(cat, [])]
    fweights = formula_topic_weights(course_dir)
    if fweights:
        top5 = sorted(fweights.items(), key=lambda kv: -kv[1])[:5]
        print("formula-sheet topic weights (top 5):", top5)

    prep_index: dict[str, list[str]] = defaultdict(list)
    for p in prep_pdfs:
        text = "\n".join(extract_pdf_pages(p, course_dir))
        for topic in topic_hits(text):
            prep_index[topic].append(str(p.relative_to(course_dir)))

    # Walk EVERY problem-bearing PDF, tag with source category
    by_topic: dict[str, list[dict]] = defaultdict(list)
    all_categories = EXAM_CATEGORIES | PREP_CATEGORIES
    for cat in all_categories:
        for p in grouped.get(cat, []):
            rel = str(p.relative_to(course_dir))
            if is_logistics_file(rel):
                continue
            pages = extract_pdf_pages(p, course_dir)
            joined = ""
            page_breaks = []
            for i, pg in enumerate(pages):
                page_breaks.append((len(joined), i + 1))
                joined += "\n" + pg + "\n"

            splits = split_problems(joined)
            # Fall back to per-page if splitter found only 1 chunk == whole doc
            if len(splits) <= 1 and len(pages) > 1:
                splits = split_pages_as_problems(pages)

            preambles = extract_preambles(joined)

            for num, body, body_offset in splits:
                pre = find_preamble_for(num, body_offset, preambles)
                if pre and pre not in body:
                    body = pre + "\n\n" + body
                topic = classify_with_chapter_bias(body, rel)
                if not topic:
                    continue
                q_part, ans_part = split_question_answer(body)
                full_body = clean_stem(q_part)
                answer_md = clean_stem(ans_part) if ans_part else None
                stem = full_body[:280]
                if not looks_like_problem(stem):
                    continue
                offset = joined.find(body[:60]) if body else -1
                page = next(
                    (pn for off, pn in reversed(page_breaks) if off <= offset),
                    1,
                ) if offset >= 0 else num
                ch = chapter_from_path(rel)
                d_score, d_label = difficulty_score(stem, len(body))
                like = likelihood_score(cat, rel, ch, topic=topic,
                                        formula_weights=fweights)
                by_topic[topic].append({
                    "source": rel,
                    "category": cat,
                    "source_weight": SOURCE_WEIGHTS.get(cat, 0),
                    "page": page,
                    "problem": num,
                    "stem": stem,
                    "full_body": full_body,
                    "answer": answer_md,
                    "chapter": ch,
                    "difficulty": d_score,
                    "difficulty_label": d_label,
                    "likelihood": like,
                })

    # Dedup: same PDF reachable through multiple paths (e.g.
    # exam-1-materials/X.pdf and final-exam-materials/pages/_attachments/X.pdf)
    # produces identical problems. Key on (basename, problem_num, stem-prefix).
    # Prefer the shorter path / non-aggregator path.
    def _norm_base(src: str) -> str:
        """Treat 'foo-solutions.pdf' and 'foo.pdf' as same logical exam."""
        b = Path(src).name.lower()
        b = re.sub(r"-(solutions?|key|answers?)\.pdf$", ".pdf", b)
        return b

    def _dedup_priority(rec: dict) -> tuple:
        src = rec["source"]
        agg_penalty = 0
        if "/pages/_attachments/" in src:
            agg_penalty += 3
        if "final-exam-materials" in src and ("e1-" in src or "e2-" in src):
            agg_penalty += 2
        # Prefer record WITH answer over one without (for same logical Q).
        no_answer_penalty = 0 if rec.get("answer") else 1
        return (no_answer_penalty, agg_penalty, len(src), src)

    def _stem_fingerprint(s: str) -> str:
        # Strip LaTeX delimiters + punctuation + collapse whitespace; keep
        # alphanumerics so OCR variations like '$X$' vs 'X' collapse.
        s = re.sub(r"[^a-z0-9 ]+", "", s.lower())
        s = re.sub(r"\s+", " ", s).strip()
        return s[:60]

    for t, recs in by_topic.items():
        seen: dict[tuple, dict] = {}
        for r in recs:
            key = (_norm_base(r["source"]), r["problem"], _stem_fingerprint(r["stem"]))
            cur = seen.get(key)
            if cur is None or _dedup_priority(r) < _dedup_priority(cur):
                seen[key] = r
        by_topic[t] = list(seen.values())

    # Sort each topic's problems by source weight (likelihood proxy)
    for t in by_topic:
        by_topic[t].sort(key=lambda r: (-r["source_weight"], r["source"], r["page"], r["problem"]))

    # Sort topics by exam problem count desc
    ranked = sorted(by_topic.items(), key=lambda kv: -len(kv[1]))

    out = course_dir / "bundles" / "STUDY_PLAN.md"
    with out.open("w") as fh:
        fh.write(f"# Study plan: {course_dir.name}\n\n")
        fh.write(f"prep-side PDFs: {len(prep_pdfs)}\n\n")
        for topic, problems in ranked:
            fh.write(f"\n## {topic} — {len(problems)} problems\n\n")
            review = prep_index.get(topic, [])
            if review:
                fh.write("**Review these prep files:**\n")
                for f in sorted(set(review)):
                    fh.write(f"- `{f}`\n")
            else:
                fh.write("_(no matching prep material — likely under-prepared)_\n")
            fh.write("\n**Problems (sorted by source weight):**\n")
            for q in problems[:50]:
                fh.write(f"- [{q['category']}] `{q['source']}` p{q['page']} #{q['problem']}: {q['stem']}…\n")

    (course_dir / "bundles" / "problems.json").write_text(
        json.dumps({t: ps for t, ps in ranked}, indent=2)
    )
    print(f"Wrote {out} ({len(ranked)} topics, {sum(len(v) for v in by_topic.values())} problems)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1
    build_study_plan(cdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
