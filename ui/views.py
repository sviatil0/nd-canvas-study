from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

ROOT = Path(settings.BASE_DIR)
DOWNLOADS = ROOT / "downloads"
COOKIE_FILE = ROOT / "cookies.json"
PYTHON = sys.executable


def _run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout + "\n" + p.stderr)[-4000:]


def _course_dirs() -> list[dict]:
    if not DOWNLOADS.exists():
        return []
    out = []
    for d in sorted(DOWNLOADS.iterdir()):
        if not d.is_dir():
            continue
        cid_part, _, name = d.name.partition("_")
        try:
            cid = int(cid_part)
        except ValueError:
            continue
        meta = {}
        course_json = d / "course.json"
        if course_json.exists():
            meta = json.loads(course_json.read_text())
        out.append({
            "id": cid,
            "slug": name,
            "name": meta.get("name") or name,
            "code": meta.get("course_code"),
            "term": (meta.get("term") or {}).get("name"),
        })
    return out


def _list_canvas_courses() -> list[dict]:
    if not COOKIE_FILE.exists():
        return []
    sys.path.insert(0, str(ROOT))
    from canvas_client import CanvasClient
    cookies = {c["name"]: c["value"] for c in json.loads(COOKIE_FILE.read_text())}
    try:
        return CanvasClient(cookies).list_courses()
    except Exception:
        return []


def index(request):
    have_cookies = COOKIE_FILE.exists()
    downloaded = _course_dirs()
    downloaded_ids = {c["id"] for c in downloaded}
    remote = _list_canvas_courses() if have_cookies else []
    remote_only = [c for c in remote if int(c.get("id", 0)) not in downloaded_ids]
    return render(request, "ui/index.html", {
        "have_cookies": have_cookies,
        "downloaded": downloaded,
        "remote_only": remote_only,
    })


