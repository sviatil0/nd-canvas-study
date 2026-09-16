"""Generate optimal handwritten cheatsheet for CSE 30321 Computer Architecture final exam.

Pulls per-topic summary cheat sections + final exam review slide deck OCR
+ Gradescope mistakes. Asks Gemini to compress to dense letter-sized double-sided.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from topic_graph import _COMP_ARCH, topo_sort

CHEATSHEET_PROMPT = """You are creating the OPTIMAL printed cheatsheet for the CSE 30321
Computer Architecture FINAL EXAM at Notre Dame (Thursday 5/7 1:45-3:45pm).

Constraints:
- HARD LIMIT: EXACTLY 2 PAGES total when printed at 7.6pt 3-column letter.
  Page 1 + page 2 must BOTH be densely filled — no half-empty page 2.
- Will be PRINTED via Chrome → PDF in 3-column letter layout
- Same RISC-V reference pages provided as on midterms — DO NOT include
  RISC-V opcode hex / register ABI table (already in reference pages)
- Closed book/notes except this sheet
- Cumulative; weighted toward POST-EXAM 2 (~15 Qs) > Exam 1 (~8) ≈ Exam 2 (~8)
- ~30-35 questions total, multiple choice + short answer + fill-blank

TARGET LENGTH: AT LEAST 60,000 characters of MARKDOWN (this packs ≈2 pages
at 8.2pt 3-col letter). Anything under 50,000 chars = SHORT. Pack worked
examples + tables + transition tables + mnemonics + cheatcard rules until
you hit the limit. The PDF target is 2 letter-size pages, BOTH densely
filled — no half-empty page 2.

If you reach the natural end of topics before hitting 60k chars, ADD:
- More worked numeric examples (different parameter sets per topic)
- Common confusable pairs w/ side-by-side comparison
- Step-by-step recipes ("To solve X: 1) ... 2) ...")
- More of HW09/HW10/HW06/HW08 worked traces (these dominate the final)
- Quick-recall flashcard pairs (Q on left col / A on right)

PROFESSOR EMPHASIZED (from final exam review deck):
- Dynamic Scheduling / OOO: register renaming, WAR + WAW hazards, ROB, free
  list, register map table, pipe-trace simulations (FDIEWC stages)
- Cache Coherence: MESI states + transitions; ASSUME write-invalidate,
  snooping, centralized shared memory
- Branch Prediction: 1-bit AND 2-bit predictor FSMs (transitions)
- AMAT, cache addressing, virtual memory translation (VPN/PFN/offset, TLB)
- Pipelining: 5-stage in-order, control hazards, data hazards, forwarding
- Cache-aware programming
- NOT on exam: albaCore

OUTPUT FORMAT — write dense MARKDOWN with these enrichments:

1. **LaTeX math**: use `$...$` inline and `$$...$$` block (KaTeX renders).
   Examples: $AMAT = HT + MR \\cdot MP$, $$CPI = \\sum F_i \\cdot CPI_i$$

