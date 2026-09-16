"""Homework sets: discovery, question parsing, attachments, saved state.

Canvas is the index (what is assigned, what it is worth, when it is due) but
often not the content — plenty of courses put the actual questions in a GitHub
repo or a PDF and leave the Canvas description empty. So a set is assembled
from whichever source actually has text, with an explicit local override as the
last resort the student controls.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# Names that mean "graded work you hand in", as opposed to exams/study sheets.
HW_NAME_RE = re.compile(
    r"\b(home\s*work|homework|hw\s*\d|problem\s+set|pset|lab\b|daily|dailies|assignment)\b",
    re.I,
)
EXCLUDE_NAME_RE = re.compile(r"\b(exam|study sheet|graded result|survey)\b", re.I)

# "1. ", "2) ", markdown-escaped "8\. "
Q_LINE_RE = re.compile(r"^[ \t]{0,3}(\d{1,2})[ \t]*\\?[.)][ \t]+(?=\S)", re.M)
POINTS_RE = re.compile(r"\[\s*([\d.]+)\s*(?:pt|pts|point|points)\.?\s*\]", re.I)
ATTACH_RE = re.compile(r"[\w\-]+\.(?:pcap|pcapng|csv|txt|zip|tar\.gz|py|c|h|json|xlsx|pdf)\b", re.I)

SOURCES_FILE = "homework_sources.json"


# ---------------------------------------------------------------- text extraction

def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_to_text(path: Path) -> str:
    from pypdf import PdfReader
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages).strip()
    except Exception:
        return ""


def read_any(path: Path) -> str:
    """Best-effort text out of md / html / pdf / plain."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_to_text(path)
    try:
        raw = path.read_text(errors="ignore")
    except OSError:
        return ""
    if suffix in (".html", ".htm"):
        return html_to_text(raw)
    return raw.strip()


# ---------------------------------------------------------------- discovery

def _tokens_for(name: str) -> list[str]:
    """Filename fragments that would plausibly name this assignment's source."""
    m = re.search(r"(\d{1,2})", name)
    if not m:
        return [re.sub(r"[^a-z0-9]+", "", name.lower())]
    n = int(m.group(1))
    stem = "hw" if re.search(r"home\s*work|hw", name, re.I) else \
           "lab" if re.search(r"lab", name, re.I) else "assignment"
    return [f"{stem}{n:02d}", f"{stem}{n}", f"{stem}_{n:02d}", f"{stem}_{n}",
            f"homework{n:02d}", f"homework{n}", f"homework-{n}"]


def _load_sources(cdir: Path) -> dict:
    f = cdir / "bundles" / SOURCES_FILE
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def save_source(cdir: Path, key: str, path: str) -> dict:
    """Point a homework at a local file or directory the student supplies."""
    sources = _load_sources(cdir)
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(str(p))
    sources[key] = {"path": str(p.resolve()), "saved": int(time.time())}
    out = cdir / "bundles" / SOURCES_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sources, indent=1))
    return sources[key]


def _resolve_source(cdir: Path, assignment: dict, key: str, sources: dict) -> dict:
    """Where the question text actually lives. Returns {origin, path, text}."""
    tokens = _tokens_for(assignment.get("name", ""))

    override = sources.get(key, {}).get("path")
    if override:
        p = Path(override)
        if p.is_dir():
            cands = sorted(
                [f for f in p.rglob("*")
                 if f.is_file() and f.suffix.lower() in (".md", ".html", ".htm", ".pdf", ".txt")
                 and any(t in f.name.lower() for t in tokens)],
                key=lambda f: (f.suffix.lower() != ".md", len(f.name)),
            )
            if cands:
                return {"origin": "local override", "path": str(cands[0]),
                        "text": read_any(cands[0])}
        elif p.is_file():
            return {"origin": "local override", "path": str(p), "text": read_any(p)}

    slug = re.sub(r"[^a-z0-9]+", "-", assignment.get("name", "").lower()).strip("-")
    canvas_html = cdir / "assignments" / f"{assignment.get('id')}_{slug}.html"
    if canvas_html.exists() and canvas_html.stat().st_size > 200:
        return {"origin": "canvas page", "path": str(canvas_html),
                "text": read_any(canvas_html)}

    ext = cdir / "_external"
    if ext.exists():
        hits = [f for f in ext.rglob("*.html")
                if any(t in f.name.lower().replace("-", "_") for t in tokens)]
        if hits:
            best = max(hits, key=lambda f: f.stat().st_size)
            return {"origin": "external scrape", "path": str(best), "text": read_any(best)}

    manifest = cdir / "bundles" / "manifest.json"
    if manifest.exists():
        try:
            rows = json.loads(manifest.read_text())
        except Exception:
            rows = []
        for row in rows:
            rel = row.get("path", "")
            if row.get("category") in ("homeworks", "in_class") and \
                    any(t in Path(rel).name.lower() for t in tokens):
                p = cdir / rel
                if p.exists():
                    return {"origin": "course file", "path": str(p), "text": read_any(p)}

    desc = assignment.get("description") or ""
    if desc.strip():
        return {"origin": "canvas description", "path": "", "text": html_to_text(desc)}

    return {"origin": "", "path": "", "text": ""}


