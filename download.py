"""Download Canvas course material via Modules walker (works even when /files blocked).

Usage:
    python download.py                  # list courses
    python download.py --course 128781  # download one course by id
    python download.py --course 128781 --name statistics
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from slugify import slugify

from canvas_client import CanvasClient

ROOT = Path(__file__).parent
COOKIE_FILE = ROOT / "cookies.json"
OUT_ROOT = ROOT / "downloads"


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


def download_file_by_id(client: CanvasClient, file_id, dest_dir: Path) -> Path | None:
    try:
        meta = client.get_file(file_id)
    except Exception as e:
        print(f"      file meta {file_id} fail: {e}")
        return None
    name = meta.get("display_name") or meta.get("filename") or f"file_{file_id}"
    url = meta.get("url")
    if not url:
        return None
    stem, _, ext = name.rpartition(".")
    dest = dest_dir / (f"{safe(stem)}.{ext}" if stem else safe(name))
    if dest.exists() and dest.stat().st_size == (meta.get("size") or -1):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download(url, dest)
        print(f"      ↓ {name}")
        return dest
    except Exception as e:
        print(f"      ↓ {name} FAIL: {e}")
        return None


def dump_course(client: CanvasClient, course: dict, base: Path) -> None:
    cid = course["id"]
    print(f"\n=== {course.get('name')} (id={cid}) → {base} ===")
    write_json(base / "course.json", course)

    # Try the bulk Files endpoint (often blocked)
    try:
        files = client.list_files(cid)
        write_json(base / "_files_index.json", files)
    except Exception as e:
        print(f"  /files blocked ({e}); will use modules walker")

    # Modules — primary spine
    try:
        mods = client.list_modules(cid)
        write_json(base / "modules.json", mods)
        for m in mods:
            mname = safe(m.get("name") or f"module_{m['id']}")
            mdir = base / "modules" / mname
            for it in m.get("items", []):
                t = it.get("type")
                title = it.get("title", "")
                if t == "File":
                    fid = it.get("content_id")
                    if fid:
                        download_file_by_id(client, fid, mdir)
                elif t == "Page":
                    slug = it.get("page_url")
                    if not slug:
                        continue
                    try:
                        full = client.get_page(cid, slug)
                        write_text(mdir / "pages" / f"{safe(slug)}.html", full.get("body") or "")
                        write_json(mdir / "pages" / f"{safe(slug)}.json", full)
                    except Exception as e:
                        print(f"    page {slug} fail: {e}")
                elif t == "Assignment":
                    aid = it.get("content_id")
                    if aid:
                        try:
                            a = client.get(f"/api/v1/courses/{cid}/assignments/{aid}")
                            write_text(
                                mdir / "assignments" / f"{aid}_{safe(a.get('name',''))}.html",
                                a.get("description") or "",
                            )
                            write_json(mdir / "assignments" / f"{aid}.json", a)
                        except Exception as e:
                            print(f"    assignment {aid} fail: {e}")
                elif t == "Quiz":
                    qid = it.get("content_id")
                    if qid:
                        try:
                            q = client.get(f"/api/v1/courses/{cid}/quizzes/{qid}")
                            write_json(mdir / "quizzes" / f"{qid}.json", q)
                            try:
                                qs = client.list_quiz_questions(cid, qid)
                                write_json(mdir / "quizzes" / f"{qid}_questions.json", qs)
                            except Exception as e:
                                print(f"    quiz questions {qid} fail: {e}")
                        except Exception as e:
                            print(f"    quiz {qid} fail: {e}")
                elif t == "ExternalUrl":
                    write_json(mdir / "external" / f"{safe(title)}.json", it)
    except Exception as e:
        print(f"  modules failed: {e}")

    # Standalone Pages
    try:
        pages = client.list_pages(cid)
        write_json(base / "_pages_index.json", pages)
        for p in pages:
            slug = p.get("url")
            if not slug:
                continue
            try:
                full = client.get_page(cid, slug)
                write_text(base / "pages" / f"{safe(slug)}.html", full.get("body") or "")
                write_json(base / "pages" / f"{safe(slug)}.json", full)
            except Exception:
                pass
    except Exception as e:
        print(f"  pages skipped: {e}")

    # Standalone Assignments
    try:
        assigns = client.list_assignments(cid)
        write_json(base / "assignments.json", assigns)
        for a in assigns:
            write_text(
                base / "assignments" / f"{a['id']}_{safe(a.get('name',''))}.html",
                a.get("description") or "",
            )
    except Exception as e:
        print(f"  assignments skipped: {e}")

    # Quizzes (may 401/403)
    try:
        qs = client.list_quizzes(cid)
        write_json(base / "quizzes.json", qs)
        for q in qs:
            try:
                questions = client.list_quiz_questions(cid, q["id"])
                write_json(base / "quizzes" / f"{q['id']}_questions.json", questions)
            except Exception:
                pass
    except Exception as e:
        print(f"  quizzes skipped: {e}")

    # Discussions
    try:
        ds = client.list_discussions(cid)
        write_json(base / "discussions.json", ds)
    except Exception as e:
        print(f"  discussions skipped: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=str, help="Canvas course id (omit to list)")
    ap.add_argument("--name", type=str, help="output folder name (default: slugified course name)")
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
    dump_course(client, course, base)
    print(f"\nDone → {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