@require_POST
def auth_login(request):
    # Detached so the browser doesn't hang for 2 min while user SSOs.
    subprocess.Popen(
        [PYTHON, "auth.py"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    messages.info(request, "Re-auth started — Firefox window should appear. After SSO, badge will turn green.")
    return redirect(request.META.get("HTTP_REFERER", "/"))


def auth_check(request):
    code, out = _run([PYTHON, "auth.py", "--check"])
    return HttpResponse(out, content_type="text/plain")


def courses_refresh(request):
    return redirect("ui:index")


@require_POST
def sync_course(request, cid: int):
    name = request.POST.get("name", "").strip() or None
    cdir = _course_dir_for(cid) if any(d.name.startswith(f"{cid}_") for d in DOWNLOADS.iterdir() if d.is_dir()) else None
    cdir_str = str(cdir) if cdir else ""

    log_path = f"/tmp/sync_run_{cid}.log"
    # Run full pipeline: download → bundle → ocr (vertex+tesseract) → bundle
    # → problems → analyze → likelihood → render_graph → class_info
    project = os.environ.get("GCP_PROJECT", "nd-canvas-ocr-1777818991")
    name_arg = ["--name", name] if name else []
    chain = (
        f"set -e; cd {ROOT}; "
        f"echo '=== [1/8] Canvas sync ==='; "
        f"{PYTHON} download.py --course {cid} {' '.join(name_arg)}; "
        # Resolve course dir (created by download)
        f"CDIR=$({PYTHON} -c 'import sys,pathlib; "
        f"d=[p for p in pathlib.Path(\"downloads\").iterdir() if p.name.startswith(f\"{cid}_\")]; "
        f"print(d[0]) if d else sys.exit(1)'); "
        f"echo \"course dir: $CDIR\"; "
        f"echo '=== [2/8] Bundle (initial) ==='; "
        f"{PYTHON} bundle.py --course-dir \"$CDIR\"; "
        f"echo '=== [3/8] OCR Tesseract (fallback) ==='; "
        f"{PYTHON} ocr.py --course-dir \"$CDIR\" || true; "
        f"echo '=== [4/8] OCR Vertex Gemini (high-quality) ==='; "
        f"{PYTHON} ocr_vertex.py --course-dir \"$CDIR\" --project {project} "
        f"  --model gemini-2.5-pro --workers 6 --rpm 5 "
        f"  --locations us-central1,us-east5,us-west4 || true; "
        f"echo '=== [5/8] Re-bundle with OCR text ==='; "
        f"{PYTHON} bundle.py --course-dir \"$CDIR\"; "
        f"echo '=== [6/8] Extract problems + analyze ==='; "
        f"{PYTHON} problems.py --course-dir \"$CDIR\"; "
        f"{PYTHON} analyze.py --course-dir \"$CDIR\"; "
        f"{PYTHON} likelihood.py --course-dir \"$CDIR\"; "
        f"echo '=== [7/8] Render dependency graph ==='; "
        f"{PYTHON} render_graph.py --course-dir \"$CDIR\" || true; "
        f"echo '=== [8/8] Generate class info ==='; "
        f"{PYTHON} class_info.py --course-dir \"$CDIR\" --rebuild || true; "
        f"echo '=== DONE ==='"
    )
    subprocess.Popen(
        ["bash", "-c", chain],
        cwd=ROOT,
        env={**os.environ, "GCP_PROJECT": project, "USE_BACKEND": "gemini"},
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    messages.info(request,
        f"Full re-sync pipeline started for course {cid}. "
        "Watch on the Sync page. Stages: download → bundle → OCR → re-bundle "
        "→ problems → analyze → likelihood → graph → class info.")
    return redirect("ui:sync_view", cid=cid)


def _materials_for_topic(cdir: Path, topic: str) -> list[dict]:
    """Find in-class + HW PDFs whose chapter folder matches the topic's chapter."""
    sys.path.insert(0, str(ROOT))
    from topic_graph import GRAPH
    info = GRAPH.get(topic, {})
    ch = info.get("ch")
    if not ch:
        return []
    modules_root = cdir / "modules"
    if not modules_root.exists():
        return []
    out = []
    chapter_token = f"chapter-{ch}-"
    chapter_token_alt = f"chapters-{ch}-"
    for d in modules_root.iterdir():
        if not d.is_dir():
            continue
        name = d.name.lower()
        if not (name.startswith(chapter_token) or name.startswith(chapter_token_alt) or name == f"chapter-{ch}"):
            continue
        for pdf in sorted(d.rglob("*.pdf")):
            kind = "in_class" if "inclass" in pdf.name.lower() else (
                "hw_key" if "key" in pdf.name.lower() else (
                "hw" if "hw" in pdf.name.lower() else "other"))
            out.append({
                "rel": str(pdf.relative_to(cdir)),
                "name": pdf.name,
                "kind": kind,
            })
    return out


def _course_dir_for(cid: int) -> Path:
    if DOWNLOADS.exists():
        for d in DOWNLOADS.iterdir():
            if d.is_dir() and d.name.startswith(f"{cid}_"):
                return d
    raise Http404(f"course {cid} not downloaded")


def course_detail(request, cid: int):
    cdir = _course_dir_for(cid)
    course_json = cdir / "course.json"
    course = json.loads(course_json.read_text()) if course_json.exists() else {}
    bundles_dir = cdir / "bundles"
    bundles = []
    if bundles_dir.exists():
        for f in sorted(bundles_dir.glob("*.md")):
            bundles.append({"cat": f.stem, "size": f.stat().st_size})
    files = [
        {"rel": str(p.relative_to(cdir)), "size": p.stat().st_size}
        for p in sorted(cdir.rglob("*.pdf"))
    ]
    gap_json = bundles_dir / "topic_gap.json"
    gap = json.loads(gap_json.read_text()) if gap_json.exists() else []
    # Show all topics that appear in exam corpus, sorted by gap (most under-prepared first).
    gap_top = [r for r in gap if r["exam_hits"] >= 1]
    gap_top.sort(key=lambda r: -r["share_gap_pp"])

    progress = _load_progress(cdir)
    problems_file = bundles_dir / "problems.json"
    if problems_file.exists():
        problems_by_topic = json.loads(problems_file.read_text())
    else:
        problems_by_topic = {}

    sys.path.insert(0, str(ROOT))
    from topic_graph import GRAPH

    enriched = []
    for r in gap_top:
        t = r["topic"]
        total = len(problems_by_topic.get(t, []))
        if total == 0:
            continue  # no extractable problems — hide from priorities table
        done = len(progress["done"].get(t, []))
        r["progress_done"] = done
        r["progress_total"] = total
        r["progress_pct"] = int(round(100 * done / max(total, 1))) if total else 0
        r["label"] = GRAPH.get(t, {}).get("label", t)
        r["chapter"] = GRAPH.get(t, {}).get("ch")
        r["materials"] = _materials_for_topic(cdir, t)
        enriched.append(r)
    gap_top = enriched

    overall_total = sum(len(v) for v in problems_by_topic.values())
    overall_done = sum(len(v) for v in progress["done"].values())
    overall_pct = int(round(100 * overall_done / max(overall_total, 1))) if overall_total else 0

    return render(request, "ui/course.html", {
        "cid": cid,
        "course": course,
        "bundles": bundles,
        "files": files,
        "gap_top": gap_top,
        "have_bundles": bool(bundles),
        "have_gap": bool(gap),
        "overall_done": overall_done,
        "overall_total": overall_total,
        "overall_pct": overall_pct,
    })


@require_POST
def build_bundle(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "bundle.py", "--course-dir", str(cdir)])
    if code == 0:
        messages.success(request, "Bundles built.")
    else:
        messages.error(request, f"Bundle failed: {out}")
    return redirect("ui:course_detail", cid=cid)


@require_POST
def analyze_course(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "analyze.py", "--course-dir", str(cdir)])
    if code == 0:
        messages.success(request, "Topic gap report generated.")
    else:
        messages.error(request, f"Analyze failed: {out}")
    return redirect("ui:course_detail", cid=cid)


def view_bundle(request, cid: int, cat: str):
    cdir = _course_dir_for(cid)
    f = cdir / "bundles" / f"{cat}.md"
    if not f.exists():
        raise Http404
    return HttpResponse(f.read_text(), content_type="text/plain; charset=utf-8")


def ocr_text(request, cid: int, rel: str):
    cdir = _course_dir_for(cid)
    f = cdir / "_ocr" / (rel.replace("/", "__") + ".txt")
    if not f.exists():
        raise Http404
    return HttpResponse(f.read_text(), content_type="text/plain; charset=utf-8")


@require_POST
def ask_region(request, cid: int):
    """Accept a cropped PNG region + question, send to Gemini multimodal."""
    cdir = _course_dir_for(cid)
    question = request.POST.get("question", "").strip() or "Explain this part of the page."
    img_file = request.FILES.get("region")
    if not img_file:
        return HttpResponse(json.dumps({"error": "no region image"}),
                            status=400, content_type="application/json")
    import time as _time, uuid as _uuid
    tmpdir = cdir / "bundles" / "_attempts"
    tmpdir.mkdir(parents=True, exist_ok=True)
    dest = tmpdir / f"region_{int(_time.time())}_{_uuid.uuid4().hex[:8]}.png"
    with open(dest, "wb") as f:
        for chunk in img_file.chunks():
            f.write(chunk)
    sys.path.insert(0, str(ROOT))
    from gemini_client import generate_with_images
    prompt = (
        "The image is a cropped region of a college statistics page. "
        f"Student question: {question}\n\n"
        "Answer concisely with Markdown + LaTeX ($...$ inline, $$...$$ block). "
        "If the region contains a problem, solve it step-by-step."
    )
    try:
        text = generate_with_images(prompt, [str(dest)], max_output_tokens=16384)
    except Exception as e:
        return HttpResponse(json.dumps({"error": str(e)}),
                            status=500, content_type="application/json")
    return HttpResponse(json.dumps({"answer": text}),
                        content_type="application/json")


def page_count(request, cid: int, rel: str):
    cdir = _course_dir_for(cid)
    pdf = (cdir / rel).resolve()
    if not str(pdf).startswith(str(cdir.resolve())) or not pdf.exists():
        raise Http404
    try:
        from pypdf import PdfReader
        n = len(PdfReader(str(pdf)).pages)
    except Exception:
        n = 1
    return HttpResponse(json.dumps({"pages": n}), content_type="application/json")


def serve_file(request, cid: int, rel: str):
    cdir = _course_dir_for(cid)
    target = (cdir / rel).resolve()
    if not str(target).startswith(str(cdir.resolve())):
        raise Http404
    if not target.exists():
        raise Http404
    return FileResponse(open(target, "rb"))


@require_POST
def build_index(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "vectorize.py", "--course-dir", str(cdir), "--rebuild"])
    if code == 0:
        messages.success(request, "Vector index built.")
    else:
        messages.error(request, f"Index failed: {out}")
    return redirect("ui:course_detail", cid=cid)


