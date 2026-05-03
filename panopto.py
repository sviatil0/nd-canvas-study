"""Download Panopto video transcripts for Notre Dame Canvas courses.

NOTE: ND's Panopto deployment restricts caption/transcript access to LTI-launched
sessions tied to a Canvas course. Direct DeliveryInfo / Caption.ashx requests
return ErrorCode 6 ("session isn't available") for non-LTI cookie sessions.
This script discovers all video delivery IDs and tries multiple endpoints, but
will return 0 transcripts for restricted courses. Files endpoint is still useful
for IDs to feed into a future browser-driven scraper.

Steps:
    1. Read Canvas cookies, find pages with `panopto-videos-` slug or referenced
       in any local HTML.
    2. Extract `custom_context_delivery=<UUID>` from each page body.
    3. Auth to Panopto via Playwright. Save cookies.
    4. Try DeliveryInfo / Transcript / Caption.ashx — write text if returned.

Usage:
    python panopto.py --course-dir downloads/128781_statistics
    python panopto.py --course-dir <dir> --auth-only       # just save cookies
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
        if r.status_code == 404:
            return []
        r.raise_for_status()
        for p in r.json():
            if "panopto-videos-" in (p.get("url") or ""):
                pages.append(p)
        url = r.links.get("next", {}).get("url")
    return pages


PANOPTO_PAGE_LINK = re.compile(r'/courses/\d+/pages/(panopto-videos-[a-z0-9-]+)', re.I)


def find_panopto_page_slugs(course_dir: Path) -> list[str]:
    """Walk downloaded HTML and return unique panopto-video page slugs referenced."""
    slugs: set[str] = set()
    for html in course_dir.rglob("*.html"):
        try:
            body = html.read_text(errors="ignore")
        except Exception:
            continue
        for m in PANOPTO_PAGE_LINK.finditer(body):
            slugs.add(m.group(1).rstrip("\\").lower())
    return sorted(slugs)


def scan_local_for_panopto(course_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for html in course_dir.rglob("*.html"):
        try:
            body = html.read_text(errors="ignore")
        except Exception:
            continue
        if "custom_context_delivery" not in body.lower():
            continue
        out[html.stem] = body
    return out


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
    print(f"Listing Panopto pages for course {course_id} via API...")
    pages = list_panopto_pages(course_id, canvas)
    print(f"  found {len(pages)} pages via API")

    sources: dict[str, str] = {}
    for p in pages:
        slug = p["url"]
        sources[slug] = fetch_page_body(course_id, slug, canvas)

    referenced = find_panopto_page_slugs(cdir)
    print(f"Found {len(referenced)} panopto-video page slugs referenced in local HTML.")
    for slug in referenced:
        if slug in sources:
            continue
        try:
            sources[slug] = fetch_page_body(course_id, slug, canvas)
            print(f"  fetched {slug}")
        except Exception as e:
            print(f"  fail {slug}: {e}")

    local = scan_local_for_panopto(cdir)
    for label, body in local.items():
        sources.setdefault(label, body)
    print(f"  total page sources: {len(sources)}")

    pano = panopto_session()
    out_dir = cdir / "transcripts"
    out_dir.mkdir(exist_ok=True)
    manifest = []

    for slug, body in sources.items():
        ids = extract_delivery_ids(body)
        if not ids:
            continue
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
