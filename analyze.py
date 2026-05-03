"""Cross-reference exam/practice problems vs homework/in-class material.

Identifies topics that appear frequently in exams + practice but rarely in
homeworks + in-class notes. These are likely under-prepared areas.

Usage:
    python analyze.py --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Stats topic keyword dictionary. Each topic has a list of regex aliases.
TOPICS = {
    "descriptive_statistics":   [r"mean", r"median", r"mode", r"variance", r"standard deviation", r"quartile", r"percentile", r"boxplot", r"histogram", r"stem.{0,3}leaf"],
    "probability_basics":       [r"sample space", r"\bevent\b", r"complement", r"\bunion\b", r"intersection", r"mutually exclusive"],
    "conditional_probability":  [r"conditional probab", r"P\(.*\|.*\)", r"bayes"],
    "independence":             [r"\bindependent\b", r"independence"],
    "discrete_distributions":   [r"binomial", r"poisson", r"geometric", r"hypergeometric", r"bernoulli", r"negative binomial"],
    "continuous_distributions": [r"\bnormal\b", r"gaussian", r"exponential distribut", r"\buniform\b", r"\bgamma\b", r"\bbeta\b distribut", r"weibull", r"lognormal"],
    "expected_value":           [r"expected value", r"E\(X\)", r"E\[X\]", r"variance of"],
    "joint_distributions":      [r"joint distribut", r"marginal distribut", r"covariance", r"correlation"],
    "sampling_distributions":   [r"sampling distribut", r"central limit", r"\bCLT\b", r"\bsampling\b"],
    "point_estimation":         [r"point estimat", r"\bMLE\b", r"maximum likelihood", r"unbiased", r"method of moments"],
    "confidence_intervals":     [r"confidence interval", r"\bCI\b", r"margin of error"],
    "hypothesis_testing":       [r"hypothesis test", r"null hypothesis", r"alternative hypothesis", r"p-?value", r"reject", r"type I error", r"type II error", r"power of"],
    "t_test":                   [r"\bt-?test\b", r"t-?statistic", r"student.{0,3}t"],
    "z_test":                   [r"\bz-?test\b", r"z-?statistic", r"z-?score"],
    "chi_squared":              [r"chi.{0,3}squar", r"\bchi-?sq\b", r"χ.?2", r"goodness of fit"],
    "anova":                    [r"\bANOVA\b", r"analysis of variance", r"F-?test", r"F-?statistic", r"one-?way", r"two-?way"],
    "two_sample":               [r"two[- ]sample", r"paired", r"pooled", r"difference of means", r"difference of proportions"],
    "regression_simple":        [r"simple.{0,4}regression", r"linear regression", r"least squares", r"slope", r"intercept", r"residual"],
    "regression_multiple":      [r"multiple regression", r"multicollinearity", r"adjusted R", r"R-?squared"],
    "regression_assumptions":   [r"homoscedastic", r"heteroscedastic", r"normality of residual", r"linearity assump", r"\bQ-?Q plot\b"],
    "correlation":              [r"\bcorrelation\b", r"pearson", r"spearman", r"\br\^?2\b"],
    "nonparametric":            [r"nonparametric", r"rank.{0,4}sum", r"wilcoxon", r"sign test", r"mann.{0,3}whitney"],
    "categorical_data":         [r"contingency table", r"categorical", r"proportion test"],
}


def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="ignore")


def count_topics(text: str) -> Counter:
    c: Counter = Counter()
    for topic, aliases in TOPICS.items():
        total = 0
        for pat in aliases:
            total += len(re.findall(pat, text, flags=re.I))
        c[topic] = total
    return c


def normalize(c: Counter) -> dict[str, float]:
    total = sum(c.values()) or 1
    return {k: v / total for k, v in c.items()}


def analyze(course_dir: Path) -> None:
    bundles = course_dir / "bundles"
    if not bundles.exists():
        print(f"Run bundle.py first; missing {bundles}")
        return

    exam_text = "\n".join([
        load_text(bundles / "exams.md"),
        load_text(bundles / "exam_solutions.md"),
        load_text(bundles / "practice.md"),
    ])
    prep_text = "\n".join([
        load_text(bundles / "homeworks.md"),
        load_text(bundles / "hw_keys.md"),
        load_text(bundles / "in_class.md"),
    ])

    exam_counts = count_topics(exam_text)
    prep_counts = count_topics(prep_text)
    exam_norm = normalize(exam_counts)
    prep_norm = normalize(prep_counts)

    rows = []
    for t in TOPICS:
        gap = exam_norm[t] - prep_norm[t]
        ratio = exam_norm[t] / (prep_norm[t] + 1e-9)
        rows.append({
            "topic": t,
            "exam_hits": exam_counts[t],
            "prep_hits": prep_counts[t],
            "exam_share_pct": round(exam_norm[t] * 100, 2),
            "prep_share_pct": round(prep_norm[t] * 100, 2),
            "share_gap_pp": round(gap * 100, 2),
            "exam_to_prep_ratio": round(ratio, 2),
        })

    rows.sort(key=lambda r: r["share_gap_pp"], reverse=True)

    out = course_dir / "bundles" / "topic_gap_report.md"
    with out.open("w") as fh:
        fh.write("# Topic-frequency gap: exams vs prep material\n\n")
        fh.write("Topics ranked by share of exam mentions minus share of prep mentions.\n")
        fh.write("Positive gap = appears more in exams/practice than in HW/in-class. Study these.\n\n")
        fh.write("| topic | exam hits | prep hits | exam % | prep % | gap (pp) | exam/prep ratio |\n")
        fh.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for r in rows:
            fh.write(f"| {r['topic']} | {r['exam_hits']} | {r['prep_hits']} | {r['exam_share_pct']} | {r['prep_share_pct']} | {r['share_gap_pp']} | {r['exam_to_prep_ratio']} |\n")
        fh.write("\n## Top under-prepared topics (positive gap, exam_hits>=3)\n\n")
        for r in rows:
            if r["share_gap_pp"] > 0 and r["exam_hits"] >= 3:
                fh.write(f"- **{r['topic']}** — {r['exam_hits']} exam mentions vs {r['prep_hits']} prep mentions (gap {r['share_gap_pp']}pp)\n")

    (course_dir / "bundles" / "topic_gap.json").write_text(json.dumps(rows, indent=2))
    print(f"Wrote {out}")
    print("\nTop 10 under-prepared:")
    shown = 0
    for r in rows:
        if r["exam_hits"] >= 3:
            print(f"  {r['topic']:30}  exam={r['exam_hits']:4}  prep={r['prep_hits']:4}  gap={r['share_gap_pp']:+.2f}pp")
            shown += 1
            if shown >= 10:
                break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    course_dir = Path(args.course_dir)
    if not course_dir.is_dir():
        print(f"Not a directory: {course_dir}")
        return 1
    analyze(course_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
