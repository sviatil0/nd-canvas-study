"""Gradescope scraper — pulls assignments + per-question scores + rubric.

Auth: cookies from gradescope_auth.py (Playwright SSO). Re-run if expired.
"""
from __future__ import annotations

import html as html_unescape
import json
import re
import time
from pathlib import Path

import requests

GS_BASE = "https://www.gradescope.com"
COOKIE_FILE = Path(__file__).parent / "secrets" / "gradescope_cookies.json"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}


def _jar() -> dict:
    if not COOKIE_FILE.exists():
        raise RuntimeError(
            "No Gradescope cookies. Run: python gradescope_auth.py"
        )
    raw = json.loads(COOKIE_FILE.read_text())
    return {c["name"]: c["value"] for c in raw}


def _get(path: str, allow_redirects: bool = True) -> requests.Response:
    url = path if path.startswith("http") else GS_BASE + path
    r = requests.get(url, cookies=_jar(), headers=HEADERS, timeout=30,
                     allow_redirects=allow_redirects)
    return r


def _react_props(html: str, component: str) -> dict | None:
    """Extract & decode `data-react-props` for a given React component."""
    pat = re.compile(
        r'data-react-class="' + re.escape(component) +
        r'"\s+data-react-props="([^"]+)"'
    )
    m = pat.search(html)
    if not m:
        return None
    return json.loads(html_unescape.unescape(m.group(1)))


def list_courses() -> list[dict]:
    """Return [{id, name}] for current logged-in user."""
    html = _get("/account").text
    out = []
    seen = set()
    for m in re.finditer(
        r'href="/courses/(\d+)"[^>]*>.*?courseBox--shortname"[^>]*title="([^"]+)"',
        html, re.S,
    ):
        cid = int(m.group(1))
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "name": m.group(2).strip()})
    return out


def list_assignments(course_id: int) -> list[dict]:
    """Return [{id, name, score, max_score, status}] for a course."""
    html = _get(f"/courses/{course_id}").text
    out = []
    seen_ids: set[int] = set()
    for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        row = tr_m.group(1)
        am = re.search(r"/assignments/(\d+)", row)
        if not am:
            continue
        aid = int(am.group(1))
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        nm = re.search(r"aria-label=\"View ([^\"]+)\"", row)
        if not nm:
            nm = re.search(r">View ([^<]+)<", row)
        name = nm.group(1).strip() if nm else f"asgn-{aid}"
        sm = re.search(r"submissionStatus--score[^>]*>([^<]+)<", row)
        score_str = sm.group(1).strip() if sm else None
        score = max_score = None
        if score_str and "/" in score_str:
            try:
                a, b = score_str.split("/")
                score = float(a.strip())
                max_score = float(b.strip())
            except ValueError:
                pass
        status_m = re.search(r"submissionStatus--text[^>]*>([^<]+)<", row)
        status = status_m.group(1).strip() if status_m else None
        out.append({
            "id": aid, "name": name,
            "score": score, "max_score": max_score, "status": status,
            "url": f"{GS_BASE}/courses/{course_id}/assignments/{aid}",
        })
    return out


def fetch_submission(course_id: int, assignment_id: int) -> dict | None:
    """Pull graded submission viewer JSON for one assignment.

    Returns full props dict or None if nothing graded yet.
    """
    r = _get(f"/courses/{course_id}/assignments/{assignment_id}",
             allow_redirects=True)
    if r.status_code != 200:
        return None
    if "/submissions/" not in r.url:
        return None
    props = _react_props(r.text, "AssignmentSubmissionViewer")
    if not props:
        return None
    sub_id_m = re.search(r"/submissions/(\d+)", r.url)
    props["__submission_id"] = int(sub_id_m.group(1)) if sub_id_m else None
    props["__assignment_id"] = assignment_id
    props["__course_id"] = course_id
    return props


