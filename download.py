"""Download Canvas course material via Modules walker.

Captures: files, pages (with embedded file refs followed), assignments + their
attachments, discussions + replies, announcements, quizzes (best-effort).
Writes /tmp/sync_log_<course>.json with per-asset state for the UI Sync page.
Optionally invokes panopto.py + panopto_browser.py at the end.

Usage:
    python download.py                         # list courses
    python download.py --course 128781         # download
    python download.py --course 128781 --with-panopto
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from slugify import slugify

from canvas_client import CanvasClient

ROOT = Path(__file__).parent
COOKIE_FILE = ROOT / "cookies.json"
OUT_ROOT = ROOT / "downloads"
PYTHON = sys.executable

FILE_API_RE = re.compile(r"/api/v1/courses/\d+/files/(\d+)")
FILE_VIEW_RE = re.compile(r"/courses/\d+/files/(\d+)")


# -- progress tracker --------------------------------------------------------

class Progress:
    """Step-and-asset tracker. Snapshots to JSON after each update."""
    def __init__(self, course_id: int):
        self.path = Path(f"/tmp/sync_log_{course_id}.json")
        self.data: dict = {
            "course_id": course_id,
            "started_at": time.time(),
            "finished": False,
            "steps": {},
            "assets": [],
            "errors": [],
        }
        self.flush()

    def step_start(self, name: str, total: int = 0) -> None:
        self.data["steps"][name] = {
            "started_at": time.time(), "total": total, "done": 0,
            "failed": 0, "finished": False,
        }
        self.flush()

    def step_inc(self, name: str, ok: bool = True) -> None:
        s = self.data["steps"].get(name)
        if not s:
            return
        if ok:
            s["done"] += 1
        else:
            s["failed"] += 1
        # Throttle flush to every 5 increments to keep IO low
        if (s["done"] + s["failed"]) % 5 == 0:
            self.flush()

    def step_done(self, name: str) -> None:
        s = self.data["steps"].get(name)
        if not s:
            return
        s["finished"] = True
        s["finished_at"] = time.time()
        self.flush()

    def asset(self, kind: str, source: str, dest: str | None,
              size: int | None = None, status: str = "ok",
              note: str = "") -> None:
        self.data["assets"].append({
            "kind": kind, "source": source, "dest": dest,
            "size": size, "status": status, "note": note,
            "at": time.time(),
        })
        if status != "ok":
            self.data["errors"].append({"kind": kind, "source": source, "note": note})
        if len(self.data["assets"]) % 10 == 0:
            self.flush()

    def finish(self) -> None:
        self.data["finished"] = True
        self.data["finished_at"] = time.time()
        self.flush()

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, default=str))


# -- helpers -----------------------------------------------------------------

def load_cookies() -> dict[str, str]:
    raw = json.loads(COOKIE_FILE.read_text())
    return {c["name"]: c["value"] for c in raw}


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "")


def safe(name: str) -> str:
    return slugify(name or "untitled", max_length=120) or "untitled"


def extract_file_ids(html: str) -> set[int]:
    """Find all Canvas file IDs referenced in HTML body."""
    out = set()
    for m in FILE_API_RE.finditer(html or ""):
        out.add(int(m.group(1)))
    for m in FILE_VIEW_RE.finditer(html or ""):
        out.add(int(m.group(1)))
    return out


def download_file_by_id(client: CanvasClient, file_id, dest_dir: Path,
                        prog: Progress, source_label: str) -> Path | None:
    try:
        meta = client.get_file(file_id)
    except Exception as e:
        prog.asset("file", f"{source_label}#{file_id}", None, status="meta_fail", note=str(e)[:200])
        return None
    name = meta.get("display_name") or meta.get("filename") or f"file_{file_id}"
    url = meta.get("url")
    if not url:
        prog.asset("file", f"{source_label}#{file_id}", None, status="no_url", note=name)
        return None
    stem, _, ext = name.rpartition(".")
    dest = dest_dir / (f"{safe(stem)}.{ext}" if stem else safe(name))
    expected = meta.get("size") or -1
    if dest.exists() and dest.stat().st_size == expected:
        prog.asset("file", source_label, str(dest), size=expected, status="cached", note=name)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download(url, dest)
        prog.asset("file", source_label, str(dest), size=expected, status="ok", note=name)
        return dest
    except Exception as e:
        prog.asset("file", source_label, str(dest), status="download_fail", note=str(e)[:200])
        return None


# -- main course dump --------------------------------------------------------

def dump_course(client: CanvasClient, course: dict, base: Path,
                with_panopto: bool = True) -> None:
    cid = course["id"]
    prog = Progress(cid)
    print(f"\n=== {course.get('name')} (id={cid}) → {base} ===", flush=True)
    write_json(base / "course.json", course)

    seen_files: set[int] = set()  # avoid re-downloading same file referenced from multiple places

    # Modules
    prog.step_start("modules")
    try:
        mods = client.list_modules(cid)
        write_json(base / "modules.json", mods)
        prog.data["steps"]["modules"]["total"] = sum(len(m.get("items", [])) for m in mods)
        for m in mods:
            mname = safe(m.get("name") or f"module_{m['id']}")
            mdir = base / "modules" / mname
            for it in m.get("items", []):
                t = it.get("type")
                title = it.get("title", "")
                source_label = f"module:{mname}/{title}"
                try:
                    if t == "File":
                        fid = it.get("content_id")
                        if fid and fid not in seen_files:
                            seen_files.add(fid)
                            download_file_by_id(client, fid, mdir, prog, source_label)
                    elif t == "Page":
                        slug = it.get("page_url")
                        if slug:
                            full = client.get_page(cid, slug)
                            body = full.get("body") or ""
                            write_text(mdir / "pages" / f"{safe(slug)}.html", body)
                            write_json(mdir / "pages" / f"{safe(slug)}.json", full)
                            prog.asset("page", source_label, str(mdir / "pages" / f"{safe(slug)}.html"),
                                       size=len(body), status="ok", note=slug)
                            # Follow file links inside the page body
                            for fid in extract_file_ids(body):
                                if fid not in seen_files:
                                    seen_files.add(fid)
                                    download_file_by_id(client, fid, mdir / "pages" / "_attachments",
                                                        prog, f"page:{slug}")
                    elif t == "Assignment":
                        aid = it.get("content_id")
                        if aid:
                            a = client.get(f"/api/v1/courses/{cid}/assignments/{aid}")
                            desc = a.get("description") or ""
                            adir = mdir / "assignments"
                            write_text(adir / f"{aid}_{safe(a.get('name',''))}.html", desc)
                            write_json(adir / f"{aid}.json", a)
                            prog.asset("assignment", source_label, str(adir / f"{aid}.json"),
                                       size=len(desc), status="ok", note=a.get("name", ""))
                            for fid in extract_file_ids(desc):
                                if fid not in seen_files:
                                    seen_files.add(fid)
                                    download_file_by_id(client, fid, adir / "_attachments",
                                                        prog, f"assignment:{aid}")
                    elif t == "Quiz":
                        qid = it.get("content_id")
                        if qid:
                            q = client.get(f"/api/v1/courses/{cid}/quizzes/{qid}")
                            write_json(mdir / "quizzes" / f"{qid}.json", q)
                            try:
                                qs = client.list_quiz_questions(cid, qid)
                                write_json(mdir / "quizzes" / f"{qid}_questions.json", qs)
                            except Exception:
                                pass
                            prog.asset("quiz", source_label, str(mdir / "quizzes" / f"{qid}.json"),
                                       status="ok", note=q.get("title", ""))
                    elif t in ("ExternalUrl", "ExternalTool"):
                        write_json(mdir / "external" / f"{safe(title)}.json", it)
                        prog.asset("external", source_label, None, status="ok", note=title)
                    elif t == "Discussion":
                        did = it.get("content_id")
                        if did:
                            d = client.get(f"/api/v1/courses/{cid}/discussion_topics/{did}")
                            ddir = mdir / "discussions"
                            write_json(ddir / f"{did}.json", d)
                            write_text(ddir / f"{did}_{safe(d.get('title',''))}.html",
                                       d.get("message") or "")
                            prog.asset("discussion", source_label, str(ddir / f"{did}.json"),
                                       status="ok", note=d.get("title", ""))
                            for fid in extract_file_ids(d.get("message") or ""):
                                if fid not in seen_files:
                                    seen_files.add(fid)
                                    download_file_by_id(client, fid, ddir / "_attachments",
                                                        prog, f"discussion:{did}")
                    prog.step_inc("modules", True)
                except Exception as e:
                    prog.step_inc("modules", False)
                    prog.asset(t.lower() if t else "item", source_label, None,
                               status="fail", note=str(e)[:200])
    except Exception as e:
        prog.asset("modules", "list", None, status="fail", note=str(e)[:200])
    prog.step_done("modules")

    # Standalone Pages — bulk endpoint sometimes 404 on this course; tolerate.
    prog.step_start("pages")
    try:
        pages = client.list_pages(cid)
        write_json(base / "_pages_index.json", pages)
        prog.data["steps"]["pages"]["total"] = len(pages)
        for p in pages:
            slug = p.get("url")
            if not slug:
                continue
            try:
                full = client.get_page(cid, slug)
                body = full.get("body") or ""
                write_text(base / "pages" / f"{safe(slug)}.html", body)
                write_json(base / "pages" / f"{safe(slug)}.json", full)
                prog.asset("page", "standalone", str(base / "pages" / f"{safe(slug)}.html"),
                           size=len(body), status="ok", note=slug)
                for fid in extract_file_ids(body):
                    if fid not in seen_files:
                        seen_files.add(fid)
                        download_file_by_id(client, fid, base / "pages" / "_attachments",
                                            prog, f"page:{slug}")
                prog.step_inc("pages", True)
            except Exception as e:
                prog.step_inc("pages", False)
                prog.asset("page", "standalone", None, status="fail", note=f"{slug}: {e}"[:200])
    except Exception as e:
        prog.asset("pages", "list", None, status="skipped", note=str(e)[:200])
    prog.step_done("pages")

    # Standalone Assignments
    prog.step_start("assignments")
    try:
        assigns = client.list_assignments(cid)
        write_json(base / "assignments.json", assigns)
        prog.data["steps"]["assignments"]["total"] = len(assigns)
        for a in assigns:
            try:
                desc = a.get("description") or ""
                write_text(base / "assignments" / f"{a['id']}_{safe(a.get('name',''))}.html", desc)
                prog.asset("assignment", "standalone",
                           str(base / "assignments" / f"{a['id']}.html"),
                           size=len(desc), status="ok", note=a.get("name", ""))
                for fid in extract_file_ids(desc):
                    if fid not in seen_files:
                        seen_files.add(fid)
                        download_file_by_id(client, fid, base / "assignments" / "_attachments",
                                            prog, f"assignment:{a['id']}")
                prog.step_inc("assignments", True)
            except Exception as e:
                prog.step_inc("assignments", False)
                prog.asset("assignment", "standalone", None, status="fail", note=str(e)[:200])
    except Exception as e:
        prog.asset("assignments", "list", None, status="skipped", note=str(e)[:200])
    prog.step_done("assignments")

    # Discussions (standalone list — captures any not in modules)
    prog.step_start("discussions")
    try:
        ds = client.list_discussions(cid)
        write_json(base / "discussions.json", ds)
        prog.data["steps"]["discussions"]["total"] = len(ds)
        for d in ds:
            did = d.get("id")
            if not did:
                continue
            try:
                full = client.get(f"/api/v1/courses/{cid}/discussion_topics/{did}")
                msg = full.get("message") or ""
                write_text(base / "discussions" / f"{did}_{safe(full.get('title',''))}.html", msg)
                write_json(base / "discussions" / f"{did}.json", full)
                prog.asset("discussion", "standalone",
                           str(base / "discussions" / f"{did}.json"),
                           size=len(msg), status="ok", note=full.get("title", ""))
                for fid in extract_file_ids(msg):
                    if fid not in seen_files:
                        seen_files.add(fid)
                        download_file_by_id(client, fid, base / "discussions" / "_attachments",
                                            prog, f"discussion:{did}")
                prog.step_inc("discussions", True)
            except Exception as e:
                prog.step_inc("discussions", False)
                prog.asset("discussion", "standalone", None, status="fail", note=str(e)[:200])
    except Exception as e:
        prog.asset("discussions", "list", None, status="skipped", note=str(e)[:200])
    prog.step_done("discussions")

    # Announcements
    prog.step_start("announcements")
    try:
        anns = client.list_announcements(cid)
        write_json(base / "announcements.json", anns)
        prog.data["steps"]["announcements"]["total"] = len(anns)
        for a in anns:
            msg = a.get("message") or ""
            aid = a.get("id")
            write_text(base / "announcements" / f"{aid}_{safe(a.get('title',''))}.html", msg)
            prog.asset("announcement", "list",
                       str(base / "announcements" / f"{aid}.html"),
                       size=len(msg), status="ok", note=a.get("title", ""))
            for fid in extract_file_ids(msg):
                if fid not in seen_files:
                    seen_files.add(fid)
                    download_file_by_id(client, fid, base / "announcements" / "_attachments",
                                        prog, f"announcement:{aid}")
            prog.step_inc("announcements", True)
    except Exception as e:
        prog.asset("announcements", "list", None, status="skipped", note=str(e)[:200])
    prog.step_done("announcements")

    # Panopto — auto-attempt, skip silently if no captions / not available
    if with_panopto:
        prog.step_start("panopto")
        try:
            print("  → Panopto: trying transcripts (auto-skip if unavailable)...", flush=True)
            r = subprocess.run(
                [PYTHON, "panopto.py", "--course-dir", str(base)],
                capture_output=True, text=True, timeout=600,
            )
            tail = (r.stdout + r.stderr).splitlines()[-5:]
            wrote = sum(1 for line in tail if "Wrote" in line and "transcript" in line)
            prog.asset("panopto", "auto", str(base / "transcripts"),
                       status="ok" if r.returncode == 0 else "skipped",
                       note=" | ".join(tail)[:300])
        except Exception as e:
            prog.asset("panopto", "auto", None, status="skipped", note=str(e)[:200])
        prog.step_done("panopto")

    prog.finish()
    print(f"\nProgress log: {prog.path}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=str, help="Canvas course id (omit to list)")
    ap.add_argument("--name", type=str, help="output folder name")
    ap.add_argument("--no-panopto", action="store_true", help="skip Panopto auto-attempt")
    args = ap.parse_args()

    if not COOKIE_FILE.exists():
        print(f"Missing {COOKIE_FILE}.")
        return 1
    cookies = load_cookies()
    client = CanvasClient(cookies)

    if not args.course:
        print("Active courses:")
        for c in client.list_courses():
            print(f"  {c['id']}\t{c.get('name')}")
        print("\nRe-run with --course <id> [--name <slug>]")
        return 0

    course = client.get(f"/api/v1/courses/{args.course}", **{"include[]": "term"})
    folder = args.name or safe(course.get("name") or f"course_{args.course}")
    base = OUT_ROOT / f"{args.course}_{folder}"
    dump_course(client, course, base, with_panopto=not args.no_panopto)
    print(f"\nDone → {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