def topic_detail(request, cid: int, topic: str):
    cdir = _course_dir_for(cid)
    problems_file = cdir / "bundles" / "problems.json"
    if not problems_file.exists():
        raise Http404("run problems.py first")
    data = json.loads(problems_file.read_text())
    problems = list(data.get(topic, []))

    sort_by = request.GET.get("sort", "likelihood")
    sort_keys = {
        "likelihood": lambda p: -p.get("likelihood", 0),
        "source_weight": lambda p: (-p.get("source_weight", 0),
                                    p.get("source", ""), p.get("page", 0)),
        "difficulty_desc": lambda p: -p.get("difficulty", 0),
        "difficulty_asc": lambda p: p.get("difficulty", 0),
        "sequential": lambda p: (p.get("source", ""), p.get("page", 0),
                                 p.get("problem", 0)),
        "chapter": lambda p: (p.get("chapter") or 999,
                              p.get("source", ""), p.get("problem", 0)),
    }
    if sort_by in sort_keys:
        problems.sort(key=sort_keys[sort_by])

    sys.path.insert(0, str(ROOT))
    from topic_graph import GRAPH, prereqs_of, dependents_of

    info = GRAPH.get(topic, {})
    summary_file = cdir / "bundles" / "topics" / f"{topic}.md"
    summary_md = summary_file.read_text() if summary_file.exists() else ""
    # Replace TOPIC_LINK_PLACEHOLDER:<key> with real URLs
    if summary_md:
        for key in GRAPH:
            summary_md = summary_md.replace(
                f"TOPIC_LINK_PLACEHOLDER:{key}",
                reverse("ui:topic_detail", args=[cid, key]),
            )

    progress = _load_progress(cdir)
    done_keys = set(progress["done"].get(topic, []))

    def _key(p):
        return f"{p['source']}#p{p['page']}#{p['problem']}"

    annotated = []
    for p in problems:
        ap = dict(p)
        ap["key"] = _key(p)
        ap["done"] = ap["key"] in done_keys
        annotated.append(ap)
    done_count = sum(1 for p in annotated if p["done"])

    materials = _materials_for_topic(cdir, topic)
    # Annotate with rendered URLs + OCR cache key
    for m in materials:
        rel = m["rel"]
        m["pdf_url"] = reverse("ui:serve_file", args=[cid, rel])
        m["ocr_key"] = rel.replace("/", "__") + ".txt"
        m["has_ocr"] = (cdir / "_ocr" / m["ocr_key"]).exists()
        # First-page snippet
        m["snippet1_url"] = reverse("ui:snippet", args=[cid, rel, 1])

    return render(request, "ui/topic.html", {
        "cid": cid,
        "topic": topic,
        "problems": annotated,
        "info": info,
        "prereqs": [(k, GRAPH[k]) for k in prereqs_of(topic) if k in GRAPH],
        "dependents": [(k, GRAPH[k]) for k in dependents_of(topic)],
        "summary_md": summary_md,
        "have_summary": bool(summary_md),
        "done_count": done_count,
        "total_count": len(annotated),
        "pct": int(round(100 * done_count / max(len(annotated), 1))),
        "materials": materials,
        "sort_by": sort_by,
        "sort_options": [
            ("likelihood", "Likelihood (most likely first)"),
            ("source_weight", "Source weight (practice → exams → HW → in-class)"),
            ("difficulty_desc", "Difficulty (hard first)"),
            ("difficulty_asc", "Difficulty (easy first)"),
            ("sequential", "Sequential (file order)"),
            ("chapter", "Chapter number"),
        ],
    })


@require_POST
def build_summary(request, cid: int, topic: str):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "summarize_topics.py", "--course-dir", str(cdir), "--topic", topic, "--rebuild"])
    if request.headers.get("X-Requested-With") == "fetch":
        if code == 0:
            summary_file = cdir / "bundles" / "topics" / f"{topic}.md"
            md = summary_file.read_text() if summary_file.exists() else ""
            sys.path.insert(0, str(ROOT))
            from topic_graph import GRAPH
            for key in GRAPH:
                md = md.replace(
                    f"TOPIC_LINK_PLACEHOLDER:{key}",
                    reverse("ui:topic_detail", args=[cid, key]),
                )
            return HttpResponse(json.dumps({"ok": True, "markdown": md}),
                                content_type="application/json")
        return HttpResponse(json.dumps({"ok": False, "error": out[-1500:]}),
                            content_type="application/json", status=500)
    if code == 0:
        messages.success(request, f"Summary generated for {topic}.")
    else:
        messages.error(request, f"Summary failed: {out}")
    return redirect("ui:topic_detail", cid=cid, topic=topic)


def graph_view(request, cid: int):
    sys.path.insert(0, str(ROOT))
    from topic_graph import mermaid, GRAPH
    return render(request, "ui/graph.html", {
        "cid": cid,
        "mermaid": mermaid(),
        "topics": GRAPH,
    })