2. **Mermaid diagrams** in fenced ```mermaid blocks for state machines:
   - 2-bit branch predictor FSM (4 states 00/01/10/11 with T/NT transitions)
   - MESI state diagram (M/E/S/I with read/write/snoop transitions)
   - Tomasulo pipeline data flow

3. **Tables** for: 3 C's miss types, write policies, MESI transitions,
   forwarding paths, hazard types (RAW/WAR/WAW), cache org comparison,
   pipe-trace skeletons (F D I E W C).

4. **ASCII diagrams** for: 5-stage pipeline w/ forwarding paths, cache
   address split, virtual address translation flow.

5. **Worked numeric examples mirroring HW SOLUTIONS** (1-2 lines each):
   The HW solutions below show EXACTLY how the prof works problems.
   Extract a compact worked example for EACH HW topic, matching prof's notation/steps:
   - HW02 Performance: CPI / Amdahl / MIPS calc style
   - HW03/HW04 RISC-V: procedure/stack frame example
   - HW05 Control Hazards: pipe trace w/ branch resolution + flush
   - HW06 Caches: tag/index/offset split + miss-rate calc
   - HW07 Cache-Aware: loop nest + reuse / blocking analysis
   - HW08 Virtual Memory: PT walk + TLB hit/miss timing + page table size
   - HW09 Dynamic Scheduling: OOO pipe trace w/ rename, ROB, WAW resolved
   - HW10 Cache Coherence: MESI sequence trace
   PRIORITIZE worked-example density on the OOO/MESI/scheduling topics —
   these are the post-Exam-2 weighted (15 of 30 questions).

6. **Memorization shortcuts / mnemonics** for confusable pairs:
   - WAR vs WAW (renaming fixes both; RAW needs forwarding)
   - Write-through vs write-back trade-offs
   - 1-bit vs 2-bit predictor behavior on alternating branches
   - Direct-mapped vs N-way set-associative when N×size_per_set differs

7. **Common exam gotchas + things that LOSE points easily**:
   - Amdahl's $F_{{enh}}$ is fraction of TIME not instruction count
   - Branch penalty = (resolve stage) − 1
   - Load-use stall vs sw exception (MEM→MEM forward)
   - PT entry holds PFN not VPN
   - MESI: M-evict requires writeback; E→M is silent (no bus)
   - OOO: physical reg freed only when SUBSEQUENT same-arch-reg-writer commits

WRITE FOR HIGH DENSITY — short bullets, no fluff prose, one-line definitions
preferred over paragraphs. Aim for ~9000-11000 words of dense reference material
(prints ≈2 pages at 7.6pt 3-column, both pages packed).

REQUIRED COVERAGE (don't skip — these are weighted on the exam):
- Full MESI transition table (every state × every event)
- Full Tomasulo+ROB worked trace (≥6 instructions, show RS, RAT, ROB, free list per cycle)
- 2-bit predictor FSM mermaid + transition table
- Cache addressing worked example: split address bits given (cache size, block size, associativity)
- AMAT worked w/ multi-level cache: $AMAT = HT_{{L1}} + MR_{{L1}}(HT_{{L2}} + MR_{{L2}} \\cdot MP)$
- VM walk: 32-bit addr, 4KB page, 1-level + 2-level PT example, page table size calc
- Loop blocking / cache-aware: matmul tiled vs untiled miss-rate
- Forwarding paths table: source stage → dest stage → mux ID
- Pipe trace w/ data hazard + stall + forwarding
- Pipe trace w/ control hazard + branch resolved at EX (penalty calc)
- Amdahl numeric: $S = 1/((1-F) + F/k)$ w/ values
- CPI calc: weighted by frequency
- Common gotcha cheatcard (≥10 items)

Suggested top-level structure (adapt freely):

# CSE 30321 Final Cheatsheet
## I. Performance + Amdahl
## II. Pipelining (5-stage in-order)
## III. Data Hazards + Forwarding
## IV. Control Hazards + Branch Prediction (1-bit + 2-bit FSM)
## V. Caches: Org + Performance (AMAT, 3 C's, write policies)
## VI. Cache-Aware Programming (loop nest, blocking, locality)
## VII. Virtual Memory + TLB
## VIII. Cache Coherence — MESI (FSM diagram + transition table)
## IX. Scheduling Intro + Dynamic Scheduling / OOO (Tomasulo, ROB, renaming, pipe trace)
       — INCLUDE A FULL HW09-STYLE WORKED PIPE TRACE (4-6 instrs) showing:
         renaming, RAT updates, ROB entries, WAW resolution, free-list returns
## X. Parallelism (ILP/DLP/TLP) + ISA basics highlights

USE THE FULL OUTPUT LENGTH. Density wins.

--- SOURCE MATERIAL: final exam review slides + per-topic summaries ---
{context}
--- END ---
"""


def gather(course_dir: Path) -> str:
    parts: list[str] = []

    # Final exam review slide OCR (high signal!)
    review_ocr = course_dir / "_ocr" / "_external__google_drive_local__slides__CSE 30321 SP26 Computer Architecture - 29 - Final Exam Review (marked).pdf.txt"
    if review_ocr.exists():
        parts.append(f"=== FINAL EXAM REVIEW SLIDES (HIGH PRIORITY) ===\n"
                     + review_ocr.read_text(errors="ignore")[:15000])

    # Midterm 1 + 2 OCR (real exam questions show prof's style + likely topic re-emphasis)
    for ex_name in ("midterm-1-graded.pdf.txt", "midterm-2-graded.pdf.txt"):
        ex_f = course_dir / "_ocr" / f"_external__google_drive_local__exams__{ex_name}"
        if ex_f.exists():
            parts.append(f"=== {ex_name.upper()} (real exam questions) ===\n"
                         + ex_f.read_text(errors="ignore")[:8000])

    # All HW solutions (real worked problems — likely to appear on final in similar form)
    hw_dir = course_dir / "_external" / "google_drive_local" / "hw"
    if hw_dir.exists():
        import glob
        for hw_f in sorted(hw_dir.glob("HW*SOLUTION*.txt")):
            text = hw_f.read_text(errors="ignore")[:4000]
            parts.append(f"=== {hw_f.stem} ===\n{text}")
        # HW10 has no official SOLUTION yet (only FILLED) — include it
        for hw_f in sorted(hw_dir.glob("HW*FILLED*.txt")):
            text = hw_f.read_text(errors="ignore")[:4000]
            parts.append(f"=== {hw_f.stem} (student-filled) ===\n{text}")

    # Per-topic summaries: pull cheatsheet section + pitfalls
    topics_dir = course_dir / "bundles" / "topics"
    if topics_dir.exists():
        for t in topo_sort(_COMP_ARCH):
            f = topics_dir / f"{t}.md"
            if not f.exists():
                continue
            md = f.read_text()
            cheat = re.search(r"##\s*8\..*?(?=\n##\s*\d+\.|\Z)", md, re.S)
            pitfalls = re.search(r"##\s*6\..*?(?=\n##\s*\d+\.|\Z)", md, re.S)
            label = _COMP_ARCH[t]["label"]
            chunk = f"\n=== TOPIC: {label} ===\n"
            if pitfalls: chunk += pitfalls.group(0) + "\n"
            if cheat:    chunk += cheat.group(0)
            if not pitfalls and not cheat:
                chunk += md[:4000]
            parts.append(chunk)
    return "\n".join(parts)[:200000]


def render_html(md_text: str) -> str:
    """Render markdown w/ KaTeX (math), Mermaid (FSMs), 3-col print CSS."""
    try:
        from markdown_it import MarkdownIt
        md_parser = (
            MarkdownIt("commonmark", {"html": True, "linkify": True, "breaks": True})
            .enable("table")
            .enable("strikethrough")
        )
        body = md_parser.render(md_text)
    except ImportError:
        import html as _h
        body = "<pre>" + _h.escape(md_text) + "</pre>"

    # Convert ```mermaid fences → <div class="mermaid">. markdown_it_py wraps
    # them in <pre><code class="language-mermaid">…</code></pre> by default.
    import re as _re, html as _html
    def _mer_sub(m):
        code = m.group(1)
        # markdown_it already escaped; unescape for mermaid
        code = _html.unescape(code)
        return f'<div class="mermaid">{code}</div>'
    body = _re.sub(
        r'<pre><code class="language-mermaid">([\s\S]*?)</code></pre>',
        _mer_sub, body)

    # Convert wide tables (>=7 cols) to compact bullet lists — they don't fit
    # in 3-column letter at any reasonable font size.
    def _flatten_wide_table(m):
        table_html = m.group(0)
        rows = _re.findall(r"<tr>([\s\S]*?)</tr>", table_html)
        if not rows:
            return table_html
        # Count cols in header row
        header_cells = _re.findall(r"<th[^>]*>([\s\S]*?)</th>", rows[0])
        if len(header_cells) < 7:
            return table_html
        # Strip HTML tags from cells
        def _strip(s): return _re.sub(r"<[^>]+>", "", s).strip()
        headers = [_strip(h) for h in header_cells]
        out = ['<ul class="flat-table">']
        for r in rows[1:]:
            cells = _re.findall(r"<td[^>]*>([\s\S]*?)</td>", r)
            if not cells:
                continue
            cells = [_strip(c) for c in cells]
            # First cell as label, rest as key=value
            label = cells[0]
            kv = " · ".join(f"<b>{h}</b>={c}" for h, c in zip(headers[1:], cells[1:]) if c and c != "-")
            out.append(f"<li><b>{label}.</b> {kv}</li>")
        out.append("</ul>")
        return "\n".join(out)
    body = _re.sub(r"<table>[\s\S]*?</table>", _flatten_wide_table, body)

    style = """
@page { size: letter; margin: 0.18in; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 7.6pt; line-height: 1.22;
  column-count: 3; column-gap: 0.08in; column-fill: balance;
  height: 26in;
  margin: 0;
  orphans: 2; widows: 2;
}
h1 { display: none; }
h2 {
  font-size: 7.8pt; margin: 2pt 0 1pt; padding: 0.5pt 3pt;
  background: #222; color: #fff; border-radius: 2pt;
  break-after: avoid;
}
h3 {
  font-size: 7pt; margin: 1pt 0 0.5pt; color: #b00;
  font-weight: 700; break-after: avoid;
}
pre {
  background: #f3f3f3; padding: 1pt 3pt; font-size: 6.2pt;
  white-space: pre; overflow-x: hidden;
  margin: 2pt 0; border-radius: 2pt;
  border-left: 2px solid #888; break-inside: avoid;
  line-height: 1.05;
}
code { font-size: 7.4pt; background: #f3f3f3; padding: 0 2pt;
       font-family: "SF Mono", Menlo, Consolas, monospace; }
pre code { background: transparent; padding: 0; }
table {
  border-collapse: collapse; font-size: 6pt; width: 100%; margin: 2pt 0;
  table-layout: fixed; max-width: 100%;
  break-inside: avoid;
}
th, td { border: 1px solid #888; padding: 1pt 2pt; text-align: left;
         vertical-align: top; overflow-wrap: break-word; word-break: normal;
         hyphens: auto; }
th { background: #e0e0e0; font-weight: 700; }
td code { font-size: 5.8pt; word-break: break-all; }
ul.flat-table { font-size: 6.4pt; padding-left: 8pt; list-style: square; margin: 2pt 0; }
ul.flat-table li { margin-bottom: 1pt; }
ul.flat-table b { color: #444; font-weight: 600; }
ul, ol { margin: 1pt 0; padding-left: 12pt; }
li { margin-bottom: 0.5pt; break-inside: avoid; }
p { margin: 1pt 0; }
strong { color: #000; }
em { color: #555; }
hr { border: 0; border-top: 1px dashed #aaa; margin: 3pt 0; }
.pagebreak { break-after: page; column-span: all; height: 0; margin: 0; padding: 0; }
.mermaid { background: #fafafa; padding: 2pt; margin: 2pt 0;
           text-align: center; break-inside: avoid; max-width: 100%;
           overflow: hidden; }
.mermaid svg { max-width: 100% !important; height: auto !important;
               width: 100% !important; max-height: 2.2in !important; }
.katex { font-size: 0.95em !important; }
.katex-display { margin: 2pt 0 !important; padding: 0 !important; }
"""

    katex_links = (
        '<link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">'
        '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>'
        '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" '
        'onload="renderMathInElement(document.body, {delimiters: ['
        '{left: \'$$\', right: \'$$\', display: true},'
        '{left: \'$\', right: \'$\', display: false}'
        ']});"></script>'
    )
    mermaid_script = (
        '<script type="module">'
        'import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";'
        'mermaid.initialize({ startOnLoad: true, theme: "neutral", '
        'themeVariables: { fontSize: "10px" }, '
        'flowchart: { htmlLabels: false, curve: "linear" }, '
        'stateDiagram: { fontSize: 10 } });'
        'window.addEventListener("load", () => mermaid.run());'
        '</script>'
    )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Final Exam Cheatsheet — CSE 30321</title>"
        f"{katex_links}"
        f"<style>{style}</style>"
        "</head><body>" + body
        + f"{mermaid_script}"
        + "</body></html>"
    )


