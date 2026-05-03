from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
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
    code, out = _run([PYTHON, "auth.py"])
    if code == 0:
        messages.success(request, "Logged in. Cookies saved.")
    else:
        messages.error(request, f"Login failed: {out}")
    return redirect("ui:index")


def auth_check(request):
    code, out = _run([PYTHON, "auth.py", "--check"])
    return HttpResponse(out, content_type="text/plain")


def courses_refresh(request):
    return redirect("ui:index")


@require_POST
def sync_course(request, cid: int):
    name = request.POST.get("name", "").strip() or None
    args = [PYTHON, "download.py", "--course", str(cid)]
    if name:
        args += ["--name", name]
    code, out = _run(args)
    if code == 0:
        messages.success(request, f"Synced course {cid}")
    else:
        messages.error(request, f"Sync failed: {out}")
    return redirect("ui:course_detail", cid=cid)


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
    gap_top = [r for r in gap if r["share_gap_pp"] > 0 and r["exam_hits"] >= 3][:15]
    return render(request, "ui/course.html", {
        "cid": cid,
        "course": course,
        "bundles": bundles,
        "files": files,
        "gap_top": gap_top,
        "have_bundles": bool(bundles),
        "have_gap": bool(gap),
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
    problems = data.get(topic, [])
    return render(request, "ui/topic.html", {
        "cid": cid,
        "topic": topic,
        "problems": problems,
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