@require_POST
def followup(request, cid: int, topic: str):
    cdir = _course_dir_for(cid)
    selection = request.POST.get("selection", "").strip()
    mode = request.POST.get("mode", "ask").strip()
    user_q = request.POST.get("question", "").strip()
    if not selection:
        return HttpResponse(json.dumps({"error": "no selection"}), status=400, content_type="application/json")

    summary_file = cdir / "bundles" / "topics" / f"{topic}.md"
    summary_md = summary_file.read_text() if summary_file.exists() else ""

    sys.path.insert(0, str(ROOT))
    from solver import _gather_context
    context = _gather_context(cdir, topic, max_chars=20000)

    if mode == "expand":
        instruction = (
            "The student selected the snippet below from the topic summary and wants MORE DETAIL. "
            "Provide additional context, edge cases, intuition, and one extra worked micro-example. "
            "Do NOT rewrite or contradict the selection — only add. Keep under 250 words. "
            "Use Markdown + LaTeX math ($...$ inline, $$...$$ block)."
        )
    else:
        instruction = (
            "The student selected the snippet below and asks a clarifying follow-up question. "
            "Answer the question concisely and directly with respect to the selection. "
            "Cite specific formulas. Keep under 250 words. Use Markdown + LaTeX."
        )

    prompt = (
        f"Topic: {topic}\n\n"
        f"--- TOPIC SUMMARY (for context) ---\n{summary_md}\n\n"
        f"--- COURSE REFERENCE ---\n{context}\n\n"
        f"--- SELECTED SNIPPET ---\n{selection}\n\n"
        f"--- TASK ---\n{instruction}"
    )
    if user_q:
        prompt += f"\n\n--- STUDENT QUESTION ---\n{user_q}"

    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "gemini":
        from gemini_client import generate
        try:
            text = generate(prompt, max_output_tokens=16384)
        except Exception as e:
            return HttpResponse(json.dumps({"error": str(e)}), status=500, content_type="application/json")
        return HttpResponse(json.dumps({"answer": text}), content_type="application/json")
    # claude fallback
    import shutil, subprocess
    if not shutil.which("claude"):
        return HttpResponse(json.dumps({"error": "claude CLI missing"}), status=500, content_type="application/json")
    try:
        proc = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return HttpResponse(json.dumps({"error": "timeout"}), status=504, content_type="application/json")
    if proc.returncode != 0:
        return HttpResponse(json.dumps({"error": proc.stderr[:300]}), status=500, content_type="application/json")
    return HttpResponse(json.dumps({"answer": proc.stdout.strip()}), content_type="application/json")


def _progress_path(cdir: Path) -> Path:
    return cdir / "bundles" / "progress.json"


def _load_progress(cdir: Path) -> dict:
    f = _progress_path(cdir)
    if not f.exists():
        return {"done": {}}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {"done": {}}


def _save_progress(cdir: Path, data: dict) -> None:
    _progress_path(cdir).parent.mkdir(parents=True, exist_ok=True)
    _progress_path(cdir).write_text(json.dumps(data, indent=2))


def progress_get(request, cid: int):
    cdir = _course_dir_for(cid)
    return HttpResponse(json.dumps(_load_progress(cdir)), content_type="application/json")


@require_POST
def progress_toggle(request, cid: int):
    cdir = _course_dir_for(cid)
    topic = request.POST.get("topic", "").strip()
    key = request.POST.get("key", "").strip()  # unique per problem
    state = request.POST.get("state", "").strip()  # "1" or "0"
    if not topic or not key:
        return HttpResponse(json.dumps({"error": "missing topic or key"}),
                            status=400, content_type="application/json")
    data = _load_progress(cdir)
    bucket = data["done"].setdefault(topic, [])
    if state == "1":
        if key not in bucket:
            bucket.append(key)
    else:
        if key in bucket:
            bucket.remove(key)
    _save_progress(cdir, data)
    return HttpResponse(json.dumps({"ok": True, "count": len(bucket)}),
                        content_type="application/json")


def mock_exam(request, cid: int):
    cdir = _course_dir_for(cid)
    problems_file = cdir / "bundles" / "problems.json"
    if not problems_file.exists():
        raise Http404("run problems.py first")
    data = json.loads(problems_file.read_text())

    sys.path.insert(0, str(ROOT))
    from topic_graph import GRAPH

    selected_topics = request.GET.getlist("topic") or []
    n = int(request.GET.get("n", 10))
    minutes = int(request.GET.get("minutes", 30))

    pool = []
    for t, problems in data.items():
        if selected_topics and t not in selected_topics:
            continue
        for p in problems:
            ap = dict(p)
            ap["topic"] = t
            ap["topic_label"] = GRAPH.get(t, {}).get("label", t)
            pool.append(ap)

    import random
    random.shuffle(pool)
    sample = pool[: max(1, n)]

    return render(request, "ui/mock.html", {
        "cid": cid,
        "minutes": minutes,
        "problems": sample,
        "available_topics": sorted(data.keys()),
        "selected_topics": selected_topics,
        "n": n,
    })