def export_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Print HTML → PDF via Playwright headless Chrome (waits for KaTeX/Mermaid)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — skipping PDF export")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"file://{html_path.resolve()}", wait_until="networkidle")
            # Wait for KaTeX auto-render + Mermaid SVGs
            page.wait_for_timeout(5000)
            try:
                page.wait_for_function("typeof renderMathInElement === 'function' && document.querySelectorAll('.katex').length > 0", timeout=5000)
            except Exception:
                pass
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                margin={"top": "0.18in", "bottom": "0.18in",
                        "left": "0.18in", "right": "0.18in"},
                print_background=True,
                prefer_css_page_size=True,
            )
            browser.close()
        return True
    except Exception as e:
        print(f"PDF export failed: {e}")
        return False


def build(course_dir: Path) -> Path:
    out_md = course_dir / "bundles" / "CHEATSHEET.md"
    context = gather(course_dir)
    if not context.strip():
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text("# Cheatsheet\n\n_No source material — generate topic summaries first._\n")
        return out_md
    prompt = CHEATSHEET_PROMPT.format(context=context)
    sys.path.insert(0, str(Path(__file__).parent))
    from gemini_client import generate
    print(f"asking Gemini for cheatsheet ({len(context)} chars context)...")
    text = generate(prompt, max_output_tokens=65536, temperature=0.2)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(text)
    print(f"wrote {out_md} ({len(text)} chars)")
    out_html = course_dir / "bundles" / "CHEATSHEET.html"
    out_html.write_text(render_html(text))
    print(f"wrote {out_html}")
    out_pdf = course_dir / "bundles" / "CHEATSHEET.pdf"
    if export_pdf(out_html, out_pdf):
        print(f"wrote {out_pdf}")
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
