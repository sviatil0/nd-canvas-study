"""Scrape Google Drive / Docs / Slides / Sheets URLs into local PDFs.

Uses cookies from google_auth.py (no API key). Each URL → exported PDF
saved under <course_dir>/_external/google.com/_files/<id>_<title>.pdf.

Usage:
    python google_scrape.py --course-dir downloads/<dir> --url <gdrive-url>

Or batch via stdin: one URL per line.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
COOKIE_FILE = Path(__file__).parent / "secrets" / "google_cookies.json"


def _jar() -> dict:
    if not COOKIE_FILE.exists():
        raise RuntimeError("Run python google_auth.py first.")
    raw = json.loads(COOKIE_FILE.read_text())
    return {c["name"]: c["value"] for c in raw}


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s)[:120].strip("_") or "untitled"


def _id_from_url(url: str) -> tuple[str | None, str]:
    """Extract Drive/Docs/Slides/Sheets ID + kind from URL."""
    p = urlparse(url)
    path = p.path
    qs = parse_qs(p.query)
    # docs.google.com/{document,presentation,spreadsheets,forms}/d/<id>/...
    m = re.search(r"/(document|presentation|spreadsheets|forms)/d/([a-zA-Z0-9_-]{20,})", path)
    if m:
        return (m.group(2), m.group(1))
    # drive.google.com/file/d/<id>/...
    m = re.search(r"/file/d/([a-zA-Z0-9_-]{20,})", path)
    if m:
        return (m.group(1), "file")
    # drive.google.com/folders/<id>
    m = re.search(r"/folders/([a-zA-Z0-9_-]{20,})", path)
    if m:
        return (m.group(1), "folder")
    # drive.google.com/open?id=<id>
    if "id" in qs:
        return (qs["id"][0], "file")
    return (None, "unknown")


def _export_url(file_id: str, kind: str) -> str | None:
    if kind == "document":
        return f"https://docs.google.com/document/d/{file_id}/export?format=pdf"
    if kind == "presentation":
        return f"https://docs.google.com/presentation/d/{file_id}/export/pdf"
    if kind == "spreadsheets":
        return f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx"
    if kind == "file":
        return f"https://drive.google.com/uc?id={file_id}&export=download"
    return None  # folder / form: handled separately


def _filename_for(kind: str) -> str:
    if kind == "document":
        return "doc.pdf"
    if kind == "presentation":
        return "slides.pdf"
    if kind == "spreadsheets":
        return "sheet.xlsx"
    return "drive_file"


def fetch_one(url: str, out_dir: Path, sess: requests.Session) -> dict:
    """Download a single Drive/Docs URL. Returns metadata dict."""
    file_id, kind = _id_from_url(url)
    if not file_id:
        return {"url": url, "ok": False, "error": "no id parsed"}
    if kind == "folder":
        return _fetch_folder(file_id, url, out_dir, sess)
    if kind == "forms":
        return {"url": url, "ok": False, "error": "google forms not supported"}
    export_url = _export_url(file_id, kind)
    if not export_url:
        return {"url": url, "ok": False, "error": f"no export for kind={kind}"}
    try:
        r = sess.get(export_url, timeout=120, allow_redirects=True)
    except Exception as e:
        return {"url": url, "ok": False, "error": f"req-err {e}"}
    if r.status_code != 200 or len(r.content) < 100:
        return {"url": url, "ok": False, "error": f"http {r.status_code} len {len(r.content)}"}
    # Heuristic title from response (Content-Disposition)
    cd = r.headers.get("Content-Disposition", "")
    tm = re.search(r'filename(?:\*=UTF-8\'\')?=?"?([^";]+)"?', cd)
    title = tm.group(1).strip() if tm else _filename_for(kind)
    title = _safe_name(title)
    if kind == "document" and not title.lower().endswith(".pdf"):
        title += ".pdf"
    if kind == "presentation" and not title.lower().endswith(".pdf"):
        title += ".pdf"
    if kind == "spreadsheets" and not title.lower().endswith(".xlsx"):
        title += ".xlsx"
    out_path = out_dir / f"{file_id[:10]}_{title}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    return {
        "url": url, "ok": True, "kind": kind, "id": file_id,
        "local_path": str(out_path),
        "size": len(r.content),
    }


def _fetch_folder(folder_id: str, url: str, out_dir: Path, sess: requests.Session) -> dict:
    """List a public Drive folder by scraping its HTML, then fetch children."""
    list_url = f"https://drive.google.com/drive/folders/{folder_id}"
    try:
        r = sess.get(list_url, timeout=60)
    except Exception as e:
        return {"url": url, "ok": False, "error": f"folder list-err {e}"}
    if r.status_code != 200:
        return {"url": url, "ok": False, "error": f"folder http {r.status_code}"}
    body = r.text
    # Drive embeds child IDs in JSON-like arrays inside <script>. Extract any
    # 28-44 char Drive IDs.
    child_ids = set(re.findall(r'"([a-zA-Z0-9_-]{28,44})"', body))
    # Filter out the folder itself + obvious non-IDs (must contain at least one digit + letter)
    child_ids.discard(folder_id)
    children = []
    for cid in list(child_ids)[:60]:  # cap at 60 children
        # Try as document first, then file fallback
        for kind in ("document", "presentation", "spreadsheets", "file"):
            export = _export_url(cid, kind)
            if not export:
                continue
            try:
                rr = sess.get(export, timeout=60, allow_redirects=True)
            except Exception:
                continue
            if rr.status_code == 200 and len(rr.content) > 1000:
                title = _safe_name(_filename_for(kind))
                out_path = out_dir / f"{cid[:10]}_{title}"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(rr.content)
                children.append({"id": cid, "kind": kind, "local_path": str(out_path),
                                 "size": len(rr.content)})
                break
    return {"url": url, "ok": True, "kind": "folder", "id": folder_id,
            "children": children}


def fetch_many(urls: list[str], course_dir: Path) -> dict:
    """Fetch many URLs, save under course_dir/_external/google.com/_files/."""
    out_dir = course_dir / "_external" / "google.com" / "_files"
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    sess.cookies.update(_jar())
    results = []
    for i, url in enumerate(urls):
        print(f"  [{i+1}/{len(urls)}] {url[:90]}", flush=True)
        results.append(fetch_one(url, out_dir, sess))
        time.sleep(0.7)
    manifest = course_dir / "_external" / "google.com" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"results": results}, indent=2))
    ok = sum(1 for r in results if r.get("ok"))
    return {"total": len(results), "ok": ok, "failed": len(results) - ok}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--url", action="append", default=[],
                    help="Drive/Docs URL (repeatable). Or pass via stdin (one per line).")
    ap.add_argument("--from-canvas", action="store_true",
                    help="Auto-detect Drive URLs from Canvas content.")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1
    urls = list(args.url)
    if not urls and not sys.stdin.isatty():
        for line in sys.stdin:
            ln = line.strip()
            if ln and ln.startswith("http"):
                urls.append(ln)
    if args.from_canvas:
        sys.path.insert(0, str(Path(__file__).parent))
        from external_scrape import detect_course_urls_from_canvas, _classify_external_url
        for u in detect_course_urls_from_canvas(cdir):
            kind = _classify_external_url(u)
            if kind in ("drive_folder", "drive_file", "gslides", "gdoc",
                        "gsheet"):
                urls.append(u)
        urls = list(dict.fromkeys(urls))  # dedup, keep order
        print(f"Detected {len(urls)} Google URLs in Canvas content.")
    if not urls:
        print("No URLs to fetch (use --url or --from-canvas)")
        return 1
    summary = fetch_many(urls, cdir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