@require_POST
def grade_answer(request, cid: int):
    cdir = _course_dir_for(cid)
    problem = request.POST.get("problem", "").strip()
    attempt = request.POST.get("attempt", "").strip()
    topic = request.POST.get("topic", "").strip() or None

    # Save uploaded image attempts under bundles/_attempts/ and reference paths in the prompt.
    image_paths: list[str] = []
    attempts_dir = cdir / "bundles" / "_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    import time, uuid
    for f in request.FILES.getlist("image"):
        ext = (f.name.rsplit(".", 1)[-1] or "png").lower()[:5]
        safe_ext = "".join(c for c in ext if c.isalnum())
        dest = attempts_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{safe_ext or 'png'}"
        with open(dest, "wb") as out:
            for chunk in f.chunks():
                out.write(chunk)
        image_paths.append(str(dest))

    if not problem or (not attempt and not image_paths):
        return HttpResponse(json.dumps({"error": "need problem + attempt (text or image)"}),
                            status=400, content_type="application/json")

    sys.path.insert(0, str(ROOT))
    from solver import _gather_context
    context = _gather_context(cdir, topic, max_chars=20000)

    instruction = (
        "You are a statistics tutor. Grade the student's attempt against the problem. "
        "Use this rubric:\n"
        "1. Identify the student's final answer.\n"
        "2. Compute the correct answer yourself.\n"
        "3. Compare. State CORRECT or INCORRECT in bold.\n"
        "4. If incorrect: pinpoint the FIRST step where they went wrong, quote that step, "
        "explain the specific mistake, then show the correct path from that point.\n"
        "5. If correct: confirm and note any inefficiencies.\n"
        "Use Markdown + LaTeX ($...$ inline, $$...$$ block). Keep under 350 words."
    )
    prompt = (
        f"--- COURSE REFERENCE ---\n{context}\n\n"
        f"--- PROBLEM ---\n{problem}\n\n"
        f"--- STUDENT ATTEMPT (typed) ---\n{attempt or '(none — see image)'}\n\n"
        f"--- TASK ---\n{instruction}"
    )
    if image_paths:
        prompt = (
            "Image(s) of the student's handwritten/screenshotted work are attached. "
            "Read them along with the typed text below.\n\n" + prompt
        )

    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "gemini":
        sys.path.insert(0, str(ROOT))
        from gemini_client import generate, generate_with_images
        try:
            if image_paths:
                text = generate_with_images(prompt, image_paths, max_output_tokens=16384)
            else:
                text = generate(prompt, max_output_tokens=16384)
        except Exception as e:
            return HttpResponse(json.dumps({"error": str(e)}), status=500, content_type="application/json")
        return HttpResponse(json.dumps({"feedback": text, "images": len(image_paths)}),
                            content_type="application/json")

    import shutil, subprocess
    if not shutil.which("claude"):
        return HttpResponse(json.dumps({"error": "claude CLI missing"}), status=500, content_type="application/json")
    cli_args = ["claude", "-p", prompt]
    if image_paths:
        cli_args = ["claude", "-p", "--allowedTools", "Read",
                    prompt + "\n\nImages:\n" + "\n".join(image_paths)]
    try:
        proc = subprocess.run(cli_args, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return HttpResponse(json.dumps({"error": "timeout"}), status=504, content_type="application/json")
    if proc.returncode != 0:
        return HttpResponse(json.dumps({"error": proc.stderr[:300]}), status=500, content_type="application/json")
    return HttpResponse(json.dumps({"feedback": proc.stdout.strip(), "images": len(image_paths)}),
                        content_type="application/json")


def likelihood(request, cid: int):
    cdir = _course_dir_for(cid)
    f = cdir / "bundles" / "likelihood.json"
    rows = json.loads(f.read_text()) if f.exists() else []
    return render(request, "ui/likelihood.html", {"cid": cid, "rows": rows})


@require_POST
def build_likelihood(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "likelihood.py", "--course-dir", str(cdir)])
    if code == 0:
        messages.success(request, "Likelihood ranking generated.")
    else:
        messages.error(request, f"Failed: {out}")
    return redirect("ui:likelihood", cid=cid)


@xframe_options_sameorigin
def formulas(request, cid: int):
    cdir = _course_dir_for(cid)
    pdf = cdir / "modules/final-exam-materials/30440feformulas.pdf"
    if not pdf.exists():
        raise Http404("formula sheet not found")
    sys.path.insert(0, str(ROOT))
    from snippets import render_page
    from pypdf import PdfReader
    pages = PdfReader(str(pdf)).pages
    images = []
    for i in range(len(pages)):
        try:
            png = render_page(pdf, i + 1, cdir)
            images.append(reverse("ui:snippet", args=[cid, str(pdf.relative_to(cdir)), i + 1]))
        except Exception:
            pass
    return render(request, "ui/formulas.html", {
        "cid": cid,
        "images": images,
    })


def jobs_view(request, cid: int):
    return render(request, "ui/jobs.html", {"cid": cid})


def sync_view(request, cid: int):
    return render(request, "ui/sync.html", {"cid": cid})


def sync_status(request, cid: int):
    log = Path(f"/tmp/sync_log_{cid}.json")
    pipeline_log = Path(f"/tmp/sync_run_{cid}.log")
    if not log.exists() and not pipeline_log.exists():
        return HttpResponse(json.dumps({"error": "no sync started"}),
                            content_type="application/json", status=404)
    data = {}
    if log.exists():
        try:
            data = json.loads(log.read_text())
        except Exception:
            data = {}
    if pipeline_log.exists():
        text = pipeline_log.read_text()
        # Detect current stage
        stages = ["[1/8]", "[2/8]", "[3/8]", "[4/8]", "[5/8]",
                  "[6/8]", "[7/8]", "[8/8]", "DONE"]
        last_stage = ""
        for s in stages:
            if s in text:
                last_stage = s
        data["pipeline_stage"] = last_stage or "starting"
        data["pipeline_finished"] = "=== DONE ===" in text
        data["pipeline_tail"] = "\n".join(text.splitlines()[-15:])
    return HttpResponse(json.dumps(data), content_type="application/json")


def sync_errors(request, cid: int):
    """Classify errors from sync log: critical / recoverable / safe-to-ignore."""
    log = Path(f"/tmp/sync_log_{cid}.json")
    if not log.exists():
        return HttpResponse(json.dumps({"error": "no sync"}),
                            content_type="application/json", status=404)
    data = json.loads(log.read_text())
    errors = data.get("errors", [])
    classified = {"critical": [], "recoverable": [], "ignore": []}
    for e in errors:
        note = (e.get("note") or "").lower()
        kind = e.get("kind") or ""
        if "401" in note or "unauthorized" in note:
            classified["recoverable"].append({**e, "reason": "Canvas session expired — re-auth"})
        elif "403" in note or "forbidden" in note or "no_url" in note:
            classified["ignore"].append({**e, "reason": "Instructor restricted this asset (locked/private)"})
        elif "404" in note or "not found" in note:
            classified["ignore"].append({**e, "reason": "Asset doesn't exist on Canvas"})
        elif "timeout" in note or "connection" in note:
            classified["recoverable"].append({**e, "reason": "Network glitch — retry"})
        elif kind in ("page", "file") and "fail" in (e.get("note") or "").lower():
            classified["critical"].append({**e, "reason": "Real failure — investigate"})
        else:
            classified["ignore"].append({**e, "reason": "Likely benign skip"})
    return HttpResponse(json.dumps({
        "total": len(errors),
        "critical": classified["critical"],
        "recoverable": classified["recoverable"],
        "ignore": classified["ignore"],
    }, indent=2), content_type="application/json")


