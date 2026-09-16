from __future__ import annotations

import json
import os
import shlex
import re
import subprocess
import sys
from urllib.parse import urlparse
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
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



def _chunk_label(meta: dict) -> str:
    """Header the model sees above a retrieved chunk.

    An email is cited by subject and send date, a lecture by the date it was
    recorded; "_email/messages/2026-09-09_1a08.txt p1" tells the model nothing.
    """
    src = meta.get("source", "?")
    cat = meta.get("category", "?")
    if cat in {"email", "email_attachment"} and meta.get("subject"):
        sent = (meta.get("sent") or "")[:10]
        return f"EMAIL {sent} \u2014 {meta['subject']} ({meta.get('sender','')}) [{src}]"
    if cat == "panopto_transcript" and meta.get("lecture_date"):
        return f"LECTURE RECORDING {meta['lecture_date']} [{src}]"
    return f"{src} p{meta.get('page','?')} ({cat})"


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
def google_auth_status(request):
    """JSON: whether Google Drive session is valid."""
    sys.path.insert(0, str(ROOT))
    try:
        from google_auth import COOKIE_FILE, _drive_session_valid
    except ImportError:
        return HttpResponse(json.dumps({"valid": False, "reason": "google_auth missing"}),
                            content_type="application/json")
    if not COOKIE_FILE.exists():
        return HttpResponse(json.dumps({"valid": False, "reason": "no cookies — run google_auth"}),
                            content_type="application/json")
    try:
        raw = json.loads(COOKIE_FILE.read_text())
        valid = _drive_session_valid(raw)
        return HttpResponse(json.dumps({"valid": bool(valid), "cookies": len(raw)}),
                            content_type="application/json")
    except Exception as e:
        return HttpResponse(json.dumps({"valid": False, "reason": str(e)[:200]}),
                            content_type="application/json")


