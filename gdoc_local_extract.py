"""Walk a local Google-Drive-Sync folder for .gdoc / .gsheet / .gslides
pointer files. For each, extract doc_id and fetch text via Google export
endpoints (using cookies from google_auth.py). Saves as .txt next to
the original .gdoc.

Usage:
    python gdoc_local_extract.py --root "/path/to/synced/folder"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).parent
COOKIE_FILE = ROOT_DIR / "secrets" / "google_cookies.json"


def _jar() -> dict:
    if not COOKIE_FILE.exists():
        raise RuntimeError("Run python google_auth.py first.")
    raw = json.loads(COOKIE_FILE.read_text())
    return {c["name"]: c["value"] for c in raw}


def _doc_id_from_pointer(p: Path) -> tuple[str | None, str]:
    """Read .gdoc/.gsheet/.gslides JSON pointer. Returns (doc_id, kind)."""
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return (None, "unknown")
    doc_id = data.get("doc_id")
    suffix = p.suffix.lower()
    kind = {".gdoc": "document", ".gsheet": "spreadsheets",
            ".gslides": "presentation", ".gform": "form"}.get(suffix, "unknown")
    return (doc_id, kind)


def _export_url(doc_id: str, kind: str, fmt: str = "txt") -> str | None:
    if kind == "document":
        return f"https://docs.google.com/document/d/{doc_id}/export?format={fmt}"
    if kind == "presentation":
        # txt export not available — use pdf and treat as bytes
        return f"https://docs.google.com/presentation/d/{doc_id}/export/{fmt}"
    if kind == "spreadsheets":
        return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv"
    return None


def fetch_one(p: Path, sess: requests.Session, force: bool = False) -> dict:
    doc_id, kind = _doc_id_from_pointer(p)
    if not doc_id:
        return {"path": str(p), "ok": False, "error": "no doc_id"}
    out = p.with_suffix(p.suffix + ".extracted.txt")
    if out.exists() and not force:
        return {"path": str(p), "ok": True, "cached": True,
                "out": str(out), "size": out.stat().st_size}
    url = _export_url(doc_id, kind)
    if not url:
        return {"path": str(p), "ok": False, "error": f"unsupported kind {kind}"}
    try:
        r = sess.get(url, timeout=120, allow_redirects=True)
    except Exception as e:
        return {"path": str(p), "ok": False, "error": f"req {e}"}
    if r.status_code != 200:
        return {"path": str(p), "ok": False,
                "error": f"http {r.status_code}", "doc_id": doc_id}
    if len(r.content) < 50:
        return {"path": str(p), "ok": False, "error": "tiny response"}
    out.write_bytes(r.content)
    return {"path": str(p), "ok": True, "out": str(out),
            "size": len(r.content), "kind": kind}


def walk_and_extract(root: Path, force: bool = False,
                     delay_sec: float = 0.5) -> dict:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://drive.google.com/",
    })
    sess.cookies.update(_jar())
    pointers = sorted(set(
        list(root.rglob("*.gdoc")) +
        list(root.rglob("*.gsheet")) +
        list(root.rglob("*.gslides"))
    ))
    print(f"Found {len(pointers)} pointer files under {root}")
    results = []
    ok_n = fail_n = 0
    for i, p in enumerate(pointers):
        rel = p.relative_to(root) if str(p).startswith(str(root)) else p
        r = fetch_one(p, sess, force=force)
        results.append(r)
        ok = r.get("ok")
        ok_n += int(bool(ok))
        fail_n += int(not ok)
        flag = "✓" if ok else "✗"
        info = (r.get("error") or f"{r.get('size','?')} B")
        print(f"  [{i+1}/{len(pointers)}] {flag} {rel}  -- {info}", flush=True)
        if not r.get("cached"):
            time.sleep(delay_sec)
    return {"total": len(pointers), "ok": ok_n, "failed": fail_n}


def find_panopto_links(root: Path) -> list[str]:
    """Scan all .extracted.txt files for Panopto URLs."""
    pat = re.compile(r"https?://[a-zA-Z0-9.-]*panopto[a-zA-Z0-9.-]*/[^\s\"'<>)]+", re.I)
    found: set[str] = set()
    for txt in root.rglob("*.extracted.txt"):
        try:
            data = txt.read_text(errors="ignore")
        except Exception:
            continue
        for m in pat.finditer(data):
            found.add(m.group(0).rstrip(".,;'\""))
    return sorted(found)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True,
                    help="Path to synced Google Drive folder.")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--find-panopto", action="store_true",
                    help="After extraction, scan for Panopto URLs.")
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 1
    summary = walk_and_extract(root, force=args.force)
    print()
    print(json.dumps(summary, indent=2))
    if args.find_panopto:
        urls = find_panopto_links(root)
        print(f"\nFound {len(urls)} Panopto URLs:")
        for u in urls:
            print(" ", u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
