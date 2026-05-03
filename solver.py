"""LLM problem solver via local Claude Code CLI (no API billing).

Uses `claude -p` headless mode: pipes problem + course context to the local
claude binary, captures stdout. Inherits user's Claude Code subscription.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

CLAUDE_BIN = shutil.which("claude") or "claude"
TIMEOUT_SEC = 180

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


def solve(problem_text: str, course_dir: Path, topic: str | None = None) -> dict:
    if not shutil.which("claude"):
        return {"error": "`claude` CLI not on PATH. Install Claude Code."}

    context = _gather_context(course_dir, topic)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Topic hint: {topic or 'general'}\n\n"
        f"--- COURSE REFERENCE (for your reference, do not echo) ---\n"
        f"{context}\n"
        f"--- END REFERENCE ---\n\n"
        f"Problem to solve:\n\n{problem_text}\n\n"
        f"Walk me through it step by step."
    )

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"claude CLI timed out after {TIMEOUT_SEC}s"}
    except Exception as e:
        return {"error": f"claude CLI failed: {e}"}

    if proc.returncode != 0:
        return {"error": f"claude exit {proc.returncode}: {proc.stderr[:500]}"}

    return {
        "answer": proc.stdout.strip(),
        "model": "claude-code-cli (local subscription)",
        "input_tokens": len(prompt) // 4,
        "output_tokens": len(proc.stdout) // 4,
        "cache_read": 0,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--topic")
    ap.add_argument("--problem", required=True)
    args = ap.parse_args()
    result = solve(args.problem, Path(args.course_dir), args.topic)
    print(json.dumps(result, indent=2))