@require_POST
def google_auth_run(request):
    """Launch Playwright Google login in background. Returns immediately."""
    log = "/tmp/google_auth.log"
    subprocess.Popen(
        [PYTHON, "google_auth.py"],
        cwd=ROOT,
        stdout=open(log, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    messages.info(request,
        "🔐 Google login browser launched. Complete ND SSO + Duo in the window. "
        f"Cookies auto-save when valid. Watch log: tail -f {log}")
    return redirect(request.META.get("HTTP_REFERER", "/"))


@require_POST
def external_scrape_run(request, cid: int):
    """Manually-triggered external-site scrape with user-supplied URL."""
    cdir = _course_dir_for(cid)
    if not cdir:
        return HttpResponse("course dir missing", status=400)
    url = request.POST.get("url", "").strip()
    google = request.POST.get("google", "").strip() in ("1", "on", "true")
    if not url and not google:
        messages.error(request, "Provide a URL or check 'Also pull Google Drive links'.")
        return redirect("ui:course_detail", cid=cid)
    # This value reaches a shell. Anything but an http(s) URL is rejected, and
    # what survives is still quoted below.
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            messages.error(request, "URL must start with http:// or https://.")
            return redirect("ui:course_detail", cid=cid)
    log = f"/tmp/external_scrape_{cid}.log"
    q_cdir = shlex.quote(str(cdir))
    cmd_parts = []
    if url:
        cmd_parts.append(
            f"{PYTHON} external_scrape.py --course-dir {q_cdir} "
            f"--url {shlex.quote(url)} --max-pages 200"
        )
    if google:
        cmd_parts.append(
            f"{PYTHON} google_scrape.py --course-dir {q_cdir} --from-canvas"
        )
    chain = "; ".join(cmd_parts) + f"; {PYTHON} bundle.py --course-dir {q_cdir}; " \
            f"{PYTHON} vectorize.py --course-dir {q_cdir} --rebuild || true"
    subprocess.Popen(
        ["bash", "-c", chain],
        cwd=ROOT,
        stdout=open(log, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    messages.info(request,
        f"External scrape started. Watch log: tail -f {log}")
    return redirect("ui:course_detail", cid=cid)


def sync_course(request, cid: int):
    name = request.POST.get("name", "").strip() or None
    cdir = _course_dir_for(cid) if any(d.name.startswith(f"{cid}_") for d in DOWNLOADS.iterdir() if d.is_dir()) else None
    cdir_str = str(cdir) if cdir else ""

    log_path = f"/tmp/sync_run_{cid}.log"
    # Run full pipeline: download → bundle → ocr (vertex+tesseract) → bundle
    # → problems → analyze → likelihood → render_graph → class_info
    project = os.environ.get("GCP_PROJECT", "nd-canvas-ocr-1777818991")
    # Also shell-bound, so quote it rather than trusting the form field.
    name_arg = ["--name", shlex.quote(name)] if name else []
    chain = (
        f"set -e; cd {ROOT}; "
        f"echo '=== [1/10] Canvas sync ==='; "
        f"{PYTHON} download.py --course {cid} {' '.join(name_arg)}; "
        f"CDIR=$({PYTHON} -c 'import sys,pathlib; "
        f"d=[p for p in pathlib.Path(\"downloads\").iterdir() if p.name.startswith(f\"{cid}_\")]; "
        f"print(d[0]) if d else sys.exit(1)'); "
        f"echo \"course dir: $CDIR\"; "
        f"echo '=== [2/10] External links (per-link classify + scrape) ==='; "
        f"{PYTHON} external_scrape.py --course-dir \"$CDIR\" --auto-all --max-pages 200 2>&1 | tee /tmp/external_scrape_{cid}.log || true; "
        f"echo '=== [3/10] Class info (initial pass — Canvas + scraped pages) ==='; "
        f"{PYTHON} class_info.py --course-dir \"$CDIR\" --rebuild || true; "
        f"echo '=== [4/10] Bundle (initial) ==='; "
        f"{PYTHON} bundle.py --course-dir \"$CDIR\"; "
        f"echo '=== [5/10] OCR Tesseract (fallback) ==='; "
        f"{PYTHON} ocr.py --course-dir \"$CDIR\" 2>&1 | tee /tmp/ocr_tesseract_{cid}.log || true; "
        f"echo '=== [6/10] OCR Vertex Gemini (high-quality) ==='; "
        f"{PYTHON} ocr_vertex.py --course-dir \"$CDIR\" --project {shlex.quote(project)} "
        f"  --model gemini-2.5-pro --workers 6 --rpm 5 "
        f"  --locations us-central1,us-east5,us-west4 2>&1 | tee /tmp/ocr_vertex_{cid}.log || true; "
        f"echo '=== [7/10] Re-bundle with OCR text ==='; "
        f"{PYTHON} bundle.py --course-dir \"$CDIR\"; "
        f"echo '=== [8/10] Extract problems + analyze + likelihood ==='; "
        f"{PYTHON} problems.py --course-dir \"$CDIR\"; "
        f"{PYTHON} analyze.py --course-dir \"$CDIR\"; "
        f"{PYTHON} likelihood.py --course-dir \"$CDIR\"; "
        f"echo '=== [9/10] Vector index (RAG for chat) ==='; "
        f"{PYTHON} vectorize.py --course-dir \"$CDIR\" --rebuild || true; "
        f"echo '=== [10/10] Render graph + class info ==='; "
        f"{PYTHON} render_graph.py --course-dir \"$CDIR\" || true; "
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
        "Stages: download → external scrape (auto-classify) → class info → "
        "bundle → OCR → re-bundle → problems → analyze → vectorize → "
        "graph + class info refresh. "
        f"Sync page shows Canvas progress only; full log: tail -f /tmp/sync_run_{cid}.log")
    return redirect("ui:sync_view", cid=cid)


def _materials_for_topic(cdir: Path, topic: str, cid: int | None = None) -> list[dict]:
    """Find PDFs that cover this topic.

    For stats: chapter folder match. For comp arch: lecture # → topic via
    comp_arch_topic_map. For unknown courses: empty list.
    """
    sys.path.insert(0, str(ROOT))
    from topic_graph import for_course
    if cid is None:
        # Sniff from dir name "<cid>_<slug>"
        for part in cdir.name.split("_"):
            if part.isdigit():
                cid = int(part); break
    graph = for_course(cid) if cid else {}
    info = graph.get(topic, {})

    out: list[dict] = []

    # Comp arch: lecture-number → topic
    if cid == 130417:
        from comp_arch_topic_map import LECTURE_TO_TOPIC, lecture_num_from_name
        slides_dir = cdir / "_external/google_drive_local/slides"
        if slides_dir.exists():
            for pdf in sorted(slides_dir.glob("*.pdf")):
                n = lecture_num_from_name(pdf.name)
                if n is not None and LECTURE_TO_TOPIC.get(n) == topic:
                    out.append({
                        "rel": str(pdf.relative_to(cdir)),
                        "name": pdf.name,
                        "kind": "in_class",
                    })
        return out

    # Stats: chapter folder match
    ch = info.get("ch")
    if not ch:
        return []
    modules_root = cdir / "modules"
    if not modules_root.exists():
        return []
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
    _internal = {"_ocr", "_pages", "_shards", "_snippets", "_attempts", "chroma"}
    files = [
        {"rel": str(p.relative_to(cdir)), "size": p.stat().st_size}
        for p in sorted(cdir.rglob("*.pdf"))
        if p.is_file() and not any(part in _internal for part in p.parts)
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
    from topic_graph import for_course
    GRAPH = for_course(cid)

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
        r["materials"] = _materials_for_topic(cdir, t, cid=cid)
        enriched.append(r)
    gap_top = enriched

    # Always show the full topic graph as a sequential study path, even if
    # no extracted problems yet (lets you start studying before exam-side
    # corpus exists).
    sequential_topics = []
    if GRAPH:
        from topic_graph import topo_sort
        for t in topo_sort(GRAPH):
            info = GRAPH[t]
            done = len(progress["done"].get(t, []))
            total = len(problems_by_topic.get(t, []))
            sequential_topics.append({
                "key": t, "label": info["label"], "chapter": info["ch"],
                "done": done, "total": total,
                "pct": int(round(100 * done / max(total, 1))) if total else 0,
                "has_summary": (cdir / "bundles" / "topics" / f"{t}.md").exists(),
            })

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
        "sequential_topics": sequential_topics,
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
    # Reject internal cache directories that happen to end in .pdf
    forbidden = {"_ocr", "_pages", "_shards", "_snippets", "_attempts", "chroma"}
    if any(part in forbidden for part in target.parts):
        raise Http404("internal cache, not a real file")
    if target.is_dir():
        raise Http404("not a file")
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
    if problems_file.exists():
        data = json.loads(problems_file.read_text())
    else:
        # No problems.json yet (e.g. comp arch — slides only). Show summary +
        # materials only, no problem cards.
        data = {}
    all_problems = list(data.get(topic, []))

    sys.path.insert(0, str(ROOT))
    from topic_graph import for_course
    _GRAPH_TMP = for_course(cid)

    expected_ch = _GRAPH_TMP.get(topic, {}).get("ch")

    # Chapter filter:
    #   default = topic chapter + cross-chapter exams (chapter=None) confirmed
    #             by body classifier; this is the recommended view.
    #   strict  = ONLY problems whose source folder is the topic chapter.
    #   all     = no filter (every problem in topic).
    #   <int>   = exact chapter match.
    ch_filter = request.GET.get("ch", "default")
    chapters_present = sorted({p.get("chapter") for p in all_problems if p.get("chapter")})
    has_uncategorized = any(p.get("chapter") is None for p in all_problems)
    if ch_filter == "all":
        problems = all_problems
    elif ch_filter == "strict":
        if expected_ch is None:
            problems = all_problems
        else:
            problems = [p for p in all_problems if p.get("chapter") == expected_ch]
    elif ch_filter == "default":
        if expected_ch is None:
            problems = all_problems
        else:
            problems = [
                p for p in all_problems
                if p.get("chapter") == expected_ch or p.get("chapter") is None
            ]
    else:
        try:
            want = int(ch_filter)
            problems = [p for p in all_problems if p.get("chapter") == want]
        except ValueError:
            problems = all_problems

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

    from topic_graph import prereqs_of, dependents_of
    GRAPH = _GRAPH_TMP

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
    attempts_map = progress.get("attempts", {})
    starred_keys = {s["key"] for s in progress.get("saved", [])}

    def _key(p):
        return f"{p['source']}#p{p['page']}#{p['problem']}"

    annotated = []
    for p in problems:
        ap = dict(p)
        ap["key"] = _key(p)
        ap["done"] = ap["key"] in done_keys
        ap["starred"] = ap["key"] in starred_keys
        saved = attempts_map.get(ap["key"], {})
        ap["saved_attempt"] = saved.get("attempt", "")
        ap["saved_solve"] = saved.get("solve", "")
        ap["saved_grade"] = saved.get("grade", "")
        annotated.append(ap)
    done_count = sum(1 for p in annotated if p["done"])

    materials = _materials_for_topic(cdir, topic)
    # Annotate with rendered URLs + OCR cache key
    for m in materials:
        rel = m["rel"]
        m["pdf_url"] = reverse("ui:serve_file", args=[cid, rel])
        m["ocr_key"] = rel.replace("/", "__") + ".txt"
        m["has_ocr"] = (cdir / "_ocr" / m["ocr_key"]).exists()
        m["snippet1_url"] = reverse("ui:snippet", args=[cid, rel, 1])

    # Topic-level audio (podcast version)
    audio_file = cdir / "bundles" / "audio" / f"{topic}.mp3"
    audio_url = (reverse("ui:topic_audio", args=[cid, topic])
                 if audio_file.exists() else None)
    audio_size_mb = (round(audio_file.stat().st_size / 1024 / 1024, 1)
                     if audio_file.exists() else 0)

    fu_file = cdir / "bundles" / "topics" / f"{topic}.followups.json"
    try:
        saved_followups = json.loads(fu_file.read_text()) if fu_file.exists() else []
    except Exception:
        saved_followups = []

    return render(request, "ui/topic.html", {
        "cid": cid,
        "topic": topic,
        "problems": annotated,
        "info": info,
        "saved_followups": saved_followups,
        "prereqs": [(k, GRAPH[k]) for k in prereqs_of(topic, GRAPH) if k in GRAPH],
        "dependents": [(k, GRAPH[k]) for k in dependents_of(topic, GRAPH)],
        "summary_md": summary_md,
        "have_summary": bool(summary_md),
        "done_count": done_count,
        "total_count": len(annotated),
        "pct": int(round(100 * done_count / max(len(annotated), 1))),
        "materials": materials,
        "audio_url": audio_url,
        "audio_size_mb": audio_size_mb,
        "ch_filter": ch_filter,
        "expected_ch": expected_ch,
        "chapters_present": chapters_present,
        "has_uncategorized": has_uncategorized,
        "filtered_count": len(problems),
        "all_count": len(all_problems),
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
            from topic_graph import for_course
            GRAPH = for_course(cid)
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
    from topic_graph import mermaid, for_course
    GRAPH = for_course(cid)
    return render(request, "ui/graph.html", {
        "cid": cid,
        "mermaid": mermaid(GRAPH),
        "topics": GRAPH,
    })


@require_POST
@require_POST
def solution_followup(request, cid: int):
    """Follow-up question on a Solve/Grade answer. Uses prior solution as context."""
    cdir = _course_dir_for(cid)
    problem = request.POST.get("problem", "").strip()
    prior_answer = request.POST.get("prior_answer", "").strip()
    user_q = request.POST.get("question", "").strip()
    topic = request.POST.get("topic", "").strip() or None
    thinking = request.POST.get("thinking", "").strip() in ("1", "true", "on")
    if not user_q:
        return HttpResponse(json.dumps({"error": "missing question"}),
                            status=400, content_type="application/json")

    sys.path.insert(0, str(ROOT))
    from solver import _gather_context
    context = _gather_context(cdir, topic, max_chars=12000)

    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
    pro_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
    chosen = pro_model if thinking else fast_model

    prompt = (
        "You are a statistics tutor. The student already received a worked "
        "solution to a problem and now has a follow-up question. Answer the "
        "follow-up directly and concisely, building on (not repeating) the "
        "prior solution. Use Markdown + LaTeX. Stay under 300 words unless "
        "the follow-up explicitly requires more depth.\n\n"
        f"<reference>\n{context}\n</reference>\n\n"
        f"<problem>\n{problem}\n</problem>\n\n"
        f"<prior_solution>\n{prior_answer}\n</prior_solution>\n\n"
        f"<followup>\n{user_q}\n</followup>"
    )
    from gemini_client import generate
    try:
        text = generate(prompt, max_output_tokens=8192 if not thinking else 16384,
                        model=chosen)
    except Exception as e:
        return HttpResponse(json.dumps({"error": str(e)}),
                            status=500, content_type="application/json")
    return HttpResponse(json.dumps({"answer": text, "model": chosen}),
                        content_type="application/json")


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
    context = _gather_context(cdir, topic, max_chars=20000,
                              query=f"{selection}\n{user_q}".strip())

    if mode == "expand":
        focus = user_q or "more detail"
        instruction = (
            f"The student selected the snippet below and specifically wants: **{focus}**. "
            "Add ONLY content related to that request. Do NOT rewrite or contradict the "
            "selection — only add. Keep tightly scoped to what the student asked for. "
            "Use Markdown + LaTeX math ($...$ inline, $$...$$ block)."
        )
    elif mode == "grade":
        instruction = (
            "The student selected a QUESTION from the study guide and pasted their own "
            "answer to it (see STUDENT ANSWER below). Grade the answer against the course "
            "material. Structure your reply as: **Verdict:** correct / partially correct / "
            "incorrect (with an estimated score like 4/5 if the question has a point value "
            "visible); **What's right**; **What's missing or wrong**; **Model answer** "
            "(brief). Be strict but fair — reward correct reasoning, flag hand-waving. "
            "Use Markdown + LaTeX."
        )
    else:
        instruction = (
            "The student selected the snippet below and asks a clarifying follow-up question. "
            "Answer the question concisely and directly with respect to the selection. "
            "Cite specific formulas. Use Markdown + LaTeX."
        )

    prompt = (
        f"Topic: {topic}\n\n"
        f"--- TOPIC SUMMARY (for context) ---\n{summary_md}\n\n"
        f"--- COURSE REFERENCE ---\n{context}\n\n"
        f"--- SELECTED SNIPPET ---\n{selection}\n\n"
        f"--- TASK ---\n{instruction}"
    )
    if user_q and mode == "grade":
        prompt += f"\n\n--- STUDENT ANSWER ---\n{user_q}"
    elif user_q and mode != "expand":
        prompt += f"\n\n--- STUDENT QUESTION ---\n{user_q}"

    def _persist_followup(answer: str) -> None:
        # Survive page refresh: sidecar JSON, re-inserted inline at the anchor
        # paragraph by topic.html on load.
        fu_file = cdir / "bundles" / "topics" / f"{topic}.followups.json"
        try:
            items = json.loads(fu_file.read_text()) if fu_file.exists() else []
        except Exception:
            items = []
        items.append({"selection": selection, "mode": mode,
                      "question": user_q, "answer": answer.strip()})
        fu_file.parent.mkdir(parents=True, exist_ok=True)
        fu_file.write_text(json.dumps(items, indent=1))

    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "gemini":
        from gemini_client import generate
        try:
            text = generate(prompt, max_output_tokens=16384)
        except Exception as e:
            return HttpResponse(json.dumps({"error": str(e)}), status=500, content_type="application/json")
        _persist_followup(text)
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
    _persist_followup(proc.stdout.strip())
    return HttpResponse(json.dumps({"answer": proc.stdout.strip()}), content_type="application/json")


def _progress_path(cdir: Path) -> Path:
    return cdir / "bundles" / "progress.json"


def _load_progress(cdir: Path) -> dict:
    f = _progress_path(cdir)
    if not f.exists():
        return {"done": {}, "attempts": {}, "saved": [], "regrade_flags": {}}
    try:
        d = json.loads(f.read_text())
        d.setdefault("done", {})
        d.setdefault("attempts", {})
        d.setdefault("saved", [])
        d.setdefault("regrade_flags", {})  # key -> {status, notes, ts}
        return d
    except Exception:
        return {"done": {}, "attempts": {}, "saved": [], "regrade_flags": {}}


def _save_progress(cdir: Path, data: dict) -> None:
    _progress_path(cdir).parent.mkdir(parents=True, exist_ok=True)
    _progress_path(cdir).write_text(json.dumps(data, indent=2))


def progress_get(request, cid: int):
    cdir = _course_dir_for(cid)
    return HttpResponse(json.dumps(_load_progress(cdir)), content_type="application/json")


@require_POST
def save_attempt(request, cid: int):
    """Persist user's typed attempt + (optionally) latest Solve / Grade output."""
    cdir = _course_dir_for(cid)
    key = request.POST.get("key", "").strip()
    if not key:
        return HttpResponse(json.dumps({"error": "missing key"}),
                            status=400, content_type="application/json")
    data = _load_progress(cdir)
    bucket = data["attempts"].setdefault(key, {})
    for fld in ("attempt", "solve", "grade"):
        if fld in request.POST:
            val = request.POST.get(fld, "")
            if val == "":
                bucket.pop(fld, None)
            else:
                bucket[fld] = val
    if not bucket:
        data["attempts"].pop(key, None)
    _save_progress(cdir, data)
    return HttpResponse(json.dumps({"ok": True}), content_type="application/json")


@require_POST
def save_toggle(request, cid: int):
    """Star/unstar a problem. Persists in progress.json under 'saved'."""
    cdir = _course_dir_for(cid)
    key = request.POST.get("key", "").strip()
    state = request.POST.get("state", "").strip()  # "1" save, "0" unsave
    topic = request.POST.get("topic", "").strip()
    source = request.POST.get("source", "").strip()
    page = request.POST.get("page", "").strip()
    problem = request.POST.get("problem", "").strip()
    stem = request.POST.get("stem", "").strip()[:280]
    if not key:
        return HttpResponse(json.dumps({"error": "missing key"}),
                            status=400, content_type="application/json")
    data = _load_progress(cdir)
    saved = data["saved"]
    saved_keys = {item["key"] for item in saved}
    if state == "1":
        if key not in saved_keys:
            import time as _t
            saved.append({
                "key": key, "topic": topic, "source": source,
                "page": int(page) if page.isdigit() else None,
                "problem": int(problem) if problem.isdigit() else None,
                "stem": stem, "ts": int(_t.time()),
            })
    else:
        data["saved"] = [s for s in saved if s["key"] != key]
    _save_progress(cdir, data)
    return HttpResponse(json.dumps({"ok": True, "count": len(data["saved"])}),
                        content_type="application/json")


def _ensure_chats(progress: dict) -> dict:
    """Migrate legacy single chat_history → list of named chats."""
    if "chats" not in progress:
        progress["chats"] = {}
    # One-time migration of old single chat_history blob
    if progress.get("chat_history") and not progress["chats"]:
        import time as _t
        cid = f"chat-{int(_t.time())}"
        progress["chats"][cid] = {
            "id": cid,
            "title": "Imported history",
            "created": int(_t.time()),
            "updated": int(_t.time()),
            "messages": progress["chat_history"],
        }
        progress.pop("chat_history", None)
    return progress["chats"]


CHAT_PAGE_SIZE = 30  # messages shown per render window


def chat_view(request, cid: int):
    """RAG chat over the course corpus. Supports multiple chat sessions."""
    cdir = _course_dir_for(cid)
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    _save_progress(cdir, progress)

    chat_id = request.GET.get("chat_id", "").strip()
    chat_list = sorted(chats.values(), key=lambda c: -c.get("updated", 0))
    active = None
    if chat_id and chat_id in chats:
        active = chats[chat_id]
    elif chat_list:
        active = chat_list[0]

    # Pagination: only render the LAST N messages on initial load
    all_msgs = active["messages"] if active else []
    total = len(all_msgs)
    visible = all_msgs[-CHAT_PAGE_SIZE:] if total > CHAT_PAGE_SIZE else all_msgs
    has_older = total > len(visible)

    return render(request, "ui/chat.html", {
        "cid": cid,
        "chats": chat_list,
        "active": active,
        "history": visible,
        "active_id": active["id"] if active else "",
        "has_chroma": (cdir / "chroma").exists(),
        "total_messages": total,
        "has_older": has_older,
        "next_offset": max(0, total - len(visible)),  # index of oldest visible
    })


def chat_history_page(request, cid: int):
    """Return older messages in JSON for lazy 'Load earlier' button."""
    cdir = _course_dir_for(cid)
    chat_id = request.GET.get("chat_id", "").strip()
    try:
        before_index = int(request.GET.get("before", "0"))
    except ValueError:
        before_index = 0
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    if chat_id not in chats:
        return HttpResponse(json.dumps({"error": "no chat"}),
                            status=404, content_type="application/json")
    msgs = chats[chat_id]["messages"]
    # Return CHAT_PAGE_SIZE messages BEFORE before_index
    start = max(0, before_index - CHAT_PAGE_SIZE)
    page = msgs[start:before_index]
    return HttpResponse(json.dumps({
        "ok": True,
        "messages": page,
        "next_offset": start,
        "has_older": start > 0,
    }), content_type="application/json")


@require_POST
def chat_new(request, cid: int):
    """Create a new empty chat. Returns its id."""
    cdir = _course_dir_for(cid)
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    import time as _t
    new_id = f"chat-{int(_t.time() * 1000)}"
    chats[new_id] = {
        "id": new_id,
        "title": "New chat",
        "created": int(_t.time()),
        "updated": int(_t.time()),
        "messages": [],
    }
    _save_progress(cdir, progress)
    return HttpResponse(json.dumps({"ok": True, "chat_id": new_id}),
                        content_type="application/json")


@require_POST
def chat_delete(request, cid: int):
    cdir = _course_dir_for(cid)
    chat_id = request.POST.get("chat_id", "").strip()
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    if chat_id in chats:
        chats.pop(chat_id)
        _save_progress(cdir, progress)
    return HttpResponse(json.dumps({"ok": True}), content_type="application/json")


@require_POST
def chat_rename(request, cid: int):
    cdir = _course_dir_for(cid)
    chat_id = request.POST.get("chat_id", "").strip()
    title = request.POST.get("title", "").strip()[:80]
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    if chat_id in chats and title:
        chats[chat_id]["title"] = title
        _save_progress(cdir, progress)
    return HttpResponse(json.dumps({"ok": True}), content_type="application/json")


@require_POST
def chat_ask(request, cid: int):
    """Answer a question with RAG-retrieved context. Persists conversation."""
    cdir = _course_dir_for(cid)
    user_q = request.POST.get("question", "").strip()
    thinking = request.POST.get("thinking", "").strip() in ("1", "true", "on")
    chat_id = request.POST.get("chat_id", "").strip()
    # 12, not 8: a compound question ("when is HW2 due AND what did he say
    # in lecture about X") needs room for both kinds of source.
    k = int(request.POST.get("k", "12"))
    uploaded = list(request.FILES.getlist("attachment"))
    if not user_q and not uploaded:
        return HttpResponse(json.dumps({"error": "missing question or attachment"}),
                            status=400, content_type="application/json")
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    # Auto-create if no chat_id given
    if not chat_id or chat_id not in chats:
        import time as _t
        chat_id = f"chat-{int(_t.time() * 1000)}"
        chats[chat_id] = {
            "id": chat_id, "title": "New chat",
            "created": int(_t.time()), "updated": int(_t.time()),
            "messages": [],
        }
    chat = chats[chat_id]
    history = chat["messages"]

    # Save uploaded files under bundles/_chat_attachments/<chat_id>/
    attachment_paths: list[str] = []
    attachment_meta: list[dict] = []
    if uploaded:
        import time as _t, uuid
        att_dir = cdir / "bundles" / "_chat_attachments" / chat_id
        att_dir.mkdir(parents=True, exist_ok=True)
        for f in uploaded:
            ext = (f.name.rsplit(".", 1)[-1] or "bin").lower()[:5]
            safe_ext = "".join(c for c in ext if c.isalnum())
            dest = att_dir / f"{int(_t.time())}_{uuid.uuid4().hex[:8]}.{safe_ext or 'bin'}"
            with open(dest, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            attachment_paths.append(str(dest))
            attachment_meta.append({
                "name": f.name, "size": f.size,
                "stored": str(dest.relative_to(cdir)),
            })

    # Retrieve top-k chunks
    sources_used: list[dict] = []
    context_blocks: list[str] = []
    if (cdir / "chroma").exists():
        try:
            sys.path.insert(0, str(ROOT))
            from vectorize import multi_search as vsearch
            for row in vsearch(cdir, user_q, k=k):
                doc, meta, dist = row["text"], row["meta"], row["dist"]
                sources_used.append({
                    "source": meta.get("source", "?"),
                    "page": meta.get("page", 0),
                    "category": meta.get("category", ""),
                    # None when the chunk came from the keyword index only.
                    "dist": round(float(dist), 3) if dist is not None else None,
                })
                context_blocks.append(
                    f"=== {_chunk_label(meta)} ===\n{doc}"
                )
        except Exception as e:
            sources_used.append({"source": "(retrieval error)",
                                 "page": 0, "category": "", "dist": 0,
                                 "error": str(e)[:200]})

    # Always include formula sheet (stats) OR final exam guidelines (paradigms/comp arch)
    formulas_pdf = next(cdir.glob("modules/**/*formulas*.pdf"), None)
    if formulas_pdf:
        ocr_path = cdir / "_ocr" / (
            str(formulas_pdf.relative_to(cdir)).replace("/", "__") + ".txt"
        )
        if ocr_path.exists():
            context_blocks.insert(
                0, "=== FORMULA SHEET (authoritative for the final) ===\n"
                + ocr_path.read_text()[:8000]
            )
    # Final exam guidelines doc (paradigms course)
    for guide in cdir.glob("_external/**/*exam*guide*.txt"):
        context_blocks.insert(
            0, f"=== FINAL EXAM GUIDELINES ({guide.name}) ===\n"
            + guide.read_text(errors="ignore")[:8000]
        )
        break
    for guide in cdir.glob("_external/google_drive_remote/document/16w-Wm7rCsRCH6QUiSdMwRirSe-wM71-BOpmjORygakg.txt"):
        context_blocks.insert(
            0, "=== FINAL EXAM GUIDELINES (paradigms) ===\n"
            + guide.read_text(errors="ignore")[:8000]
        )

    # Include last 3 turns for continuity
    history_excerpt = ""
    for turn in history[-6:]:
        role = "Student" if turn["role"] == "user" else "Tutor"
        history_excerpt += f"{role}: {turn['content'][:600]}\n\n"

    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
    pro_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
    chosen = pro_model if thinking else fast_model

    _COURSE_PROFILES = {
        128781: ("ACMS 30440 statistics tutor",
                 "the final exam (May 7, 2026)",
                 "Use Markdown + LaTeX for math."),
        129492: ("CSE 30332 Programming Paradigms tutor",
                 "the final exam (May 8, 2026) covering JS, Python/Django, Java, Clojure, paradigms (OO, functional, declarative)",
                 "Use Markdown + fenced code blocks. Show short runnable snippets."),
        130417: ("CSE 30321 Computer Architecture tutor",
                 "the final exam covering RISC-V, pipelining, caches, virtual memory, ILP, branch prediction",
                 "Use Markdown. Show RISC-V assembly + bit-level diagrams when relevant."),
        139705: ("CSE 40113 Design/Analysis of Algorithms tutor (Prof. Danny Chen, CLRS 4th ed.)",
                 "the in-class midterm (Oct 13, 2026) and the final (Dec 11, 2026), covering "
                 "induction, asymptotic notation, recurrences, divide-and-conquer, data structures, "
                 "greedy algorithms, dynamic programming, graph algorithms, and NP-completeness",
                 "Use Markdown + LaTeX for math and fenced blocks for pseudocode. Course material "
                 "arrives mostly by email and in the Panopto lecture recordings; cite the email "
                 "subject and date, or the lecture date, rather than a file path."),
    }
    role, exam_ctx, fmt_hint = _COURSE_PROFILES.get(int(cid),
        ("Notre Dame course tutor", "the final exam", "Use Markdown."))
    system_prompt = (
        f"You are a {role}. Notre Dame student studying for {exam_ctx}. "
        f"Answer grounded in course material. Cite source files when relevant. "
        f"{fmt_hint} Do not invent.\n\n"
        "TOOLS AVAILABLE: you can call structured tools to query the student's "
        "personalized data: ranked exam topics, under-prepared topics, problems "
        "per topic, deep summaries, Gradescope mistakes, saved problems, class "
        "info, formula sheet. Use them when the question asks about coverage, "
        "ranking, what to study, mistakes, or course-specific data. Otherwise "
        "answer directly from RAG context. Cite tool results in your answer."
    )
    user_prompt = (
        f"<conversation_history>\n{history_excerpt or '(none)'}\n</conversation_history>\n\n"
        f"<retrieved_course_context>\n"
        + ("\n\n---\n\n".join(context_blocks)[:25000])
        + "\n</retrieved_course_context>\n\n"
        f"<question>\n{user_q}\n</question>"
    )

    sys.path.insert(0, str(ROOT))
    tool_calls_made: list[dict] = []
    try:
        from chat_tools import vertex_tool_declarations, call_tool
        from vertexai.generative_models import GenerativeModel, Part, Content, GenerationConfig
        import vertexai
        project = os.environ.get("GCP_PROJECT")
        location = os.environ.get("GCP_LOCATIONS", "us-central1").split(",")[0].strip()
        vertexai.init(project=project, location=location)
        tool_obj = vertex_tool_declarations()
        model = GenerativeModel(chosen, tools=[tool_obj], system_instruction=system_prompt)
        chat_session = model.start_chat()
        # Build first-turn parts: attachments + text
        first_parts: list = []
        if attachment_paths:
            import mimetypes
            for ap in attachment_paths:
                with open(ap, "rb") as fh:
                    data = fh.read()
                guessed, _ = mimetypes.guess_type(ap)
                if not guessed:
                    if data[:4] == b"%PDF":
                        guessed = "application/pdf"
                    elif data[:3] == b"\xff\xd8\xff":
                        guessed = "image/jpeg"
                    elif data[:8] == b"\x89PNG\r\n\x1a\n":
                        guessed = "image/png"
                    else:
                        guessed = "application/octet-stream"
                first_parts.append(Part.from_data(data=data, mime_type=guessed))
        first_parts.append(user_prompt)
        resp = chat_session.send_message(
            first_parts,
            generation_config=GenerationConfig(
                max_output_tokens=8192 if not thinking else 16384,
                temperature=0.3,
            ),
        )
        # Tool-call loop (max 5 iterations to prevent runaway)
        for _ in range(5):
            calls = []
            for cand in resp.candidates:
                for part in cand.content.parts:
                    fn = getattr(part, "function_call", None)
                    if fn and fn.name:
                        args = dict(fn.args) if fn.args else {}
                        calls.append((fn.name, args))
            if not calls:
                break
            tool_responses = []
            for name, args in calls:
                result = call_tool(name, args, cdir)
                tool_calls_made.append({"name": name, "args": args,
                                        "result_preview": json.dumps(result)[:200]})
                tool_responses.append(Part.from_function_response(
                    name=name, response={"content": result}
                ))
            resp = chat_session.send_message(tool_responses,
                generation_config=GenerationConfig(
                    max_output_tokens=8192 if not thinking else 16384,
                    temperature=0.3,
                ),
            )
        # Extract final text
        text_parts = []
        for cand in resp.candidates:
            for part in cand.content.parts:
                t = getattr(part, "text", None)
                if t:
                    text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            text = "(empty response from model)"
    except Exception as e:
        return HttpResponse(json.dumps({"error": str(e)}),
                            status=500, content_type="application/json")

    # Persist turn
    history.append({
        "role": "user", "content": user_q,
        "attachments": attachment_meta,
    })
    history.append({
        "role": "assistant", "content": text,
        "model": chosen, "sources": sources_used[:k],
        "tool_calls": tool_calls_made,
    })
    import time as _t
    chat["updated"] = int(_t.time())
    # Auto-title from first user message
    if chat["title"] in ("New chat", "Imported history") or not chat["title"].strip():
        chat["title"] = (user_q[:60] + ("…" if len(user_q) > 60 else "")).strip()
    _save_progress(cdir, progress)

    return HttpResponse(json.dumps({
        "ok": True, "answer": text, "model": chosen,
        "sources": sources_used, "chat_id": chat_id, "title": chat["title"],
        "tool_calls": tool_calls_made,
        "attachments": attachment_meta,
    }), content_type="application/json")


@require_POST
def chat_ask_stream(request, cid: int):
    """SSE-stream Gemini reply token-by-token. Skips tool-calls for speed.

    Frontend uses for continuous-voice mode: fast first-token + chunk-by-chunk
    TTS playback as text streams in.
    """
    cdir = _course_dir_for(cid)
    user_q = request.POST.get("question", "").strip()
    chat_id = request.POST.get("chat_id", "").strip()
    k = int(request.POST.get("k", "6"))
    if not user_q:
        return HttpResponse(json.dumps({"error": "missing question"}),
                            status=400, content_type="application/json")
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    if not chat_id or chat_id not in chats:
        import time as _t
        chat_id = f"chat-{int(_t.time() * 1000)}"
        chats[chat_id] = {
            "id": chat_id, "title": "Voice chat",
            "created": int(_t.time()), "updated": int(_t.time()),
            "messages": [],
        }
    chat = chats[chat_id]
    history = chat["messages"]

    # RAG retrieval
    context_blocks: list[str] = []
    if (cdir / "chroma").exists():
        try:
            sys.path.insert(0, str(ROOT))
            from vectorize import search as vsearch
            for row in vsearch(cdir, user_q, k=k):
                context_blocks.append(
                    f"=== {_chunk_label(row['meta'])} ===\n{row['text']}"
                )
        except Exception:
            pass

    _COURSE_PROFILES = {
        128781: ("ACMS 30440 statistics tutor", "the final exam"),
        129492: ("CSE 30332 Programming Paradigms tutor", "the final exam"),
        130417: ("CSE 30321 Computer Architecture tutor", "the final exam"),
        139705: ("CSE 40113 Design/Analysis of Algorithms tutor (Prof. Danny Chen)",
                 "the in-class midterm on October 13 and the final on December 11"),
    }
    role, exam_ctx = _COURSE_PROFILES.get(int(cid),
        ("Notre Dame course tutor", "the final exam"))
    system_prompt = (
        f"You are a {role}. Notre Dame student studying for {exam_ctx}. "
        f"VOICE MODE: keep replies SHORT (under 120 words), conversational, "
        f"no markdown tables/code blocks/LaTeX (will be read aloud). "
        f"Plain prose. Cite material naturally."
    )
    history_excerpt = ""
    for turn in history[-6:]:
        role_t = "Student" if turn["role"] == "user" else "Tutor"
        history_excerpt += f"{role_t}: {turn['content'][:300]}\n\n"
    user_prompt = (
        f"<conversation_history>\n{history_excerpt or '(none)'}\n</conversation_history>\n\n"
        f"<retrieved_course_context>\n"
        + ("\n\n---\n\n".join(context_blocks)[:15000])
        + "\n</retrieved_course_context>\n\n"
        f"<question>\n{user_q}\n</question>"
    )

    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")

    def event_stream():
        sys.path.insert(0, str(ROOT))
        try:
            from vertexai.generative_models import GenerativeModel, GenerationConfig
            import vertexai
            project = os.environ.get("GCP_PROJECT")
            location = os.environ.get("GCP_LOCATIONS", "us-central1").split(",")[0].strip()
            vertexai.init(project=project, location=location)
            model = GenerativeModel(fast_model, system_instruction=system_prompt)
            stream = model.generate_content(
                user_prompt,
                generation_config=GenerationConfig(
                    max_output_tokens=2048, temperature=0.4,
                ),
                stream=True,
            )
            full_text = ""
            yield f"event: meta\ndata: {json.dumps({'chat_id': chat_id, 'model': fast_model})}\n\n"
            for chunk in stream:
                try:
                    txt = chunk.text or ""
                except Exception:
                    txt = ""
                if txt:
                    full_text += txt
                    yield f"event: chunk\ndata: {json.dumps({'text': txt})}\n\n"
            # Persist
            history.append({"role": "user", "content": user_q, "attachments": []})
            history.append({"role": "assistant", "content": full_text,
                            "model": fast_model, "sources": [], "tool_calls": []})
            import time as _t
            chat["updated"] = int(_t.time())
            if chat["title"] in ("New chat", "Voice chat", "Imported history") or not chat["title"].strip():
                chat["title"] = (user_q[:60] + ("…" if len(user_q) > 60 else "")).strip()
            _save_progress(cdir, progress)
            yield f"event: done\ndata: {json.dumps({'full_text': full_text})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)[:300]})}\n\n"

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@require_POST
def chat_clear(request, cid: int):
    """Clear messages of a specific chat (or all if no chat_id given)."""
    cdir = _course_dir_for(cid)
    chat_id = request.POST.get("chat_id", "").strip()
    progress = _load_progress(cdir)
    chats = _ensure_chats(progress)
    if chat_id and chat_id in chats:
        chats[chat_id]["messages"] = []
    elif not chat_id:
        progress["chats"] = {}
    _save_progress(cdir, progress)
    return HttpResponse(json.dumps({"ok": True}), content_type="application/json")


def topic_pdf(request, cid: int, topic: str):
    """Render topic summary as printable HTML (browser Cmd+P → PDF).

    True PDF generation needs weasyprint/wkhtmltopdf. We instead serve a
    print-styled HTML page and let the browser export it via "Save as PDF".
    """
    cdir = _course_dir_for(cid)
    md_file = cdir / "bundles" / "topics" / f"{topic}.md"
    if not md_file.exists():
        return HttpResponse("topic summary not generated", status=404)
    md_text = md_file.read_text()
    sys.path.insert(0, str(ROOT))
    from topic_graph import for_course
    info = for_course(cid).get(topic, {})
    label = info.get("label", topic)
    try:
        from markdown_it import MarkdownIt
        md_parser = (
            MarkdownIt("commonmark", {"html": False, "linkify": True})
            .enable("table").enable("strikethrough")
        )
        body = md_parser.render(md_text)
    except ImportError:
        import html as _h
        body = "<pre>" + _h.escape(md_text) + "</pre>"
    style = (
        "@page { size: letter; margin: 0.6in; }"
        "body { font-family: -apple-system, sans-serif; font-size: 11pt; line-height: 1.45; max-width: 7in; margin: 0 auto; }"
        "h1 { font-size: 18pt; border-bottom: 2px solid #1e3a5f; padding-bottom: 4pt; }"
        "h2 { font-size: 14pt; margin-top: 18pt; color: #1e3a5f; border-bottom: 1px solid #ccc; }"
        "h3 { font-size: 12pt; margin-top: 12pt; color: #3b5a82; }"
        "pre { background: #f8f5f0; padding: 8pt; font-size: 9.5pt; white-space: pre-wrap; "
        "      border: 1px solid #ddd; border-radius: 3pt; page-break-inside: avoid; }"
        "code { font-size: 10pt; background: #f8f5f0; padding: 0 3pt; }"
        "table { border-collapse: collapse; font-size: 10pt; width: 100%; margin: 6pt 0; }"
        "th, td { border: 1px solid #888; padding: 4pt 6pt; text-align: left; }"
        "th { background: #e9e5dc; }"
        "ul, ol { margin: 4pt 0; padding-left: 20pt; }"
        "li { margin-bottom: 2pt; }"
        ".meta { font-size: 9pt; color: #666; margin-bottom: 12pt; }"
        "@media print { .no-print { display: none; } }"
    )
    print_btn = (
        f'<div class="no-print" style="position:fixed; top:8px; right:8px; '
        f'background:#1e3a5f; color:#fff; padding:8px 14px; border-radius:6px; '
        f'cursor:pointer; font-family:sans-serif;" '
        f'onclick="window.print()">⬇ Save as PDF (Cmd+P)</div>'
    )
    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{label} — CSE study guide</title>"
        f"<style>{style}</style></head><body>"
        + print_btn
        + f'<div class="meta">Topic study guide · cid {cid} · '
          f'<a href="/course/{cid}/topic/{topic}/">interactive version</a></div>'
        + body
        + "</body></html>"
    )
    return HttpResponse(html_doc)


def cheatsheet_pdf(request, cid: int):
    """Serve cheatsheet. ?format=pdf → pre-rendered PDF; default → HTML viewer."""
    cdir = _course_dir_for(cid)
    if request.GET.get("format") == "pdf":
        pdf = cdir / "bundles" / "CHEATSHEET.pdf"
        if pdf.exists():
            resp = HttpResponse(pdf.read_bytes(), content_type="application/pdf")
            resp["Content-Disposition"] = f'inline; filename="cheatsheet_{cid}.pdf"'
            return resp
    f = cdir / "bundles" / "CHEATSHEET.html"
    if not f.exists():
        return HttpResponse("cheatsheet not generated", status=404)
    html = f.read_text()
    pdf_link = (f'<a class="no-print" href="?format=pdf" '
                'style="position:fixed; top:8px; right:160px; '
                'background:#444; color:#fff; padding:8px 14px; border-radius:6px; '
                'text-decoration:none;">⬇ Download PDF</a>')
    btn = ('<div class="no-print" style="position:fixed; top:8px; right:8px; '
           'background:#d97757; color:#fff; padding:8px 14px; border-radius:6px; '
           'cursor:pointer;" onclick="window.print()">🖨 Print (Cmd+P)</div>'
           '<style>@media print { .no-print { display: none; } }</style>')
    html = html.replace("<body>", "<body>" + pdf_link + btn, 1)
    return HttpResponse(html)


def topic_audio(request, cid: int, topic: str):
    """Serve generated podcast MP3 for a topic."""
    cdir = _course_dir_for(cid)
    f = cdir / "bundles" / "audio" / f"{topic}.mp3"
    if not f.exists():
        return HttpResponse("audio not generated", status=404)
    resp = HttpResponse(f.read_bytes(), content_type="audio/mpeg")
    resp["Content-Disposition"] = f'inline; filename="{topic}.mp3"'
    resp["Accept-Ranges"] = "bytes"
    return resp


def all_audio(request, cid: int):
    """Page listing all available topic audio files for download."""
    cdir = _course_dir_for(cid)
    audio_dir = cdir / "bundles" / "audio"
    sys.path.insert(0, str(ROOT))
    from topic_graph import for_course, topo_sort
    GRAPH = for_course(cid)
    files = []
    if audio_dir.exists():
        topics_order = topo_sort(GRAPH) if GRAPH else []
        for t in topics_order:
            mp3 = audio_dir / f"{t}.mp3"
            if mp3.exists():
                files.append({
                    "topic": t,
                    "label": GRAPH.get(t, {}).get("label", t),
                    "size_mb": round(mp3.stat().st_size / 1024 / 1024, 1),
                    "url": reverse("ui:topic_audio", args=[cid, t]),
                })
    # Mega-mix combined exam-priority podcast
    megamix_file = audio_dir / "_MEGAMIX_exam_review.mp3"
    megamix = None
    if megamix_file.exists():
        megamix = {
            "size_mb": round(megamix_file.stat().st_size / 1024 / 1024, 1),
            "url": reverse("ui:megamix_audio", args=[cid]),
        }
    return render(request, "ui/audio.html", {
        "cid": cid,
        "files": files,
        "megamix": megamix,
        "total_mb": round(sum(f["size_mb"] for f in files), 1),
    })


def megamix_audio(request, cid: int):
    """Serve the combined exam-review podcast MP3."""
    cdir = _course_dir_for(cid)
    f = cdir / "bundles" / "audio" / "_MEGAMIX_exam_review.mp3"
    if not f.exists():
        return HttpResponse("megamix not built", status=404)
    resp = HttpResponse(f.read_bytes(), content_type="audio/mpeg")
    resp["Content-Disposition"] = 'inline; filename="exam_review_megamix.mp3"'
    resp["Accept-Ranges"] = "bytes"
    return resp


def saved_problems(request, cid: int):
    """List all starred problems. Pulls fresh stem/full_body from problems.json."""
    cdir = _course_dir_for(cid)
    data = _load_progress(cdir)
    saved = data.get("saved", [])
    pfile = cdir / "bundles" / "problems.json"
    by_key: dict[str, dict] = {}
    if pfile.exists():
        problems_by_topic = json.loads(pfile.read_text())
        for topic, plist in problems_by_topic.items():
            for p in plist:
                k = f"{p['source']}#p{p['page']}#{p['problem']}"
                by_key[k] = {**p, "topic": topic}
    enriched = []
    for s in sorted(saved, key=lambda x: -x.get("ts", 0)):
        full = by_key.get(s["key"])
        if full:
            r = {**full, "saved_ts": s.get("ts"), "key": s["key"]}
        else:
            r = {**s, "saved_ts": s.get("ts"), "missing": True}
        enriched.append(r)
    sys.path.insert(0, str(ROOT))
    from topic_graph import for_course
    GRAPH = for_course(cid)
    return render(request, "ui/saved.html", {
        "cid": cid,
        "saved": enriched,
        "topic_labels": {k: v.get("label", k) for k, v in GRAPH.items()},
    })


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
    from topic_graph import for_course
    GRAPH = for_course(cid)

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

    thinking = request.POST.get("thinking", "").strip() in ("1", "true", "on")
    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
    pro_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
    chosen_model = pro_model if thinking else fast_model
    backend = os.environ.get("USE_BACKEND", "gemini")
    if backend == "gemini":
        sys.path.insert(0, str(ROOT))
        from gemini_client import generate, generate_with_images
        max_tok = 16384 if thinking else 8192
        try:
            if image_paths:
                text = generate_with_images(prompt, image_paths,
                                            max_output_tokens=max_tok,
                                            model=chosen_model)
            else:
                text = generate(prompt, max_output_tokens=max_tok,
                                model=chosen_model)
        except Exception as e:
            return HttpResponse(json.dumps({"error": str(e)}), status=500, content_type="application/json")
        return HttpResponse(json.dumps({"feedback": text, "images": len(image_paths),
                                        "model": chosen_model}),
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
def regrade_flag_toggle(request, cid: int):
    """Mark / unmark a mistake for regrade discussion. Persists notes + status."""
    cdir = _course_dir_for(cid)
    key = request.POST.get("key", "").strip()
    if not key:
        return HttpResponse(json.dumps({"error": "missing key"}),
                            status=400, content_type="application/json")
    state = request.POST.get("state", "").strip()  # "1" flag, "0" unflag
    notes = request.POST.get("notes", "").strip()[:2000]
    status = request.POST.get("status", "").strip().lower() or "open"
    if status not in ("open", "drafted", "sent", "resolved", "rejected"):
        status = "open"
    progress = _load_progress(cdir)
    flags = progress["regrade_flags"]
    if state == "0":
        flags.pop(key, None)
    else:
        import time as _t
        existing = flags.get(key, {})
        flags[key] = {
            "status": status,
            "notes": notes if notes else existing.get("notes", ""),
            "created": existing.get("created", int(_t.time())),
            "updated": int(_t.time()),
        }
    _save_progress(cdir, progress)
    return HttpResponse(json.dumps({"ok": True, "count": len(flags)}),
                        content_type="application/json")


def regrade_queue(request, cid: int):
    """Show all flagged mistakes — drafting board for emailing the prof."""
    cdir = _course_dir_for(cid)
    progress = _load_progress(cdir)
    flags = progress.get("regrade_flags", {})
    summary_file = cdir / "_gradescope" / "summary.json"
    summary = json.loads(summary_file.read_text()) if summary_file.exists() else {}
    explanations = progress.get("mistake_explanations", {})
    flagged_rows: list[dict] = []
    by_assignment: dict[str, list[dict]] = {}
    for a in summary.get("assignments", []):
        for w in a.get("wrong", []):
            key = f"{a['id']}#{w['question_id']}"
            if key not in flags:
                continue
            row = {
                **w,
                "key": key,
                "assignment_name": a["name"],
                "assignment_id": a["id"],
                "assignment_url": a.get("url", ""),
                "flag": flags[key],
                "ai_explanation": explanations.get(key, ""),
                "img_url": reverse("ui:mistake_image", args=[cid, a["id"], w["question_id"]]),
                "solution_img_url": reverse("ui:mistake_solution_image", args=[cid, a["id"], w["question_id"]]),
                "pct_lost": round(100 * w["lost"] / w["max_score"], 1) if w["max_score"] else 0,
            }
            flagged_rows.append(row)
            by_assignment.setdefault(a["name"], []).append(row)
    # Sort by status then -lost
    flagged_rows.sort(key=lambda r: (r["flag"]["status"], -r["lost"]))
    return render(request, "ui/regrade.html", {
        "cid": cid,
        "flagged": flagged_rows,
        "by_assignment": sorted(by_assignment.items()),
        "total_lost": sum(r["lost"] for r in flagged_rows),
    })


def regrade_email_export(request, cid: int):
    """Generate a plain-text email body for the prof, listing all open flags."""
    cdir = _course_dir_for(cid)
    progress = _load_progress(cdir)
    flags = progress.get("regrade_flags", {})
    summary_file = cdir / "_gradescope" / "summary.json"
    summary = json.loads(summary_file.read_text()) if summary_file.exists() else {}
    by_assignment: dict[str, list[dict]] = {}
    for a in summary.get("assignments", []):
        for w in a.get("wrong", []):
            key = f"{a['id']}#{w['question_id']}"
            if key not in flags:
                continue
            if flags[key].get("status") == "resolved":
                continue
            by_assignment.setdefault(a["name"], []).append({
                "title": w["title"],
                "score": w["score"],
                "max_score": w["max_score"],
                "lost": w["lost"],
                "comments": w.get("comments", ""),
                "notes": flags[key].get("notes", ""),
            })
    lines = [
        "Subject: ACMS 30440 — Regrade request",
        "",
        "Hi Professor,",
        "",
        "I would like to request a regrade discussion for the questions below. "
        "I've included my reasoning for each.",
        "",
    ]
    total_lost = 0.0
    for asgn_name, items in sorted(by_assignment.items()):
        lines.append(f"--- {asgn_name} ---")
        for it in items:
            lines.append(f"  • {it['title']}: I got {it['score']}/{it['max_score']} (lost {it['lost']} pts)")
            if it["comments"]:
                lines.append(f"    Grader comment: {it['comments']}")
            if it["notes"]:
                for ln in it["notes"].splitlines():
                    lines.append(f"    My reasoning: {ln}")
            lines.append("")
            total_lost += it["lost"]
        lines.append("")
    lines.append(f"Total points in question: {round(total_lost, 2)}")
    lines.append("")
    lines.append("Thank you for your time,")
    lines.append("")
    body = "\n".join(lines)
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def mistakes_view(request, cid: int):
    """Show wrong-question incidents pulled from Gradescope."""
    cdir = _course_dir_for(cid)
    summary_file = cdir / "_gradescope" / "summary.json"
    summary = {}
    asgns_with_wrong = []
    if summary_file.exists():
        summary = json.loads(summary_file.read_text())
        asgns_with_wrong = [a for a in summary.get("assignments", []) if a.get("wrong")]
    progress = _load_progress(cdir)
    explanations = progress.setdefault("mistake_explanations", {})
    flags = progress.get("regrade_flags", {})
    # Annotate each wrong with key + saved explanation + flag state
    gs_dir = cdir / "_gradescope"
    for a in asgns_with_wrong:
        for w in a["wrong"]:
            w["key"] = f"{a['id']}#{w['question_id']}"
            w["explanation"] = explanations.get(w["key"], "")
            f = flags.get(w["key"])
            w["flagged"] = bool(f)
            w["flag_status"] = f.get("status") if f else None
            w["flag_notes"] = f.get("notes", "") if f else ""
            w["img_url"] = reverse(
                "ui:mistake_image", args=[cid, a["id"], w["question_id"]]
            )
            sol_jpg = gs_dir / f"asgn_{a['id']}_q{w['question_id']}_solution.jpg"
            w["has_solution_img"] = sol_jpg.exists()
            w["solution_img_url"] = reverse(
                "ui:mistake_solution_image",
                args=[cid, a["id"], w["question_id"]],
            )
            w["pct_lost"] = (
                round(100 * w["lost"] / w["max_score"], 1)
                if w["max_score"] else 0
            )
    return render(request, "ui/mistakes.html", {
        "cid": cid,
        "summary": summary,
        "assignments": asgns_with_wrong,
        "wrong_total": summary.get("wrong_total", 0),
        "course_id_gs": summary.get("course_id"),
    })


def mistake_image(request, cid: int, aid: int, qid: int):
    """Serve the per-question Gradescope image (your handwritten answer)."""
    cdir = _course_dir_for(cid)
    p = cdir / "_gradescope" / f"asgn_{aid}_q{qid}.jpg"
    if not p.exists():
        return HttpResponse(status=404)
    return HttpResponse(p.read_bytes(), content_type="image/jpeg")


def mistake_solution_image(request, cid: int, aid: int, qid: int):
    """Serve the official solution-page image for a wrong question."""
    cdir = _course_dir_for(cid)
    p = cdir / "_gradescope" / f"asgn_{aid}_q{qid}_solution.jpg"
    if not p.exists():
        # Lazy render
        sys.path.insert(0, str(ROOT))
        from gradescope_client import render_solution_for_question
        summary_file = cdir / "_gradescope" / "summary.json"
        if summary_file.exists():
            summary = json.loads(summary_file.read_text())
            for a in summary.get("assignments", []):
                if a["id"] != aid:
                    continue
                for w in a.get("wrong", []):
                    if w["question_id"] != qid:
                        continue
                    render_solution_for_question(cdir, a["name"], w["title"], p)
                    break
                break
    if not p.exists():
        return HttpResponse(status=404)
    return HttpResponse(p.read_bytes(), content_type="image/jpeg")


@require_POST
def mistakes_pull(request, cid: int):
    """Trigger Gradescope sync (foreground; takes ~30-60s)."""
    cdir = _course_dir_for(cid)
    gs_cid = request.POST.get("gs_course_id", "").strip()
    if not gs_cid.isdigit():
        return HttpResponse(json.dumps({"error": "gs_course_id required (numeric)"}),
                            status=400, content_type="application/json")
    sys.path.insert(0, str(ROOT))
    try:
        from gradescope_client import pull_course
        summary = pull_course(int(gs_cid), cdir)
    except Exception as e:
        return HttpResponse(json.dumps({"error": str(e)}),
                            status=500, content_type="application/json")
    return HttpResponse(json.dumps({
        "ok": True, "wrong_total": summary["wrong_total"],
        "assignments": len(summary["assignments"]),
    }), content_type="application/json")


def _build_mistake_prompt(props: dict, qid: int):
    """Return (prompt, images_used) for a single wrong question."""
    qs_by_id = {q["id"]: q for q in props.get("questions", [])}
    sub_by_q = {qs["question_id"]: qs for qs in props.get("question_submissions", [])}
    q = qs_by_id.get(qid, {})
    qs = sub_by_q.get(qid, {})
    title = q.get("title") or f"Q{qid}"
    try:
        score = float(qs.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        weight = float(q.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0.0
    rubric_items = qs.get("rubric_items", [])
    comments = qs.get("comments") or ""
    asgn_name = props.get("assignment", {}).get("title", "Assignment")
    rubric_lookup = {ri["id"]: ri for ri in props.get("rubric_items", [])}
    rubric_desc = []
    for ri in rubric_items:
        ri_id = ri.get("rubric_item_id") if isinstance(ri, dict) else ri
        full = rubric_lookup.get(ri_id, {})
        rubric_desc.append(
            f"  - ({full.get('weight','?')} pts) {full.get('description','(no description)')}"
        )
    return {
        "title": title,
        "asgn_name": asgn_name,
        "score": score,
        "weight": weight,
        "rubric_desc": rubric_desc,
        "comments": comments,
    }


def _explain_one(cdir: Path, aid: int, qid: int, props: dict,
                 fast_model: str) -> tuple[int, str | None, str | None]:
    """Worker: generate explanation for one wrong question. Returns (qid, text, error)."""
    img_file = cdir / "_gradescope" / f"asgn_{aid}_q{qid}.jpg"
    sol_img = cdir / "_gradescope" / f"asgn_{aid}_q{qid}_solution.jpg"
    info = _build_mistake_prompt(props, qid)
    images_used = []
    if img_file.exists():
        images_used.append(str(img_file))
    if sol_img.exists():
        images_used.append(str(sol_img))
    if len(images_used) == 2:
        legend = ("Two images attached: image 1 = student's submitted answer. "
                  "image 2 = official instructor solution. Compare them.\n\n")
    elif len(images_used) == 1 and images_used[0] == str(img_file):
        legend = "Image = student's submitted answer.\n\n"
    elif len(images_used) == 1:
        legend = "Image = official instructor solution.\n\n"
    else:
        legend = ""
    prompt = (
        "You are a stats tutor. The student got points off on this Gradescope "
        "question. Explain in 250 words or less:\n"
        "1. What concept the question tested.\n"
        "2. The student's specific mistake.\n"
        "3. The correct method, briefly.\n"
        "4. One sentence of advice for the upcoming final.\n"
        "Use Markdown + LaTeX ($...$).\n\n"
        + legend +
        f"Assignment: {info['asgn_name']}\n"
        f"Question: {info['title']}\n"
        f"Score: {info['score']} / {info['weight']} (lost "
        f"{(info['weight'] or 0) - (info['score'] or 0)} pts)\n"
        f"Rubric items applied:\n"
        + ("\n".join(info["rubric_desc"]) or "  (none recorded)") + "\n"
        f"Grader comments: {info['comments'] or '(none)'}\n"
    )
    try:
        sys.path.insert(0, str(ROOT))
        from gemini_client import generate, generate_with_images
        if images_used:
            text = generate_with_images(prompt, images_used,
                                        max_output_tokens=4096, model=fast_model)
        else:
            text = generate(prompt, max_output_tokens=4096, model=fast_model)
        return (qid, text, None)
    except Exception as e:
        return (qid, None, str(e)[:300])


@require_POST
def mistake_followup(request, cid: int, aid: int, qid: int):
    """Follow-up question on a mistake explanation. Uses prior explanation as context."""
    cdir = _course_dir_for(cid)
    user_q = request.POST.get("question", "").strip()
    if not user_q:
        return HttpResponse(json.dumps({"error": "missing question"}),
                            status=400, content_type="application/json")
    progress = _load_progress(cdir)
    explanations = progress.get("mistake_explanations", {})
    prior = explanations.get(f"{aid}#{qid}", "")
    asgn_file = cdir / "_gradescope" / f"asgn_{aid}.json"
    img_file = cdir / "_gradescope" / f"asgn_{aid}_q{qid}.jpg"
    sol_img = cdir / "_gradescope" / f"asgn_{aid}_q{qid}_solution.jpg"
    if not asgn_file.exists():
        return HttpResponse(json.dumps({"error": "no cached assignment"}),
                            status=404, content_type="application/json")
    props = json.loads(asgn_file.read_text())
    info = _build_mistake_prompt(props, qid)
    images = [str(p) for p in (img_file, sol_img) if p.exists()]
    prompt = (
        "You are a stats tutor. The student already received an explanation for "
        "what they got wrong. Now they have a follow-up question. Answer "
        "directly and concisely (max 250 words). Build on the prior explanation; "
        "do NOT repeat it. Use Markdown + LaTeX.\n\n"
        f"<assignment>{info['asgn_name']} — {info['title']}</assignment>\n"
        f"<prior_explanation>\n{prior}\n</prior_explanation>\n\n"
        f"<followup>\n{user_q}\n</followup>"
    )
    sys.path.insert(0, str(ROOT))
    from gemini_client import generate, generate_with_images
    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
    try:
        if images:
            text = generate_with_images(prompt, images,
                                        max_output_tokens=4096, model=fast_model)
        else:
            text = generate(prompt, max_output_tokens=4096, model=fast_model)
    except Exception as e:
        return HttpResponse(json.dumps({"error": str(e)}),
                            status=500, content_type="application/json")
    return HttpResponse(json.dumps({"ok": True, "answer": text, "model": fast_model}),
                        content_type="application/json")


@require_POST
def mistakes_explain_all(request, cid: int):
    """Bulk-generate AI explanations for every wrong question in parallel.

    Skips already-explained questions unless ?force=1. Streams progress as
    JSON with counts. Workers = min(8, total).
    """
    cdir = _course_dir_for(cid)
    summary_file = cdir / "_gradescope" / "summary.json"
    if not summary_file.exists():
        return HttpResponse(json.dumps({"error": "no Gradescope summary cached"}),
                            status=404, content_type="application/json")
    summary = json.loads(summary_file.read_text())
    force = request.POST.get("force", "").strip() in ("1", "true", "on")
    progress = _load_progress(cdir)
    explanations = progress.setdefault("mistake_explanations", {})

    # Build job list
    jobs = []  # (aid, qid, props)
    asgn_props_cache: dict[int, dict] = {}
    for a in summary.get("assignments", []):
        if not a.get("wrong"):
            continue
        aid = a["id"]
        afile = cdir / "_gradescope" / f"asgn_{aid}.json"
        if not afile.exists():
            continue
        props = json.loads(afile.read_text())
        asgn_props_cache[aid] = props
        for w in a["wrong"]:
            qid = w["question_id"]
            key = f"{aid}#{qid}"
            if not force and key in explanations and explanations[key].strip():
                continue
            jobs.append((aid, qid, props))

    if not jobs:
        return HttpResponse(json.dumps({
            "ok": True, "generated": 0, "skipped": len(explanations),
            "msg": "all wrong questions already explained (use force=1 to regenerate)",
        }), content_type="application/json")

    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = min(8, len(jobs))
    generated = 0
    failed = 0
    fail_msgs: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_explain_one, cdir, aid, qid, props, fast_model): (aid, qid)
            for (aid, qid, props) in jobs
        }
        for fut in as_completed(futures):
            aid, qid = futures[fut]
            try:
                _qid, text, err = fut.result()
            except Exception as e:
                err, text = str(e)[:200], None
            if text:
                explanations[f"{aid}#{qid}"] = text
                generated += 1
            else:
                failed += 1
                fail_msgs.append(f"asgn={aid} qid={qid}: {err}")
    _save_progress(cdir, progress)
    return HttpResponse(json.dumps({
        "ok": True,
        "generated": generated,
        "failed": failed,
        "skipped": len([1 for j in jobs if False]),  # placeholder
        "errors": fail_msgs[:10],
        "workers": workers,
    }), content_type="application/json")


@require_POST
def mistake_explain(request, cid: int, aid: int, qid: int):
    """Generate Gemini explanation of what went wrong on one question."""
    cdir = _course_dir_for(cid)
    asgn_file = cdir / "_gradescope" / f"asgn_{aid}.json"
    img_file = cdir / "_gradescope" / f"asgn_{aid}_q{qid}.jpg"
    sol_img = cdir / "_gradescope" / f"asgn_{aid}_q{qid}_solution.jpg"
    if not asgn_file.exists():
        return HttpResponse(json.dumps({"error": "no cached assignment"}),
                            status=404, content_type="application/json")
    props = json.loads(asgn_file.read_text())
    qs_by_id = {q["id"]: q for q in props.get("questions", [])}
    sub_by_q = {qs["question_id"]: qs for qs in props.get("question_submissions", [])}
    q = qs_by_id.get(qid, {})
    qs = sub_by_q.get(qid, {})
    title = q.get("title") or f"Q{qid}"
    try:
        score = float(qs.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        weight = float(q.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0.0
    rubric_items = qs.get("rubric_items", [])
    comments = qs.get("comments") or ""
    asgn_name = props.get("assignment", {}).get("title", f"Assignment {aid}")

    # Look up rubric item descriptions
    rubric_lookup = {ri["id"]: ri for ri in props.get("rubric_items", [])}
    rubric_desc = []
    for ri in rubric_items:
        ri_id = ri.get("rubric_item_id") if isinstance(ri, dict) else ri
        full = rubric_lookup.get(ri_id, {})
        rubric_desc.append(
            f"  - ({full.get('weight','?')} pts) {full.get('description','(no description)')}"
        )

    images_used = []
    if img_file.exists():
        images_used.append(str(img_file))
    if sol_img.exists():
        images_used.append(str(sol_img))
    img_legend = ""
    if len(images_used) == 2:
        img_legend = ("Two images attached: image 1 = student's submitted answer "
                      "(handwritten / typed). image 2 = official instructor "
                      "solution page. Compare them.\n\n")
    elif len(images_used) == 1 and images_used[0] == str(img_file):
        img_legend = "Image = student's submitted answer.\n\n"
    elif len(images_used) == 1:
        img_legend = "Image = official instructor solution.\n\n"

    prompt = (
        "You are a stats tutor. The student got points off on this Gradescope "
        "question. Explain in 250 words or less:\n"
        "1. What concept the question tested.\n"
        "2. The student's specific mistake (compare student's image vs official solution).\n"
        "3. The correct method, briefly.\n"
        "4. One sentence of advice for the upcoming final.\n"
        "Use Markdown + LaTeX ($...$).\n\n"
        + img_legend +
        f"Assignment: {asgn_name}\n"
        f"Question: {title}\n"
        f"Score: {score} / {weight}  (lost {(weight or 0) - (score or 0)} pts)\n"
        f"Rubric items applied (these explain the deduction):\n"
        + ("\n".join(rubric_desc) or "  (none recorded)") + "\n"
        f"Grader comments: {comments or '(none)'}\n"
    )
    sys.path.insert(0, str(ROOT))
    from gemini_client import generate, generate_with_images
    fast_model = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
    try:
        if images_used:
            text = generate_with_images(prompt, images_used,
                                        max_output_tokens=4096, model=fast_model)
        else:
            text = generate(prompt, max_output_tokens=4096, model=fast_model)
    except Exception as e:
        return HttpResponse(json.dumps({"error": str(e)}),
                            status=500, content_type="application/json")
    progress = _load_progress(cdir)
    explanations = progress.setdefault("mistake_explanations", {})
    explanations[f"{aid}#{qid}"] = text
    _save_progress(cdir, progress)
    return HttpResponse(json.dumps({"ok": True, "explanation": text}),
                        content_type="application/json")


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
    """Return status of background OCR job by parsing per-course log files.

    Looks for both per-course logs (`/tmp/ocr_<name>_<cid>.log`, written by
    the sync_course chain) and falls back to the legacy global log only when
    no per-course log exists AND it was written within last 30 min (to avoid
    cross-course bleed).
    """
    import re, time as _t
    cdir = _course_dir_for(cid)

    jobs = []
    log_files = [
        ("vertex", f"/tmp/ocr_vertex_{cid}.log", "/tmp/ocr_vertex_full.log"),
        ("claude", f"/tmp/ocr_claude_{cid}.log", "/tmp/ocr_claude_full.log"),
        ("gemini", f"/tmp/ocr_gemini_{cid}.log", "/tmp/ocr_gemini_full.log"),
        ("tesseract", f"/tmp/ocr_tesseract_{cid}.log", "/tmp/ocr_tesseract_full.log"),
        ("summaries", f"/tmp/summaries_{cid}.log", "/tmp/summaries.log"),
    ]
    log_files_resolved: list[tuple[str, str]] = []
    for name, per_course, legacy in log_files:
        p = Path(per_course)
        if p.exists():
            log_files_resolved.append((name, per_course))
            continue
        # Legacy fallback: only if recently modified (<30 min) to avoid stale bleed
        lp = Path(legacy)
        if lp.exists() and (_t.time() - lp.stat().st_mtime) < 1800:
            log_files_resolved.append((name, legacy))
    log_files = log_files_resolved
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

        # Live-rate ETA from last 20 completed lines (OCR or summary format)
        page_times = [float(m) for m in re.findall(r"\(\d+c, ([\d.]+)s\)", text)][-20:]
        if not page_times:
            page_times = [float(m) for m in re.findall(r"chars in ([\d.]+)s", text)][-20:]
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
    from topic_graph import topo_sort, for_course
    GRAPH = for_course(cid)
    order = topo_sort(GRAPH)
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
    thinking = request.POST.get("thinking", "").strip() in ("1", "true", "on")
    if not problem:
        return HttpResponse("missing problem", status=400)
    sys.path.insert(0, str(ROOT))
    from solver import solve
    result = solve(problem, cdir, topic, thinking=thinking)
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
        from vectorize import search as vsearch
        where = {"category": cat} if cat else None
        for row in vsearch(cdir, q, k=k, where=where):
            results.append({"doc": row["text"], "meta": row["meta"],
                            "dist": round(row["dist"], 3)
                            if row["dist"] is not None else None})
    return render(request, "ui/ask.html", {"cid": cid, "q": q, "k": k, "cat": cat, "results": results})