def class_info(request, cid: int):
    cdir = _course_dir_for(cid)
    f = cdir / "bundles" / "CLASS_INFO.md"
    md = f.read_text() if f.exists() else ""
    return render(request, "ui/class_info.html", {
        "cid": cid,
        "info_md": md,
        "have_info": bool(md.strip()),
    })


@require_POST
def build_class_info(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "class_info.py", "--course-dir", str(cdir), "--rebuild"])
    if code == 0:
        messages.success(request, "Class info generated.")
    else:
        messages.error(request, f"Failed: {out}")
    return redirect("ui:class_info", cid=cid)


def calendar_dedup(request, cid: int):
    """Check existing Calendar events on the same dates; return list for confirm."""
    cdir = _course_dir_for(cid)
    cal_id = request.GET.get("calendar_id", "primary").strip() or "primary"
    sys.path.insert(0, str(ROOT))
    from calendar_sync import (extract_events_from_class_info,
                               extract_events_from_assignments,
                               get_calendar_service, categorize_event)
    info_md = (cdir / "bundles" / "CLASS_INFO.md").read_text() if (cdir / "bundles" / "CLASS_INFO.md").exists() else ""
    events = extract_events_from_class_info(info_md) + extract_events_from_assignments(cdir)
    seen = set()
    uniq = []
    for e in events:
        key = (e["date"], e["title"][:60])
        if key in seen: continue
        seen.add(key); uniq.append(e)

    try:
        service = get_calendar_service()
    except Exception as exc:
        return HttpResponse(json.dumps({"error": str(exc)}), status=500,
                            content_type="application/json")

    # Group dates
    dates = sorted({e["date"] for e in uniq})
    if not dates:
        return HttpResponse(json.dumps({"matches": []}),
                            content_type="application/json")
    from datetime import datetime as _dt
    time_min = _dt.fromisoformat(min(dates) + "T00:00:00").isoformat() + "-05:00"
    time_max = _dt.fromisoformat(max(dates) + "T23:59:59").isoformat() + "-05:00"
    try:
        existing = service.events().list(
            calendarId=cal_id, timeMin=time_min, timeMax=time_max,
            singleEvents=True, maxResults=2500,
        ).execute().get("items", [])
    except Exception as exc:
        return HttpResponse(json.dumps({"error": str(exc)}), status=500,
                            content_type="application/json")

    # For each new event, find existing events same date with similar title
    def _toks(s: str) -> set:
        s = (s or "").lower()
        # strip emoji + brackets + punctuation
        import re as _re
        s = _re.sub(r"[^\w\s]", " ", s)
        return {t for t in s.split() if len(t) > 2 and t not in
                {"the", "and", "for", "from", "ndcanvas", "statistics"}}

    matches = []
    label = cdir.name.split("_", 1)[1] if "_" in cdir.name else cdir.name
    for e in uniq:
        emoji, kind, _color = categorize_event(e)
        new_tokens = _toks(e["title"]) | _toks(kind)
        same_date = []
        # Group existing by ndcanvas_tag → if multiple ours have same logical
        # event (date+title-prefix) keep newest, mark older as suggested-delete
        for ex in existing:
            ex_start = ex.get("start", {})
            ex_date = ex_start.get("date") or (ex_start.get("dateTime") or "")[:10]
            if ex_date != e["date"]:
                continue
            ex_summary = ex.get("summary") or ""
            ex_priv = (ex.get("extendedProperties") or {}).get("private", {})
            already_ours = bool(ex_priv.get("ndcanvas_tag"))
            ex_tokens = _toks(ex_summary)
            overlap = len(new_tokens & ex_tokens) / max(len(new_tokens), 1)
            same_date.append({
                "id": ex.get("id"),
                "summary": ex_summary,
                "ours": already_ours,
                "tag": ex_priv.get("ndcanvas_tag", ""),
                "overlap": round(overlap, 2),
                "preselect": False,  # set below
            })

        # Pre-select logic:
        # - If multiple "ours" with same tag, keep newest (last in list), mark others
        # - If foreign event has high overlap (>=0.6) with the new one, suggest delete
        if same_date:
            ours_by_tag = {}
            for x in same_date:
                if x["ours"] and x["tag"]:
                    ours_by_tag.setdefault(x["tag"], []).append(x)
            for tag, group in ours_by_tag.items():
                if len(group) > 1:
                    # mark all but last for deletion
                    for x in group[:-1]:
                        x["preselect"] = True
                        x["reason"] = "duplicate of newer same-tag event"
            for x in same_date:
                if not x["ours"] and x["overlap"] >= 0.6:
                    x["preselect"] = True
                    x["reason"] = f"high title overlap ({int(x['overlap']*100)}%) with new event"
            matches.append({
                "date": e["date"], "title": e["title"], "emoji": emoji, "kind": kind,
                "existing": same_date,
            })
    return HttpResponse(json.dumps({"matches": matches, "count": len(matches)}),
                        content_type="application/json")


@require_POST
def calendar_delete_events(request, cid: int):
    """Delete a list of Google Calendar event IDs the user confirmed."""
    cal_id = request.POST.get("calendar_id", "primary").strip() or "primary"
    ids_raw = request.POST.get("event_ids", "")
    try:
        ids = json.loads(ids_raw) if ids_raw else []
    except Exception:
        return HttpResponse(json.dumps({"error": "bad event_ids"}), status=400,
                            content_type="application/json")
    if not isinstance(ids, list) or not ids:
        return HttpResponse(json.dumps({"deleted": 0}),
                            content_type="application/json")
    sys.path.insert(0, str(ROOT))
    from calendar_sync import get_calendar_service
    try:
        service = get_calendar_service()
    except Exception as exc:
        return HttpResponse(json.dumps({"error": str(exc)}), status=500,
                            content_type="application/json")
    deleted = 0
    failed = []
    for eid in ids:
        try:
            service.events().delete(calendarId=cal_id, eventId=eid).execute()
            deleted += 1
        except Exception as exc:
            failed.append({"id": eid, "error": str(exc)[:200]})
    return HttpResponse(json.dumps({"deleted": deleted, "failed": failed}),
                        content_type="application/json")


