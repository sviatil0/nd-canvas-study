"""Tool functions exposed to the chat agent.

Each tool returns plain JSON-serializable dicts/lists. Backend runs them
when Gemini calls them; Gemini receives the result + composes final answer.
"""
from __future__ import annotations

import json
from pathlib import Path


def _safe_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# -------- TOOL IMPLEMENTATIONS --------

def list_topics_by_likelihood(course_dir: Path, n: int = 20) -> dict:
    """Topics ranked by exam likelihood (formula-sheet weight + practice + recency)."""
    rows = _safe_json(course_dir / "bundles" / "likelihood.json") or []
    by_topic: dict[str, dict] = {}
    for r in rows:
        t = r["topic"]
        if t not in by_topic:
            by_topic[t] = {
                "topic": t, "label": r.get("topic_label", t),
                "chapter": r.get("chapter", 0),
                "max_score": r["score"], "n_problems": 0,
            }
        by_topic[t]["n_problems"] += 1
        by_topic[t]["max_score"] = max(by_topic[t]["max_score"], r["score"])
    ranked = sorted(by_topic.values(), key=lambda x: -x["max_score"])
    return {"topics": ranked[:n]}


def list_topics_by_underprep(course_dir: Path, n: int = 20) -> dict:
    """Topics ranked by under-preparation gap (exam frequency vs prep coverage)."""
    rows = _safe_json(course_dir / "bundles" / "topic_gap.json") or []
    rows.sort(key=lambda r: -r.get("share_gap_pp", 0))
    return {"topics": rows[:n]}


def list_problems_for_topic(course_dir: Path, topic: str, n: int = 10) -> dict:
    """Top problems for a topic (by likelihood). Returns stem + source."""
    data = _safe_json(course_dir / "bundles" / "problems.json") or {}
    probs = data.get(topic, [])
    out = []
    for p in probs[:n]:
        out.append({
            "source": p["source"],
            "page": p["page"],
            "problem": p["problem"],
            "stem": p["stem"][:200],
            "category": p.get("category"),
            "difficulty": p.get("difficulty_label"),
            "likelihood": p.get("likelihood"),
        })
    return {"topic": topic, "n_total": len(probs), "problems": out}


def get_topic_summary(course_dir: Path, topic: str) -> dict:
    """Full Markdown summary for a topic, if generated."""
    p = course_dir / "bundles" / "topics" / f"{topic}.md"
    if not p.exists():
        return {"topic": topic, "summary": None,
                "msg": "no summary generated; run summarize_topics.py"}
    text = p.read_text()
    return {"topic": topic, "summary": text[:8000]}


def list_recent_mistakes(course_dir: Path, n: int = 20) -> dict:
    """Wrong-question incidents from Gradescope (with score + rubric items + topic guess)."""
    summary = _safe_json(course_dir / "_gradescope" / "summary.json") or {}
    progress = _safe_json(course_dir / "bundles" / "progress.json") or {}
    explanations = progress.get("mistake_explanations", {})
    out = []
    for a in summary.get("assignments", []):
        for w in a.get("wrong", []):
            key = f"{a['id']}#{w['question_id']}"
            exp = explanations.get(key, "")
            out.append({
                "assignment": a["name"],
                "question": w["title"],
                "score": w["score"],
                "max_score": w["max_score"],
                "lost": w["lost"],
                "rubric_items_count": len(w.get("rubric_items", [])),
                "comments": w.get("comments", "")[:160],
                "has_ai_explanation": bool(exp),
                "explanation_preview": (exp[:200] + "…") if len(exp) > 200 else exp,
            })
    out.sort(key=lambda r: -r["lost"])
    return {"total": len(out), "top_mistakes": out[:n]}


def get_mistake_explanation(course_dir: Path, assignment: str,
                            question: str) -> dict:
    """Full AI explanation for a specific wrong question. Pass assignment name + question title."""
    summary = _safe_json(course_dir / "_gradescope" / "summary.json") or {}
    progress = _safe_json(course_dir / "bundles" / "progress.json") or {}
    explanations = progress.get("mistake_explanations", {})
    asgn_l = assignment.lower().strip()
    q_l = question.lower().strip()
    for a in summary.get("assignments", []):
        if asgn_l not in a["name"].lower():
            continue
        for w in a.get("wrong", []):
            if q_l not in w["title"].lower():
                continue
            key = f"{a['id']}#{w['question_id']}"
            exp = explanations.get(key, "")
            return {
                "assignment": a["name"],
                "question": w["title"],
                "score": w["score"],
                "max_score": w["max_score"],
                "rubric_items_count": len(w.get("rubric_items", [])),
                "grader_comments": w.get("comments", ""),
                "ai_explanation": exp or "(not yet generated — open /mistakes/ and click Explain)",
            }
    return {"error": f"no match for assignment={assignment!r} question={question!r}"}


def list_mistakes_by_assignment(course_dir: Path, assignment: str) -> dict:
    """All wrong questions in one assignment (e.g. 'E1', 'E2-S26', 'HW3', 'Quiz 5')."""
    summary = _safe_json(course_dir / "_gradescope" / "summary.json") or {}
    asgn_l = assignment.lower().strip()
    out = []
    for a in summary.get("assignments", []):
        if asgn_l not in a["name"].lower():
            continue
        for w in a.get("wrong", []):
            out.append({
                "assignment": a["name"],
                "question": w["title"],
                "score": w["score"],
                "max_score": w["max_score"],
                "lost": w["lost"],
                "comments": w.get("comments", "")[:160],
            })
    return {"assignment_filter": assignment, "matched": len(out), "questions": out}