def wrong_questions(props: dict) -> list[dict]:
    """Extract per-question rows where user's score < max."""
    qs_by_id = {q["id"]: q for q in props.get("questions", [])}
    out = []
    for qs in props.get("question_submissions", []):
        qid = qs.get("question_id")
        q = qs_by_id.get(qid, {})
        sc = qs.get("score")
        wt = q.get("weight")
        if sc is None or wt is None:
            continue
        try:
            sc_f, wt_f = float(sc), float(wt)
        except (TypeError, ValueError):
            continue
        if sc_f >= wt_f:
            continue
        out.append({
            "question_id": qid,
            "title": q.get("title") or q.get("name") or f"Q{qid}",
            "score": sc_f,
            "max_score": wt_f,
            "lost": round(wt_f - sc_f, 3),
            "rubric_items": qs.get("rubric_items", []),
            "comments": qs.get("comments") or "",
            "page_indices": q.get("page_indices") or q.get("crop_rect_list") or [],
            "parent_id": q.get("parent_id"),
        })
    return out


def fetch_pdf(props: dict, save_to: Path) -> bool:
    """Download graded PDF (with rubric annotations) for the submission."""
    paths = props.get("paths", {})
    candidates = [
        paths.get("graded_pdf_path"),
        paths.get("original_file_path"),
    ]
    for url in candidates:
        if not url:
            continue
        if url.startswith("/"):
            url = GS_BASE + url
        try:
            r = requests.get(url, cookies=_jar(), headers=HEADERS,
                             timeout=120, allow_redirects=True)
        except Exception:
            continue
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(r.content)
            return True
    return False


def render_question_crop(pdf_path: Path, question: dict, save_to: Path,
                         dpi: int = 150,
                         student_pages: list[int] | None = None) -> bool:
    """Render a question's PDF region as a JPG.

    Priority:
      1. `student_pages` (from question_submissions[].data.pages) — the
         actual pages the student mapped their answer to. Render those
         FULL pages stacked vertically.
      2. Fallback: question.parameters.crop_rect_list — for exam templates
         where each question has a fixed page region.
    """
    try:
        from pdf2image import convert_from_path
        from PIL import Image
    except ImportError:
        return False

    # Prefer student-mapped pages (HW assignments)
    if student_pages:
        imgs = []
        for pn in student_pages:
            try:
                pages = convert_from_path(str(pdf_path), dpi=dpi,
                                          first_page=pn, last_page=pn)
                if pages:
                    imgs.append(pages[0])
            except Exception:
                continue
        if not imgs:
            return False
        # Stack vertically
        max_w = max(img.width for img in imgs)
        total_h = sum(img.height for img in imgs)
        canvas = Image.new("RGB", (max_w, total_h), "white")
        y = 0
        for img in imgs:
            canvas.paste(img.convert("RGB"), (0, y))
            y += img.height
        save_to.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(str(save_to), "JPEG", quality=85)
        return True

    # Fallback: crop_rect_list (exam templates)
    crop_list = (question.get("parameters") or {}).get("crop_rect_list") or []
    if not crop_list:
        return False
    rect = crop_list[0]
    page_num = int(rect.get("page_number", 1))
    try:
        pages = convert_from_path(str(pdf_path), dpi=dpi,
                                  first_page=page_num, last_page=page_num)
    except Exception:
        return False
    if not pages:
        return False
    img = pages[0]
    w, h = img.size
    left = int(w * float(rect.get("x1", 0)) / 100.0)
    right = int(w * float(rect.get("x2", 100)) / 100.0)
    top = int(h * float(rect.get("y1", 0)) / 100.0)
    bottom = int(h * float(rect.get("y2", 100)) / 100.0)
    if right <= left or bottom <= top:
        return False
    cropped = img.crop((left, top, right, bottom))
    save_to.parent.mkdir(parents=True, exist_ok=True)
    cropped.convert("RGB").save(str(save_to), "JPEG", quality=85)
    return True