def calendar_status(request, cid: int):
    log_path = Path(f"/tmp/calendar_sync_{cid}.log")
    if not log_path.exists():
        return HttpResponse(json.dumps({"error": "no run yet"}),
                            content_type="application/json", status=404)
    text = log_path.read_text()
    created = text.count("CREATED ")
    updated = text.count("UPDATED ")
    failed = text.count("FAIL ")
    total_match = re.search(r"Found (\d+) unique events", text)
    total = int(total_match.group(1)) if total_match else 0
    finished = ("Done." in text or
                (created + updated + failed) >= total > 0 or
                "INSUFFICIENT" in text.upper())
    scope_missing = "insufficient authentication scopes" in text.lower()
    api_disabled = "has not been used" in text.lower() or "accessnotconfigured" in text.lower()
    return HttpResponse(json.dumps({
        "total": total, "created": created, "updated": updated, "failed": failed,
        "finished": finished, "scope_missing": scope_missing,
        "api_disabled": api_disabled,
        "tail": "\n".join(text.splitlines()[-15:]),
    }), content_type="application/json")


@require_POST
def calendar_ics_build(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "calendar_sync.py", "--course-dir", str(cdir), "--ics"])
    if code == 0:
        n = out.count("Wrote")
        messages.success(request, "ICS file generated. Click 'Download .ics' to import to Google/Apple/Outlook calendar.")
    else:
        messages.error(request, f"ICS build failed: {out[-800:]}")
    return redirect("ui:class_info", cid=cid)


def calendar_ics(request, cid: int):
    cdir = _course_dir_for(cid)
    label = cdir.name.split("_", 1)[1] if "_" in cdir.name else cdir.name
    f = cdir / "bundles" / f"{label}.ics"
    if not f.exists():
        raise Http404("Generate the ICS first.")
    resp = FileResponse(open(f, "rb"), content_type="text/calendar")
    resp["Content-Disposition"] = f'attachment; filename="{label}.ics"'
    return resp


