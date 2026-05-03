"""LLM problem solver — Vertex AI Gemini 2.5 Pro (default) with local Claude fallback.

Switch backend via env:
    USE_BACKEND=gemini   (default; needs GCP_PROJECT)
    USE_BACKEND=claude   (local claude CLI; uses your Claude Code subscription)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT_SEC = 240

SYSTEM_PROMPT = (
    "You are an expert statistics tutor for ACMS 30440 at Notre Dame. "
    "Solve the student's problem step by step. Show every formula used, "
    "every intermediate calculation, and the final answer with units. "
    "Cite which course concepts/chapters apply. If the problem is multiple "
    "choice, identify the correct option and explain why each distractor is wrong. "
    "Use Markdown with LaTeX for formulas ($...$ inline, $$...$$ block). "
    "Do NOT call any tools or read files; respond only with the worked solution."
)


def _gather_context(course_dir: Path, topic: str | None, max_chars: int = 40000) -> str:
    parts: list[str] = []
    formulas = course_dir / "modules/final-exam-materials/30440feformulas.pdf"
    if formulas.exists():
        from pypdf import PdfReader
        try:
            ftxt = "\n".join((p.extract_text() or "") for p in PdfReader(str(formulas)).pages)
            parts.append("=== FORMULA SHEET ===\n" + ftxt)
        except Exception:
            pass
    if topic and (course_dir / "chroma").exists():
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from vectorize import get_collection
            coll = get_collection(course_dir)
            res = coll.query(
                query_texts=[topic],
                n_results=6,
                where={"category": "in_class"},
            )
            for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
                parts.append(f"=== {meta['source']} p{meta['page']} ===\n{doc}")
        except Exception as e:
            parts.append(f"[context retrieval failed: {e}]")
    return ("\n\n".join(parts))[:max_chars]


def _solve_via_gemini(prompt: str) -> dict:
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    try:
        text = generate(prompt, system=SYSTEM_PROMPT, max_output_tokens=16384)
    except Exception as e:
        return {"error": str(e)}
    return {
        "answer": text,
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
        "input_tokens": len(prompt) // 4,
        "output_tokens": len(text) // 4,
        "cache_read": 0,
    }


def _solve_via_claude(prompt: str) -> dict:
    if not shutil.which("claude"):
        return {"error": "`claude` CLI not on PATH."}
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"claude CLI timed out after {TIMEOUT_SEC}s"}
    if proc.returncode != 0:
        return {"error": f"claude exit {proc.returncode}: {proc.stderr[:500]}"}
    return {
        "answer": proc.stdout.strip(),
        "model": "claude-code-cli (local subscription)",
        "input_tokens": len(prompt) // 4,
        "output_tokens": len(proc.stdout) // 4,
        "cache_read": 0,
    }


def solve(problem_text: str, course_dir: Path, topic: str | None = None) -> dict:
    context = _gather_context(course_dir, topic)
    prompt = (
        f"Topic hint: {topic or 'general'}\n\n"
        f"--- COURSE REFERENCE (use as needed; do not echo) ---\n"
        f"{context}\n"
        f"--- END REFERENCE ---\n\n"
        f"Problem to solve:\n\n{problem_text}\n\n"
        f"Walk me through it step by step."
    )
    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "claude":
        full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
        return _solve_via_claude(full_prompt)
    return _solve_via_gemini(prompt)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic")
    ap.add_argument("--problem", required=True)
    args = ap.parse_args()
    result = solve(args.problem, Path(args.course_dir), args.topic)
    print(json.dumps(result, indent=2))