def fetch_question_image(props: dict, question_id: int, save_to: Path) -> bool:
    """Render question's region from cached graded PDF."""
    aid = props.get("__assignment_id")
    if aid is None:
        return False
    pdf_path = save_to.parent / f"asgn_{aid}.pdf"
    if not pdf_path.exists():
        if not fetch_pdf(props, pdf_path):
            return False
    qs_by_id = {q["id"]: q for q in props.get("questions", [])}
    q = qs_by_id.get(question_id)
    if not q:
        return False
    # Find student-mapped pages from question_submissions
    student_pages: list[int] | None = None
    for qs in props.get("question_submissions", []):
        if qs.get("question_id") == question_id:
            data = qs.get("data") or {}
            pages = data.get("pages")
            if isinstance(pages, list) and pages:
                student_pages = [int(p) for p in pages if isinstance(p, (int, float))]
            break
    return render_question_crop(pdf_path, q, save_to,
                                student_pages=student_pages)


# Map Gradescope assignment name → official solution PDF in downloads/<cid>/modules/.
def find_solution_pdf(course_dir: Path, asgn_name: str) -> Path | None:
    name = asgn_name.lower().replace(" ", "").replace("/", "")
    candidates = []
    # Exam patterns: E1-S26-V2 → exam-1 ... s26 ... solutions
    import re as _re
    em = _re.search(r"e(\d)[-_]?(s|f|sp|fa)(\d{2})", name)
    if em:
        exam_n = em.group(1)
        sem = em.group(2) + em.group(3)
        for pdf in course_dir.glob(f"modules/exam-{exam_n}-materials/**/*solutions*.pdf"):
            if sem in pdf.name.lower().replace(" ", "").replace("_", ""):
                candidates.append(pdf)
        for pdf in course_dir.glob(f"modules/exam-{exam_n}-materials/**/*{sem}*solutions*.pdf"):
            candidates.append(pdf)
    # HW pattern: HW1, HW5/6 → acms-30440-hw1-s26-key.pdf or hw5-6-...
    hm = _re.search(r"hw(\d+)", name)
    if hm:
        n = hm.group(1)
        for pdf in course_dir.glob(f"modules/**/acms-30440-hw{n}-*key.pdf"):
            candidates.append(pdf)
        for pdf in course_dir.glob(f"modules/**/*hw{n}-*-key.pdf"):
            candidates.append(pdf)
        # Combined HW (HW5/6 → hw5-6-)
        hm2 = _re.search(r"hw(\d+)/(\d+)", asgn_name.lower())
        if hm2:
            for pdf in course_dir.glob(f"modules/**/*hw{hm2.group(1)}-{hm2.group(2)}-*key.pdf"):
                candidates.append(pdf)
    # Quiz pattern: Quiz 1 → spring-2026-quizzes folder
    qm = _re.search(r"quiz(\d+)|q(\d+)", name)
    if qm:
        qn = qm.group(1) or qm.group(2)
        for pdf in course_dir.glob(f"modules/**/quiz{qn}*solutions*.pdf"):
            candidates.append(pdf)
        for pdf in course_dir.glob(f"modules/**/q{qn}*key*.pdf"):
            candidates.append(pdf)
    if not candidates:
        return None
    # Prefer shortest path (less nested = more authoritative)
    candidates.sort(key=lambda p: (len(str(p)), str(p)))
    return candidates[0]