@require_POST
def calendar_grant(request, cid: int):
    """Spawn gcloud ADC login with Calendar scope. Browser opens for SSO."""
    subprocess.Popen(
        ["gcloud", "auth", "application-default", "login",
         "--scopes=https://www.googleapis.com/auth/cloud-platform,"
         "https://www.googleapis.com/auth/calendar.events"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    messages.info(request, "Browser should open for Calendar grant. "
                            "After SSO completes, click 'Sync to Google Calendar' again.")
    return redirect("ui:class_info", cid=cid)


def calendar_preview(request, cid: int):
    """Return JSON list of events the calendar sync would create/update."""
    cdir = _course_dir_for(cid)
    sys.path.insert(0, str(ROOT))
    from calendar_sync import (
        extract_events_from_class_info, extract_events_from_assignments,
        categorize_event,
    )
    info_md = ""
    info_path = cdir / "bundles" / "CLASS_INFO.md"
    if info_path.exists():
        info_md = info_path.read_text()
    events = extract_events_from_class_info(info_md) + extract_events_from_assignments(cdir)
    seen = set()
    uniq = []
    for e in events:
        key = (e["date"], e["title"][:60])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    for e in uniq:
        emoji, kind, color = categorize_event(e)
        e["emoji"] = emoji
        e["kind"] = kind
        e["color"] = color
    return HttpResponse(json.dumps({"events": uniq, "count": len(uniq)}, indent=2),
                        content_type="application/json")


@require_POST
def calendar_sync_run(request, cid: int):
    cdir = _course_dir_for(cid)
    cal_id = request.POST.get("calendar_id", "").strip() or "primary"
    log_path = f"/tmp/calendar_sync_{cid}.log"
    if request.headers.get("X-Requested-With") == "fetch":
        # Async mode: spawn detached, return immediately
        subprocess.Popen(
            [PYTHON, "calendar_sync.py", "--course-dir", str(cdir), "--calendar", cal_id],
            cwd=ROOT,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return HttpResponse(json.dumps({"started": True}), content_type="application/json")
    code, out = _run([PYTHON, "calendar_sync.py", "--course-dir", str(cdir),
                      "--calendar", cal_id])
    created = out.count("CREATED ")
    updated = out.count("UPDATED ")
    failed = out.count("FAIL ")
    if "insufficient authentication scopes" in out.lower():
        messages.error(request,
            "Calendar scope missing. Click 'Grant Calendar access' to open a "
            "browser auth flow.")
    elif failed and not (created or updated):
        messages.error(request, f"Calendar sync: {failed} failed. Tail: {out[-800:]}")
    elif code == 0 or created or updated:
        msg = f"Calendar synced: {created} created, {updated} updated"
        if failed:
            msg += f", {failed} failed"
        messages.success(request, msg + ".")
    else:
        messages.error(request, f"Calendar sync failed (code {code}): {out[-1500:]}")
    return redirect("ui:class_info", cid=cid)


def jobs_status(request, cid: int):
    """Return status of background OCR job by parsing /tmp/ocr_*.log files."""
    import re
    cdir = _course_dir_for(cid)

    jobs = []
    log_files = [
        ("vertex", "/tmp/ocr_vertex_full.log"),
        ("claude", "/tmp/ocr_claude_full.log"),
        ("gemini", "/tmp/ocr_gemini_full.log"),
        ("tesseract", "/tmp/ocr_tesseract_full.log"),
        ("summaries", "/tmp/summaries.log"),
    ]
    for name, path in log_files:
        p = Path(path)
        if not p.exists():
            continue
        try:
            text = p.read_text()
        except Exception:
            continue
        total_match = re.search(r"Transcribing (\d+) pages|Generating (\d+) topic summaries", text)
        total = int(total_match.group(1) or total_match.group(2)) if total_match else 0
        done = len(re.findall(r"^\s*✓ \[", text, re.M))
        failed = len(re.findall(r"^\s*✗ \[", text, re.M))
        cost_match = re.search(r"Estimated cost: ~\$([\d.]+)", text)
        cost = float(cost_match.group(1)) if cost_match else 0.0
        # If no explicit cost line and this looks like a summary job, estimate
        # from chars (avg ~$0.04 per Gemini 2.5 Pro summary call)
        if cost == 0.0 and "topic summaries" in text:
            cost = round(0.04 * (done + failed), 2)
        rpm_match = re.search(r"@ (\d+) RPM", text)
        rpm = int(rpm_match.group(1)) if rpm_match else None
        workers_match = re.search(r"(\d+) workers @", text)
        workers = int(workers_match.group(1)) if workers_match else None
        model_match = re.search(r"using ([\w.\-]+) on", text)
        model = model_match.group(1) if model_match else None
        finished = "Done." in text
        # Process actually still alive?
        running = False
        if not finished:
            try:
                # Quick mtime heuristic — log updated within last 90s
                running = (Path(path).stat().st_mtime > (Path(path).stat().st_mtime - 0)) and \
                          (text.splitlines()[-1].strip() != "")
                # Better: subprocess check
                import subprocess
                pid_check = subprocess.run(["pgrep", "-f", f"ocr_{name}.py"],
                                           capture_output=True, text=True)
                running = bool(pid_check.stdout.strip())
            except Exception:
                pass

        # Live-rate ETA from last 10 completed page lines
        page_times = [float(m) for m in re.findall(r"\(\d+c, ([\d.]+)s\)", text)][-20:]
        avg_page_s = sum(page_times) / len(page_times) if page_times else None
        remaining = max(total - done - failed, 0)

        # Theoretical floor from RPM
        rpm_floor_min = remaining / rpm if rpm else None
        # Observed throughput
        observed_min = (remaining * avg_page_s / 60 / max(workers or 1, 1)) if avg_page_s else None
        # Use the larger of the two (real-world bound)
        eta_min = None
        if rpm_floor_min is not None and observed_min is not None:
            eta_min = max(rpm_floor_min, observed_min)
        elif rpm_floor_min is not None:
            eta_min = rpm_floor_min
        elif observed_min is not None:
            eta_min = observed_min

        last_lines = "\n".join(text.splitlines()[-12:])
        jobs.append({
            "name": name,
            "log": str(path),
            "model": model,
            "rpm": rpm,
            "workers": workers,
            "total": total,
            "done": done,
            "failed": failed,
            "remaining": remaining,
            "pct": int(round(100 * done / total)) if total else 0,
            "cost_est": cost,
            "avg_page_s": round(avg_page_s, 1) if avg_page_s else None,
            "rpm_floor_min": round(rpm_floor_min, 1) if rpm_floor_min else None,
            "observed_min": round(observed_min, 1) if observed_min else None,
            "eta_min": round(eta_min, 1) if eta_min else None,
            "finished": finished,
            "running": running,
            "tail": last_lines,
            "mtime": p.stat().st_mtime,
        })

    # OCR cache state
    ocr_dir = cdir / "_ocr"
    cached = sorted(ocr_dir.glob("*.txt")) if ocr_dir.exists() else []
    ocr_files = []
    for f in cached:
        text = f.read_text(errors="ignore")
        bad = "[vertex error" in text or "[claude error" in text or "[gemini error" in text
        ocr_files.append({"name": f.name, "size": f.stat().st_size, "bad": bad})

    return HttpResponse(json.dumps({"jobs": jobs, "ocr_files": ocr_files}, indent=2),
                        content_type="application/json")


def graph_png(request, cid: int):
    cdir = _course_dir_for(cid)
    png = cdir / "bundles" / "topic_graph.png"
    if not png.exists():
        raise Http404("run render_graph.py first")
    return FileResponse(open(png, "rb"), content_type="image/png")


@require_POST
def render_graph_png(request, cid: int):
    cdir = _course_dir_for(cid)
    code, out = _run([PYTHON, "render_graph.py", "--course-dir", str(cdir)])
    if code == 0:
        messages.success(request, "Graph PNG rendered.")
    else:
        messages.error(request, f"Render failed: {out}")
    return redirect("ui:graph_view", cid=cid)


def path_view(request, cid: int):
    sys.path.insert(0, str(ROOT))
    from topic_graph import topo_sort, GRAPH
    order = topo_sort()
    return render(request, "ui/path.html", {
        "cid": cid,
        "order": [(k, GRAPH[k]) for k in order],
    })


def snippet(request, cid: int, rel: str, page: int):
    cdir = _course_dir_for(cid)
    pdf_path = (cdir / rel).resolve()
    if not str(pdf_path).startswith(str(cdir.resolve())) or not pdf_path.exists():
        raise Http404
    sys.path.insert(0, str(ROOT))
    from snippets import render_page
    try:
        png = render_page(pdf_path, page, cdir)
    except Exception as e:
        return HttpResponse(f"render failed: {e}", status=500)
    return FileResponse(open(png, "rb"), content_type="image/png")


@require_POST
def solve_problem(request, cid: int):
    cdir = _course_dir_for(cid)
    problem = request.POST.get("problem", "").strip()
    topic = request.POST.get("topic", "").strip() or None
    if not problem:
        return HttpResponse("missing problem", status=400)
    sys.path.insert(0, str(ROOT))
    from solver import solve
    result = solve(problem, cdir, topic)
    return HttpResponse(
        json.dumps(result, indent=2),
        content_type="application/json",
    )


def ask(request, cid: int):
    cdir = _course_dir_for(cid)
    q = request.GET.get("q", "").strip()
    k = int(request.GET.get("k", 5))
    cat = request.GET.get("category", "").strip()
    results = []
    if q and (cdir / "chroma").exists():
        sys.path.insert(0, str(ROOT))
        from vectorize import get_collection
        coll = get_collection(cdir)
        where = {"category": cat} if cat else None
        res = coll.query(query_texts=[q], n_results=k, where=where)
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            results.append({"doc": doc, "meta": meta, "dist": round(dist, 3)})
    return render(request, "ui/ask.html", {"cid": cid, "q": q, "k": k, "cat": cat, "results": results})