def list_saved_problems(course_dir: Path) -> dict:
    """User's starred problems."""
    progress = _safe_json(course_dir / "bundles" / "progress.json") or {}
    saved = progress.get("saved", [])
    return {"count": len(saved), "saved": [
        {"topic": s.get("topic"), "source": s.get("source"),
         "page": s.get("page"), "problem": s.get("problem"),
         "stem": s.get("stem", "")[:200]} for s in saved
    ]}


def get_class_info(course_dir: Path) -> dict:
    """Course logistics: exam dates, allowed materials, calculator policy, etc."""
    info = _safe_json(course_dir / "bundles" / "class_info.json")
    if not info:
        return {"msg": "no class_info.json — run class_info.py"}
    return info


def get_formula_sheet(course_dir: Path) -> dict:
    """Full text of the official formula sheet (OCR'd)."""
    for p in course_dir.glob("modules/**/*formulas*.pdf"):
        rel = str(p.relative_to(course_dir))
        ocr = course_dir / "_ocr" / (rel.replace("/", "__") + ".txt")
        if ocr.exists():
            return {"source": rel, "text": ocr.read_text()[:12000]}
    return {"msg": "formula sheet not found / not OCR'd"}


# -------- TOOL REGISTRY --------

TOOLS = {
    "list_topics_by_likelihood": {
        "fn": list_topics_by_likelihood,
        "params": {"n": int},
        "description": "Get topics ranked by likelihood of appearing on the final exam (combines formula-sheet weight, practice frequency, past-exam frequency, chapter recency).",
        "param_schema": {
            "type": "OBJECT",
            "properties": {"n": {"type": "INTEGER", "description": "How many topics (default 20)"}},
        },
    },
    "list_topics_by_underprep": {
        "fn": list_topics_by_underprep,
        "params": {"n": int},
        "description": "Get topics where exam frequency exceeds prep coverage — the most under-prepared topics.",
        "param_schema": {
            "type": "OBJECT",
            "properties": {"n": {"type": "INTEGER", "description": "How many topics (default 20)"}},
        },
    },
    "list_problems_for_topic": {
        "fn": list_problems_for_topic,
        "params": {"topic": str, "n": int},
        "description": "Top problems for one topic (by likelihood). Topic key is e.g. 'anova', 'regression_simple', 'discrete_distributions'.",
        "param_schema": {
            "type": "OBJECT",
            "properties": {
                "topic": {"type": "STRING", "description": "Topic key"},
                "n": {"type": "INTEGER", "description": "How many problems (default 10)"},
            },
            "required": ["topic"],
        },
    },
    "get_topic_summary": {
        "fn": get_topic_summary,
        "params": {"topic": str},
        "description": "Full deep-dive summary (definitions, formulas, worked examples) for a topic.",
        "param_schema": {
            "type": "OBJECT",
            "properties": {"topic": {"type": "STRING", "description": "Topic key"}},
            "required": ["topic"],
        },
    },
    "list_recent_mistakes": {
        "fn": list_recent_mistakes,
        "params": {"n": int},
        "description": "Wrong-question incidents from Gradescope, sorted by points lost. Includes rubric, grader comments, and whether an AI explanation has been generated.",
        "param_schema": {
            "type": "OBJECT",
            "properties": {"n": {"type": "INTEGER", "description": "How many mistakes (default 20)"}},
        },
    },
    "get_mistake_explanation": {
        "fn": get_mistake_explanation,
        "params": {"assignment": str, "question": str},
        "description": "Full AI explanation for one specific wrong question. Use partial substring matches for assignment + question title.",
        "param_schema": {
            "type": "OBJECT",
            "properties": {
                "assignment": {"type": "STRING", "description": "Assignment name (e.g. 'E1', 'HW3', 'Quiz 5')"},
                "question": {"type": "STRING", "description": "Question title (e.g. 'P1A', 'MC11', 'P3b')"},
            },
            "required": ["assignment", "question"],
        },
    },
    "list_mistakes_by_assignment": {
        "fn": list_mistakes_by_assignment,
        "params": {"assignment": str},
        "description": "List all wrong questions in a specific assignment (substring match on assignment name).",
        "param_schema": {
            "type": "OBJECT",
            "properties": {"assignment": {"type": "STRING", "description": "Assignment name filter"}},
            "required": ["assignment"],
        },
    },
    "list_saved_problems": {
        "fn": list_saved_problems,
        "params": {},
        "description": "User's starred / bookmarked problems.",
        "param_schema": {"type": "OBJECT", "properties": {}},
    },
    "get_class_info": {
        "fn": get_class_info,
        "params": {},
        "description": "Course logistics: exam dates, location, allowed materials, calculator policy.",
        "param_schema": {"type": "OBJECT", "properties": {}},
    },
    "get_formula_sheet": {
        "fn": get_formula_sheet,
        "params": {},
        "description": "Full text of the official formula sheet for the final exam.",
        "param_schema": {"type": "OBJECT", "properties": {}},
    },
}


def vertex_tool_declarations():
    """Return Vertex AI Tool object suitable for GenerativeModel(tools=[...])."""
    from vertexai.generative_models import FunctionDeclaration, Tool
    decls = []
    for name, spec in TOOLS.items():
        decls.append(FunctionDeclaration(
            name=name,
            description=spec["description"],
            parameters=spec["param_schema"],
        ))
    return Tool(function_declarations=decls)


def call_tool(name: str, args: dict, course_dir: Path):
    spec = TOOLS.get(name)
    if not spec:
        return {"error": f"unknown tool {name}"}
    fn = spec["fn"]
    # Filter only known params
    clean_args = {k: v for k, v in (args or {}).items() if k in spec["params"]}
    try:
        return fn(course_dir, **clean_args)
    except TypeError as e:
        return {"error": f"bad args for {name}: {e}"}
    except Exception as e:
        return {"error": f"{name} failed: {e}"}
