"""Topic dependency graph for ACMS 30440 (statistics).

Each entry: topic key (matches keys in analyze.py TOPICS) → {chapter, prereqs, label}.
Used by graph view and topological study path.
"""
from __future__ import annotations

GRAPH: dict[str, dict] = {
    "descriptive_statistics":   {"ch": 1,  "label": "Descriptive Statistics",            "prereqs": []},
    "probability_basics":       {"ch": 2,  "label": "Probability Basics",                "prereqs": ["descriptive_statistics"]},
    "conditional_probability":  {"ch": 2,  "label": "Conditional Probability",           "prereqs": ["probability_basics"]},
    "independence":             {"ch": 2,  "label": "Independence",                      "prereqs": ["conditional_probability"]},
    "discrete_distributions":   {"ch": 3,  "label": "Discrete Distributions",            "prereqs": ["probability_basics", "independence"]},
    "expected_value":           {"ch": 3,  "label": "Expected Value & Variance",         "prereqs": ["discrete_distributions"]},
    "continuous_distributions": {"ch": 4,  "label": "Continuous Distributions",          "prereqs": ["expected_value"]},
    "joint_distributions":      {"ch": 5,  "label": "Joint Distributions",               "prereqs": ["discrete_distributions", "continuous_distributions"]},
    "point_estimation":         {"ch": 6,  "label": "Point Estimation (MLE, MoM)",       "prereqs": ["joint_distributions"]},
    "sampling_distributions":   {"ch": 6,  "label": "Sampling Distributions & CLT",      "prereqs": ["point_estimation", "continuous_distributions"]},
    "confidence_intervals":     {"ch": 7,  "label": "Confidence Intervals (1 sample)",   "prereqs": ["sampling_distributions"]},
    "hypothesis_testing":       {"ch": 8,  "label": "Hypothesis Testing (1 sample)",     "prereqs": ["confidence_intervals"]},
    "z_test":                   {"ch": 8,  "label": "Z-test",                            "prereqs": ["hypothesis_testing"]},
    "t_test":                   {"ch": 8,  "label": "t-test",                            "prereqs": ["hypothesis_testing"]},
    "two_sample":               {"ch": 9,  "label": "Two-sample Inference",              "prereqs": ["t_test", "z_test"]},
    "anova":                    {"ch": 10, "label": "ANOVA + Tukey HSD",                 "prereqs": ["two_sample"]},
    "regression_simple":        {"ch": 12, "label": "Simple Linear Regression",          "prereqs": ["hypothesis_testing", "expected_value"]},
    "correlation":              {"ch": 12, "label": "Correlation",                       "prereqs": ["regression_simple"]},
    "regression_multiple":      {"ch": 13, "label": "Multiple Regression",               "prereqs": ["regression_simple"]},
    "regression_assumptions":   {"ch": 13, "label": "Regression Assumptions / Diagnostics", "prereqs": ["regression_multiple"]},
    "categorical_data":         {"ch": 14, "label": "Categorical Data Analysis",         "prereqs": ["hypothesis_testing"]},
    "chi_squared":              {"ch": 14, "label": "Chi-Squared Tests",                 "prereqs": ["categorical_data"]},
    "nonparametric":            {"ch": 14, "label": "Nonparametric Methods",             "prereqs": ["hypothesis_testing"]},
}


def topo_sort() -> list[str]:
    """Kahn's algorithm — returns topics in dependency order."""
    indeg = {k: 0 for k in GRAPH}
    for k, v in GRAPH.items():
        for p in v["prereqs"]:
            if p in GRAPH:
                indeg[k] += 1
    ready = [k for k, d in indeg.items() if d == 0]
    out: list[str] = []
    while ready:
        # Stable order: prefer lower chapter numbers
        ready.sort(key=lambda k: (GRAPH[k]["ch"], k))
        n = ready.pop(0)
        out.append(n)
        for k, v in GRAPH.items():
            if n in v["prereqs"]:
                indeg[k] -= 1
                if indeg[k] == 0:
                    ready.append(k)
    return out


def mermaid() -> str:
    lines = ["flowchart LR"]
    # Group by chapter for clearer layout
    seen = set()
    for k, v in GRAPH.items():
        label = f"{k}[\"Ch{v['ch']}: {v['label']}\"]"
        if k not in seen:
            lines.append(f"  {label}")
            seen.add(k)
    for k, v in GRAPH.items():
        for p in v["prereqs"]:
            if p in GRAPH:
                lines.append(f"  {p} --> {k}")
    return "\n".join(lines)


def prereqs_of(topic: str) -> list[str]:
    return GRAPH.get(topic, {}).get("prereqs", [])


def dependents_of(topic: str) -> list[str]:
    return [k for k, v in GRAPH.items() if topic in v["prereqs"]]