def find_attachments(cdir: Path, assignment: dict, source: dict) -> list[dict]:
    """Files this homework operates on: named in the text, or matching its token."""
    tokens = _tokens_for(assignment.get("name", ""))
    named = {m.group(0).lower() for m in ATTACH_RE.finditer(source.get("text", ""))}
    roots = [cdir / "files", cdir / "assignments" / "_attachments", cdir / "modules"]
    src_path = source.get("path")
    if src_path:
        roots.append(Path(src_path).parent)

    seen: dict[str, dict] = {}
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() in (".md", ".html", ".htm"):
                continue
            low = f.name.lower()
            if low in named or any(t in low for t in tokens):
                seen.setdefault(str(f.resolve()), {
                    "name": f.name,
                    "path": str(f.resolve()),
                    "size": f.stat().st_size,
                })
    return sorted(seen.values(), key=lambda a: a["name"])


# ---------------------------------------------------------------- parsing

def parse_questions(text: str) -> list[dict]:
    """Split prose into numbered questions.

    Only a run that starts at 1 and steps by 1 counts, so a stray "2." inside a
    paragraph cannot silently shard the homework.
    """
    if not text.strip():
        return []
    marks = [(m.start(), int(m.group(1))) for m in Q_LINE_RE.finditer(text)]
    if not marks:
        return []

    best: list[tuple[int, int]] = []
    for i, (_, num) in enumerate(marks):
        if num != 1:
            continue
        run = [marks[i]]
        expect = 2
        for pos, n in marks[i + 1:]:
            if n == expect:
                run.append((pos, n))
                expect += 1
        if len(run) > len(best):
            best = run
    if len(best) < 2:
        return []

    questions = []
    for idx, (start, num) in enumerate(best):
        end = best[idx + 1][0] if idx + 1 < len(best) else len(text)
        body = text[start:end].strip()
        body = re.sub(r"^[ \t]{0,3}\d{1,2}[ \t]*\\?[.)][ \t]+", "", body, count=1)
        pts = POINTS_RE.search(body)
        questions.append({
            "num": str(num),
            "points": float(pts.group(1)) if pts else None,
            "text": body.strip(),
        })
    return questions


# ---------------------------------------------------------------- public API

def list_sets(cdir: Path) -> list[dict]:
    f = cdir / "assignments.json"
    if not f.exists():
        return []
    try:
        rows = json.loads(f.read_text())
    except Exception:
        return []
    sources = _load_sources(cdir)
    state = load_state(cdir)

    out = []
    for a in rows:
        name = a.get("name") or ""
        if EXCLUDE_NAME_RE.search(name) or not HW_NAME_RE.search(name):
            continue
        key = str(a.get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()))
        saved = state.get(key, {}).get("questions", {})
        out.append({
            "key": key,
            "name": name,
            "points": a.get("points_possible"),
            "due": a.get("due_at"),
            "url": a.get("html_url"),
            "has_source": bool(sources.get(key)) or bool(a.get("description")),
            "answered": sum(1 for q in saved.values() if q.get("done")),
            "saved_count": len(saved),
        })
    return out


def load_set(cdir: Path, key: str) -> dict:
    f = cdir / "assignments.json"
    rows = json.loads(f.read_text()) if f.exists() else []
    assignment = next(
        (a for a in rows
         if str(a.get("id")) == key
         or re.sub(r"[^a-z0-9]+", "-", (a.get("name") or "").lower()) == key),
        None,
    )
    if assignment is None:
        raise KeyError(key)

    sources = _load_sources(cdir)
    source = _resolve_source(cdir, assignment, key, sources)
    questions = parse_questions(source["text"])
    state = load_state(cdir).get(key, {}).get("questions", {})
    for q in questions:
        saved = state.get(q["num"], {})
        q["draft"] = saved.get("draft", "")
        q["answer"] = saved.get("answer", "")
        q["feedback"] = saved.get("feedback", "")
        q["tool_output"] = saved.get("tool_output", "")
        q["citations"] = saved.get("citations", [])
        q["done"] = bool(saved.get("done"))

    return {
        "key": key,
        "name": assignment.get("name"),
        "points": assignment.get("points_possible"),
        "due": assignment.get("due_at"),
        "url": assignment.get("html_url"),
        "source": source,
        "source_override": sources.get(key, {}).get("path", ""),
        "questions": questions,
        "attachments": find_attachments(cdir, assignment, source),
        "parsed_points": sum(q["points"] or 0 for q in questions),
        "done_count": sum(1 for q in questions if q["done"]),
    }


# ---------------------------------------------------------------- state
# Lives inside bundles/progress.json under "homework" so it travels with the
# rest of the per-course progress the UI already persists.

def _progress_file(cdir: Path) -> Path:
    return cdir / "bundles" / "progress.json"


def _read_progress(cdir: Path) -> dict:
    f = _progress_file(cdir)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def load_state(cdir: Path) -> dict:
    return _read_progress(cdir).get("homework", {})


def save_question(cdir: Path, key: str, num: str, patch: dict) -> dict:
    progress = _read_progress(cdir)
    hw = progress.setdefault("homework", {})
    entry = hw.setdefault(key, {"questions": {}})
    q = entry.setdefault("questions", {}).setdefault(num, {})
    q.update(patch)
    q["updated"] = int(time.time())
    entry["updated"] = q["updated"]
    f = _progress_file(cdir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(progress, indent=2))
    return q


def export_markdown(cdir: Path, key: str) -> str:
    hw = load_set(cdir, key)
    lines = [f"# {hw['name']}", ""]
    if hw.get("url"):
        lines += [f"Submit: {hw['url']}", ""]
    for q in hw["questions"]:
        pts = f" [{q['points']:g} pt]" if q["points"] else ""
        lines.append(f"## Q{q['num']}{pts}")
        lines.append("")
        body = (q["draft"] or q["answer"] or "_(unanswered)_").strip()
        lines += [body, ""]
    return "\n".join(lines)
