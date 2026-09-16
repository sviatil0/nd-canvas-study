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
    "You are an expert statistics tutor for ACMS 30440 at Notre Dame.\n\n"
    "CRITICAL RULES:\n"
    "1. Answer ONLY the exact problem inside <problem>...</problem>. Do NOT "
    "invent or substitute other questions, even if the stem looks incomplete.\n"
    "2. If the problem is multiple choice (has options A/B/C/D), your final "
    "answer MUST be one of those letters. Start your response with "
    "**Answer: X** where X is the chosen letter.\n"
    "3. If the problem is open-ended, give the numeric/symbolic final answer "
    "in a **bold** line at the top, then justify below.\n"
    "4. Show every formula, intermediate step, and units. Use Markdown + "
    "LaTeX ($...$ inline, $$...$$ block).\n"
    "5. For each MC distractor, briefly note WHY it is wrong.\n"
    "6. Do NOT call tools or read external files. Do NOT speculate that the "
    "problem is incomplete — work with exactly what is given."
)


def _gather_context(course_dir: Path, topic: str | None, max_chars: int = 40000,
                    query: str | None = None) -> str:
    """Formula sheet (if the course has one) + vector-retrieved chunks.

    `query` overrides the retrieval text (e.g. the student's actual question);
    falls back to the topic key. Retrieval prefers in_class chunks but falls
    back to ALL categories when the course has none (non-stats courses)."""
    parts: list[str] = []
    formulas = course_dir / "modules/final-exam-materials/30440feformulas.pdf"
    if formulas.exists():
        from pypdf import PdfReader
        try:
            ftxt = "\n".join((p.extract_text() or "") for p in PdfReader(str(formulas)).pages)
            parts.append("=== FORMULA SHEET ===\n" + ftxt)
        except Exception:
            pass
    retrieval_text = query or topic
    if retrieval_text and (course_dir / "chroma").exists():
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from vectorize import get_collection
            coll = get_collection(course_dir)
            res = coll.query(
                query_texts=[retrieval_text],
                n_results=6,
                where={"category": "in_class"},
            )
            if not res["documents"][0]:
                res = coll.query(query_texts=[retrieval_text], n_results=6)
            for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
                parts.append(f"=== {meta['source']} p{meta['page']} ===\n{doc}")
        except Exception as e:
            parts.append(f"[context retrieval failed: {e}]")
    return ("\n\n".join(parts))[:max_chars]


FAST_MODEL = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
THINKING_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")


def _solve_via_gemini(prompt: str, thinking: bool = False) -> dict:
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    model_name = THINKING_MODEL if thinking else FAST_MODEL
    try:
        text = generate(prompt, system=SYSTEM_PROMPT,
                        max_output_tokens=16384 if thinking else 8192,
                        model=model_name)
    except Exception as e:
        return {"error": str(e)}
    return {
        "answer": text,
        "model": model_name,
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


def solve(problem_text: str, course_dir: Path, topic: str | None = None,
          thinking: bool = False) -> dict:
    context = _gather_context(course_dir, topic)
    prompt = (
        f"Topic: {topic or 'general'}\n\n"
        f"<problem>\n{problem_text}\n</problem>\n\n"
        f"Reference (formula sheet + selected notes — consult ONLY if a formula "
        f"is needed; do NOT treat this as the question):\n"
        f"<reference>\n{context}\n</reference>\n\n"
        f"Now answer the problem inside <problem>. Remember rules 1-6."
    )
    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "claude":
        full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
        return _solve_via_claude(full_prompt)
    return _solve_via_gemini(prompt, thinking=thinking)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic")
    ap.add_argument("--problem", required=True)
    args = ap.parse_args()
    result = solve(args.problem, Path(args.course_dir), args.topic)
    print(json.dumps(result, indent=2))
