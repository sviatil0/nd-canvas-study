"""Download Panopto video transcripts for Notre Dame Canvas courses.

Steps:
    1. Read Canvas cookies (cookies.json), list pages prefixed `panopto-videos-`.
    2. Extract `custom_context_delivery=<UUID>` Panopto session IDs from each page.
    3. Auth to Panopto via Playwright (one-time login). Save cookies to panopto_cookies.json.
    4. For each session, fetch transcript via the Pages/Transcript.aspx endpoint
       and write plain-text transcript to downloads/<course>/transcripts/.

Usage:
    python panopto.py --course-dir downloads/128781_statistics
    python panopto.py --course-dir <dir> --auth-only       # just save Panopto cookies
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import requests

ROOT = Path(__file__).parent
PANOPTO_HOST = "https://notredame.hosted.panopto.com"
PANOPTO_COOKIE_FILE = ROOT / "panopto_cookies.json"
CANVAS_COOKIE_FILE = ROOT / "cookies.json"
DELIVERY_RE = re.compile(r"custom_context_delivery%3D([0-9a-f-]{36})", re.I)


def load_canvas_session() -> requests.Session:
    s = requests.Session()
    s.cookies.update({c["name"]: c["value"] for c in json.loads(CANVAS_COOKIE_FILE.read_text())})
    s.headers["Accept"] = "application/json"
    return s


def list_panopto_pages(course_id: int, sess: requests.Session) -> list[dict]:
    url = f"https://canvas.nd.edu/api/v1/courses/{course_id}/pages?per_page=100"
    pages = []
    while url:
        r = sess.get(url)
        r.raise_for_status()
        for p in r.json():
            if "panopto-videos-" in (p.get("url") or ""):
                pages.append(p)
        url = r.links.get("next", {}).get("url")
    return pages


def fetch_page_body(course_id: int, slug: str, sess: requests.Session) -> str:
    r = sess.get(f"https://canvas.nd.edu/api/v1/courses/{course_id}/pages/{slug}")
    r.raise_for_status()
    return r.json().get("body") or ""


def extract_delivery_ids(body: str) -> list[str]:
    return list({m.group(1) for m in DELIVERY_RE.finditer(unquote(body))} |
                {m.group(1) for m in DELIVERY_RE.finditer(body)})


def panopto_login() -> list[dict]:
    from playwright.sync_api import sync_playwright
    print("Opening Panopto. Sign in via ND SSO + Duo. Will auto-detect login completion.")
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        # Open a known sessions list URL — forces auth
        page.goto(f"{PANOPTO_HOST}/Panopto/Pages/Home.aspx")
        try:
            page.wait_for_url(lambda u: "Home.aspx" in u or "Sessions" in u, timeout=300_000)
            # Wait for a UI element only present post-login
            page.wait_for_selector("#siteHomeNav, #content, .panopto-page-header", timeout=120_000)
        except Exception as e:
            print(f"Login wait failed: {e}")
        cookies = ctx.cookies()
        browser.close()
    panopto = [c for c in cookies if "panopto.com" in c.get("domain", "")]
    PANOPTO_COOKIE_FILE.write_text(json.dumps(panopto, indent=2))
    print(f"Saved {len(panopto)} Panopto cookies.")
    return panopto


def panopto_session() -> requests.Session:
    if not PANOPTO_COOKIE_FILE.exists():
        panopto_login()
    cookies = json.loads(PANOPTO_COOKIE_FILE.read_text())
    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c["domain"])
    s.headers["User-Agent"] = "Mozilla/5.0"
    return s


def fetch_transcript(delivery_id: str, sess: requests.Session) -> str | None:
    """Try multiple Panopto transcript endpoints. Return plain text or None."""
    # Endpoint 1: GetCaptions JSON API
    r = sess.get(
        f"{PANOPTO_HOST}/Panopto/Pages/Viewer/DeliveryInfo.aspx",
        params={
            "deliveryId": delivery_id,
            "isLiveNotes": "false",
            "refreshAuthCookie": "true",
            "isActiveBroadcast": "false",
            "isEditing": "false",
            "isKollectiveAgentInstalled": "false",
            "isEmbed": "false",
            "responseType": "json",
        },
    )
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
        try:
            data = r.json()
            if data.get("ErrorCode"):
                return None
        except Exception:
            pass

    # Endpoint 2: Pages/Transcript.aspx?id=<id>  (HTML)
    r = sess.get(f"{PANOPTO_HOST}/Panopto/Pages/Transcript.aspx", params={"id": delivery_id})
    if r.status_code == 200 and "transcript" in r.text.lower():
        # Strip HTML to plain text
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 200:
            return text

    # Endpoint 3: Caption SRT (deliveryId path)
    r = sess.get(
        f"{PANOPTO_HOST}/Panopto/Pages/Viewer/Caption.ashx",
        params={"id": delivery_id, "language": "0"},
    )
    if r.status_code == 200 and r.text.strip():
        # Strip SRT timing/index lines
        lines = []
        for ln in r.text.splitlines():
            ln = ln.strip()
            if not ln or ln.isdigit() or "-->" in ln:
                continue
            lines.append(ln)
        if lines:
            return "\n".join(lines)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--auth-only", action="store_true", help="just authenticate Panopto and exit")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1

    if args.auth_only:
        panopto_login()
        return 0

    course_id = int(cdir.name.split("_", 1)[0])
    canvas = load_canvas_session()
    print(f"Listing Panopto pages for course {course_id}...")
    pages = list_panopto_pages(course_id, canvas)
    print(f"  found {len(pages)} pages")

    pano = panopto_session()
    out_dir = cdir / "transcripts"
    out_dir.mkdir(exist_ok=True)
    manifest = []

    for p in pages:
        slug = p["url"]
        body = fetch_page_body(course_id, slug, canvas)
        ids = extract_delivery_ids(body)
        print(f"\n[{slug}] {len(ids)} videos")
        for vid in ids:
            dest = out_dir / f"{slug}__{vid}.txt"
            if dest.exists() and dest.stat().st_size > 0:
                print(f"  skip cached {vid}")
                manifest.append({"page": slug, "id": vid, "path": str(dest.relative_to(cdir))})
                continue
            text = fetch_transcript(vid, pano)
            if not text:
                print(f"  ✗ no transcript for {vid}")
                continue
            dest.write_text(text)
            print(f"  ✓ {vid} ({len(text)} chars)")
            manifest.append({"page": slug, "id": vid, "path": str(dest.relative_to(cdir))})

    (cdir / "transcripts" / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nDone. Wrote {len(manifest)} transcript records → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
