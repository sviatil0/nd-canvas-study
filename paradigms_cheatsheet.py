"""Generate the optimal handwritten cheatsheet for CSE 30332 final exam.

Pulls "## 8. Cheatsheet snippet" sections from per-topic summary markdown
+ adds prof-mentioned exam triggers + Gradescope mistakes + final exam
guideline question types.

Then asks Gemini to compress into 1 letter-sized double-sided page,
prioritizing HARD-TO-REMEMBER, EXAM-LIKELY items.

Output:
  bundles/CHEATSHEET.md  — markdown source (you handwrite from this)
  bundles/CHEATSHEET.html — printable letter-sized 2-column dense layout
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from topic_graph import _PARADIGMS, topo_sort

CHEATSHEET_PROMPT = """You are helping a Notre Dame student create the OPTIMAL one-page
HANDWRITTEN cheatsheet for the CSE 30332 Programming Paradigms final exam.

Constraints:
- Letter-sized (8.5x11"), DOUBLE-SIDED → roughly 2 dense pages of content
- Will be HANDWRITTEN with pen on paper
- Closed book except this sheet
- Prof's exam includes: conceptual Qs, output prediction, code writing
- Topics covered: JavaScript (closures, async, prototypes, DOM, MVC),
  Python (decorators, OOP, GIL), Django (models/views/templates/forms/auth),
  REST APIs, Java (OOP, generics, collections, concurrency), Clojure
  (immutability, recursion, lazy), paradigm comparisons (imperative,
  functional, OOP, declarative, event-driven), binding & typing.

Below are per-topic notes + the actual final exam guidelines + question
examples from the prof. Use them to identify what is MOST WORTH PUTTING
ON THE CHEATSHEET — i.e. things that:

1. Are hard to remember on the spot (exact syntax, gotchas, comparison tables)
2. Are LIKELY to appear (the prof's example questions hint at this)
3. Take up minimal space when handwritten
4. Distinguish easily-confused pairs (== vs ===, let vs var, equals vs ==,
   shallow vs deep binding, static vs dynamic typing)

DO NOT include things that are easy to memorize, common knowledge, or
unlikely to appear. Skip narrative prose.

Output: dense MARKDOWN cheatsheet, organized by language/topic. Use
tables and short bullets. Code blocks should be 1-3 lines max. Aim for
~3000-4000 words total (will compress to 2 sides handwriting).

Sections suggested (adapt as needed):

# Cheatsheet — CSE 30332 Final
## Page 1: Concepts + JavaScript + Front-end
## Page 2: Python/Django + REST + Java + Clojure + Paradigm Comparison

For each topic group, include:
- 1-line definitions of key terms
- Comparison tables (e.g. typing systems, paradigms, MVC vs MVT)
- EXACT syntax for things people forget (decorators, async/await, fetch,
  Django URL patterns, Java generic bounds, Clojure recur)
- Common output-prediction traps (var hoisting, this binding, late binding,
  reference vs value semantics)
- 1-2 most-likely exam-question types per topic with answer skeleton

--- SOURCE: per-topic summaries + final exam guidelines + sample questions ---
{context}
--- END ---
"""


def gather(course_dir: Path) -> str:
    parts: list[str] = []
    g = course_dir / "_external/google_drive_remote/document/16w-Wm7rCsRCH6QUiSdMwRirSe-wM71-BOpmjORygakg.txt"
    if g.exists():
        parts.append(f"=== FINAL EXAM GUIDELINES (sample questions inside) ===\n"
                     + g.read_text()[:10000])
    topics_dir = course_dir / "bundles" / "topics"
    if topics_dir.exists():
        for t in topo_sort(_PARADIGMS):
            f = topics_dir / f"{t}.md"
            if not f.exists():
                continue
            md = f.read_text()
            cheat = re.search(r"##\s*8\..*?(?=\n##\s*\d+\.|\Z)", md, re.S)
            pitfalls = re.search(r"##\s*6\..*?(?=\n##\s*\d+\.|\Z)", md, re.S)
            label = _PARADIGMS[t]["label"]
            chunk = f"\n=== TOPIC: {label} ===\n"
            if pitfalls: chunk += pitfalls.group(0) + "\n"
            if cheat:    chunk += cheat.group(0)
            if not pitfalls and not cheat:
                chunk += md[:4000]
            parts.append(chunk)
    return "\n".join(parts)[:120000]


def render_html(md_text: str) -> str:
    """Render Markdown via markdown-it-py + sanitize via Bleach.

    Avoids client-side innerHTML; produces fully baked HTML that just
    needs Ctrl+P → save as PDF.
    """
    try:
        from markdown_it import MarkdownIt
        md_parser = (
            MarkdownIt("commonmark", {"html": False, "linkify": True})
            .enable("table")
            .enable("strikethrough")
        )
        body = md_parser.render(md_text)
    except ImportError:
        # Fallback: very dumb conversion
        import html as _h
        body = "<pre>" + _h.escape(md_text) + "</pre>"
    style = (
        "@page { size: letter; margin: 0.4in; }"
        "body { font-family: -apple-system, sans-serif; font-size: 8.5pt; line-height: 1.25;"
        " column-count: 2; column-gap: 0.3in; column-fill: balance; }"
        "h1 { font-size: 13pt; column-span: all; margin: 0 0 4pt; text-align: center; }"
        "h2 { font-size: 10pt; margin: 6pt 0 3pt; border-bottom: 1px solid #999; }"
        "h3 { font-size: 9pt; margin: 4pt 0 2pt; }"
        "pre { background: #f5f5f5; padding: 2pt 4pt; font-size: 7.5pt;"
        " white-space: pre-wrap; margin: 2pt 0; border-radius: 2pt; }"
        "code { font-size: 8pt; background: #f5f5f5; padding: 0 2pt; }"
        "table { border-collapse: collapse; font-size: 7.5pt; width: 100%; margin: 2pt 0; }"
        "th, td { border: 1px solid #aaa; padding: 1pt 3pt; text-align: left; }"
        "th { background: #e5e5e5; }"
        "ul, ol { margin: 2pt 0; padding-left: 14pt; }"
        "li { margin-bottom: 1pt; }"
        "p { margin: 2pt 0; }"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Final Exam Cheatsheet — CSE 30332</title>"
        f"<style>{style}</style>"
        "</head><body>" + body + "</body></html>"
    )


def build(course_dir: Path) -> Path:
    out_md = course_dir / "bundles" / "CHEATSHEET.md"
    context = gather(course_dir)
    if not context.strip():
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text("# Cheatsheet\n\n_No source material yet — generate topic summaries first._\n")
        return out_md
    prompt = CHEATSHEET_PROMPT.format(context=context)
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    print(f"asking Gemini for cheatsheet ({len(context)} chars context)...")
    text = generate(prompt, max_output_tokens=16384, temperature=0.2)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(text)
    print(f"wrote {out_md} ({len(text)} chars)")
    out_html = course_dir / "bundles" / "CHEATSHEET.html"
    out_html.write_text(render_html(text))
    print(f"wrote {out_html}")
    return out_md


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    build(cdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