def render_solution_for_question(course_dir: Path, asgn_name: str,
                                 question_title: str, save_to: Path) -> bool:
    """Find official solution PDF, OCR-locate the question, render page as JPG.

    Falls back to whole-page render of the page where the question label first
    appears in the OCR transcript.
    """
    sol_pdf = find_solution_pdf(course_dir, asgn_name)
    if not sol_pdf or not sol_pdf.exists():
        return False
    # Look up OCR text of solution PDF
    rel = str(sol_pdf.relative_to(course_dir))
    ocr_file = course_dir / "_ocr" / (rel.replace("/", "__") + ".txt")
    if not ocr_file.exists():
        return False
    ocr_text = ocr_file.read_text()
    # Split into pages
    page_chunks = ocr_text.split("--- page ")
    # Find which page mentions the question title (e.g. "12.", "MC1", "P2D")
    import re as _re
    title = question_title.strip()
    # Generate matchers: numbered ("12."), MC label ("MC1"), part label ("P1A").
    # For exams, MC1..MC20 likely mapped to "1.", "2.", ...
    matchers = [_re.escape(title)]
    mc_m = _re.match(r"MC(\d+)", title, _re.I)
    if mc_m:
        matchers.append(rf"\b{mc_m.group(1)}\.")
    # Free-response: P1A, P1B, P3a, P3b, P1, P2 etc.
    pm = _re.match(r"P(\d+)([A-Za-z]?)(?:_.+)?", title)
    if pm:
        prob_num = pm.group(1)
        part = pm.group(2).lower() if pm.group(2) else ""
        matchers.append(rf"\bProblem\s+{prob_num}\b")
        matchers.append(rf"(?:^|\n)\s*{prob_num}\.\s*\(\d+\s*p")
        matchers.append(rf"(?:^|\n)\s*{prob_num}\.\s+[A-Z(]")
        if part:
            matchers.append(rf"\({part}\)")
    matched_page = None
    for chunk in page_chunks:
        # First line of chunk is "N ---\n..." for pages 2+
        page_match = _re.match(r"^(\d+)\s*---", chunk)
        page_num = int(page_match.group(1)) if page_match else 1
        body = chunk[page_match.end():] if page_match else chunk
        for pat in matchers:
            if _re.search(pat, body):
                matched_page = page_num
                break
        if matched_page:
            break
    if not matched_page:
        return False
    try:
        from pdf2image import convert_from_path
    except ImportError:
        return False
    try:
        pages = convert_from_path(str(sol_pdf), dpi=150,
                                  first_page=matched_page, last_page=matched_page)
    except Exception:
        return False
    if not pages:
        return False
    save_to.parent.mkdir(parents=True, exist_ok=True)
    pages[0].convert("RGB").save(str(save_to), "JPEG", quality=85)
    return True


def pull_course(course_id: int, course_dir: Path,
                progress_cb=None) -> dict:
    """Pull everything for a course → save to course_dir/_gradescope/.

    Returns summary {assignments: [...], wrong_count: int}.
    """
    out_dir = course_dir / "_gradescope"
    out_dir.mkdir(parents=True, exist_ok=True)

    assignments = list_assignments(course_id)
    summary = {"course_id": course_id, "assignments": [], "wrong_total": 0}
    for i, a in enumerate(assignments):
        if progress_cb:
            progress_cb(i + 1, len(assignments), a["name"])
        props = fetch_submission(course_id, a["id"])
        if not props:
            a["wrong"] = []
            summary["assignments"].append(a)
            continue
        wrongs = wrong_questions(props)
        a["wrong"] = wrongs
        a["submission_id"] = props.get("__submission_id")
        # Save the full props JSON for offline reference
        (out_dir / f"asgn_{a['id']}.json").write_text(
            json.dumps(props, indent=2)
        )
        # Try to grab PDF
        pdf_path = out_dir / f"asgn_{a['id']}.pdf"
        if not pdf_path.exists():
            try:
                fetch_pdf(props, pdf_path)
            except Exception:
                pass
        # Per-question images for wrong ones
        for w in wrongs:
            img_path = out_dir / f"asgn_{a['id']}_q{w['question_id']}.jpg"
            if img_path.exists():
                continue
            try:
                fetch_question_image(props, w["question_id"], img_path)
            except Exception:
                pass
        summary["wrong_total"] += len(wrongs)
        summary["assignments"].append(a)
        time.sleep(0.5)  # be polite

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
