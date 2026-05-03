"""LLM-backed problem solver with prompt caching.

Reads ANTHROPIC_API_KEY from env. Uses Claude Sonnet 4.6 with prompt caching
on the long course-context block.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are an expert statistics tutor for ACMS 30440 at Notre Dame. "
    "Solve the student's problem step by step. Show every formula used, "
    "every intermediate calculation, and the final answer with units. "
    "Cite which course concepts/chapters apply. If the problem is multiple "
    "choice, identify the correct option and explain why each distractor is wrong. "
    "Use Markdown with LaTeX for formulas ($...$ inline, $$...$$ block)."
)


def _gather_context(course_dir: Path, topic: str | None, max_chars: int = 60000) -> str:
    """Pull formula sheet + topic-relevant in_class material as cached context."""
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
            import sys
            sys.path.insert(0, str(Path(__file__).parent))
            from vectorize import get_collection
            coll = get_collection(course_dir)
            res = coll.query(
                query_texts=[topic],
                n_results=8,
                where={"category": "in_class"},
            )
            for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
                parts.append(f"=== {meta['source']} p{meta['page']} ===\n{doc}")
        except Exception as e:
            parts.append(f"[context retrieval failed: {e}]")

    out = "\n\n".join(parts)
    return out[:max_chars]


def solve(problem_text: str, course_dir: Path, topic: str | None = None) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set in environment."}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic SDK not installed."}

    client = Anthropic(api_key=api_key)
    context = _gather_context(course_dir, topic)

    msg = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Course reference material (use as needed):\n\n{context}",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Topic hint: {topic or 'general'}\n\n"
                            f"Problem to solve:\n\n{problem_text}\n\n"
                            "Walk me through it step by step."
                        ),
                    },
                ],
            }
        ],
    )

    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    usage = msg.usage
    return {
        "answer": text,
        "model": MODEL,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0),
        "cache_creation": getattr(usage, "cache_creation_input_tokens", 0),
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
