"""Headless Playwright Panopto folder scraper via Canvas LTI launch.

Approach: open Canvas's Panopto external tool URL in Playwright using saved
Canvas cookies. Canvas auto-fires LTI launch → Panopto receives the SAML
assertion → loads the course folder. Once loaded, scrape video session list
+ for each session pull caption track if available.

Usage:
    python panopto_lti_scraper.py --course-id 130417 --course-dir downloads/130417_computer-architecture
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
CANVAS_COOKIE_FILE = ROOT / "cookies.json"


def _cookies_for_playwright() -> list[dict]:
    if not CANVAS_COOKIE_FILE.exists():
        raise RuntimeError("No canvas cookies. Run python auth.py first.")
    raw = json.loads(CANVAS_COOKIE_FILE.read_text())
    cookies = []
    for c in raw:
        cookies.append({
            "name": c["name"], "value": c["value"],
            "domain": c.get("domain", ".canvas.nd.edu"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": c.get("sameSite", "Lax").capitalize() if isinstance(c.get("sameSite"), str) else "Lax",
        })
    return cookies


def find_panopto_tool_id(course_id: int) -> int | None:
    """Query Canvas tabs API to get Panopto external tool numeric id."""
    import requests
    raw = json.loads(CANVAS_COOKIE_FILE.read_text())
    jar = {c["name"]: c["value"] for c in raw}
    r = requests.get(f"https://canvas.nd.edu/api/v1/courses/{course_id}/tabs",
                     cookies=jar, headers={"Accept": "application/json"},
                     timeout=15)
    if r.status_code != 200:
        return None
    for t in r.json():
        if "panopto" in (t.get("label") or "").lower():
            html_url = t.get("html_url", "")
            m = re.search(r"/external_tools/(\d+)", html_url)
            if m:
                return int(m.group(1))
    return None


SESSIONS_JS = """
async (folderId) => {
  const body = {queryParameters: {query: null, sortColumn: 1, sortAscending: true,
    maxResults: 200, page: 0, startDate: null, endDate: null, folderID: folderId,
    bookmarked: false, getFolderData: true, isSharedWithMe: false,
    includePlaylists: false}};
  const r = await fetch('/Panopto/Services/Data.svc/GetSessions', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    credentials: 'include', body: JSON.stringify(body)});
  if (!r.ok) return {error: 'http ' + r.status};
  const j = await r.json();
  const rows = (j && j.d && j.d.Results) || [];
  return {sessions: rows.map(s => ({id: s.DeliveryID, name: s.SessionName,
    start: s.StartTime, duration: s.Duration, folder: s.FolderName}))};
}
"""


def _panopto_date(raw) -> str:
    """Panopto returns either an ISO string or ASP.NET /Date(ms)/."""
    if not raw:
        return ""
    m = re.match(r"/Date\((-?\d+)", str(raw))
    if m:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).strftime("%Y-%m-%d")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw))
    return m.group(1) if m else ""


def fetch_sessions(frame, folder_id: str | None) -> dict[str, dict]:
    """delivery_id -> {name, date, duration}. Empty dict when Panopto says no."""
    if not folder_id:
        return {}
    try:
        res = frame.evaluate(SESSIONS_JS, folder_id) or {}
    except Exception as e:  # noqa: BLE001
        print(f"  session metadata fetch failed: {e}")
        return {}
    if res.get("error"):
        print(f"  session metadata unavailable: {res['error']}")
        return {}
    out = {}
    for s in res.get("sessions", []):
        if not s.get("id"):
            continue
        out[s["id"].lower()] = {"name": s.get("name") or "",
                                "date": _panopto_date(s.get("start")),
                                "duration": s.get("duration")}
    print(f"  session metadata for {len(out)} recordings")
    return out


def scrape_folder(course_id: int, course_dir: Path,
                  headless: bool = True, max_videos: int | None = None,
                  tool_id: int | None = None) -> dict:
    """Launch LTI tool in Playwright + scrape Panopto folder contents."""
    from playwright.sync_api import sync_playwright

    tool_id = tool_id or find_panopto_tool_id(course_id)
    if not tool_id:
        return {"error": "no Panopto tool id found in Canvas tabs"}
    # Use the user-facing course-navigation URL (Canvas server fires LTI POST
    # internally with full session). Sessionless launch endpoint requires
    # admin token which we don't have.
    launch_url = (f"https://canvas.nd.edu/courses/{course_id}/"
                  f"external_tools/{tool_id}")
    print(f"Canvas LTI tab URL: {launch_url}")

    out_dir = course_dir / "_panopto"
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) "
                        "Gecko/20100101 Firefox/135.0"),
            viewport={"width": 1400, "height": 900},
        )
        ctx.add_cookies(_cookies_for_playwright())
        page = ctx.new_page()
        # Sessionless launch returns JSON {url: <one-time launch url>}
        # Easier: just navigate to the launch endpoint, Canvas redirects.
        try:
            page.goto(launch_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            browser.close()
            return {"error": f"launch goto failed: {e}"}
        page.wait_for_timeout(3000)
        print(f"  After Canvas tab load: {page.url[:140]}")
        # Canvas LTI 1.3 page contains hidden form auto-submitted to Panopto.
        # Find iframe pointing to Panopto, OR submit the form manually.
        try:
            # Wait for iframe with name="tool_content" or src containing panopto
            page.wait_for_selector(
                "iframe[name='tool_content'], iframe[src*='panopto']",
                timeout=20000,
            )
        except Exception:
            print("  no panopto iframe yet — trying form submit")
        # Find the LTI iframe
        frames = page.frames
        panopto_frame = None
        for f in frames:
            if "panopto" in (f.url or "").lower():
                panopto_frame = f
                print(f"  found Panopto frame: {f.url[:140]}")
                break
        if not panopto_frame:
            # Submit the LTI form (id varies, look for action containing panopto)
            try:
                form_action = page.evaluate(
                    "() => { const f = document.querySelector('form[action*=\"panopto\"]'); "
                    "return f ? f.action : null; }"
                )
                print(f"  LTI form action: {form_action}")
                if form_action:
                    page.evaluate(
                        "() => { const f = document.querySelector('form[action*=\"panopto\"]'); "
                        "if (f) f.submit(); }"
                    )
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)
            except Exception as e:
                print(f"  form-submit fallback failed: {e}")
        # Re-check frames
        for f in page.frames:
            if "panopto" in (f.url or "").lower():
                panopto_frame = f
                break
        sessions: dict[str, dict] = {}
        if panopto_frame:
            print(f"  Panopto frame URL: {panopto_frame.url[:140]}")
            fm = re.search(r"folderID=%22([0-9a-f-]{36})%22", panopto_frame.url or "")
            sessions = fetch_sessions(panopto_frame, fm.group(1) if fm else None)
            # Use the frame for evaluations
            try:
                videos_in_frame = panopto_frame.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href*=\"Viewer.aspx\"]'))"
                    "  .map(a => ({href: a.href, title: a.innerText.trim().slice(0,200)}))"
                ) or []
                if videos_in_frame:
                    print(f"  videos in frame: {len(videos_in_frame)}")
                    # Save HTML
                    (out_dir / "_folder_page.html").write_text(panopto_frame.content())
                    page = panopto_frame  # use frame from now on
            except Exception as e:
                print(f"  frame eval failed: {e}")
        print(f"  current URL after launch: {page.url if hasattr(page, 'url') else 'frame'}")
        # Save snapshot for debugging
        (out_dir / "_folder_page.html").write_text(page.content())
        # Look for video session links: typical pattern /Pages/Viewer.aspx?id=<GUID>
        videos = []
        try:
            ids = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href*=\"Viewer.aspx\"]'))"
                "  .map(a => ({href: a.href, title: a.innerText.trim().slice(0,200)}))"
            ) or []
            videos = ids
        except Exception:
            pass
        if not videos:
            # Try alternate selectors
            try:
                videos = page.evaluate(
                    "() => Array.from(document.querySelectorAll('[id^=\"detail-\"], .detail-title a'))"
                    "  .map(a => ({href: a.href || '', title: a.innerText.trim().slice(0,200)}))"
                ) or []
            except Exception:
                pass
        print(f"  found {len(videos)} video links")
        # The folder lists each recording twice (title link + thumbnail link).
        seen_hrefs = set()
        deduped = []
        for v in videos:
            key = re.search(r"id=([0-9a-f-]{36})", v.get("href", ""))
            key = key.group(1).lower() if key else v.get("href", "")
            if key in seen_hrefs:
                continue
            seen_hrefs.add(key)
            deduped.append(v)
        videos = deduped
        print(f"  {len(videos)} unique recordings")
        results = {"course_id": course_id, "tool_id": tool_id,
                   "panopto_url": page.url, "sessions": sessions,
                   "videos": videos[:max_videos] if max_videos else videos}
        (out_dir / "summary.json").write_text(json.dumps(results, indent=2))
        # For each video: open viewer in NEW tab, click transcript tab,
        # scrape rendered transcript pane innerText.
        viewer_page = ctx.new_page()
        for i, v in enumerate(results["videos"]):
            href = v.get("href", "")
            m = re.search(r"id=([0-9a-f-]{36})", href)
            if not m:
                continue
            delivery_id = m.group(1)
            v["delivery_id"] = delivery_id
            meta = sessions.get(delivery_id.lower(), {})
            v["session_name"] = meta.get("name", "")
            v["date"] = meta.get("date", "")
            # Date-stamped so a retrieval hit can say which lecture it came from.
            stem = f"{meta['date']}_{delivery_id[:8]}" if meta.get("date") else delivery_id
            txt_path = out_dir / f"{stem}.txt"
            if txt_path.exists() and txt_path.stat().st_size > 500:
                v["transcript_size"] = txt_path.stat().st_size
                v["cached"] = True
                continue
            try:
                viewer_page.goto(
                    f"https://notredame.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id={delivery_id}",
                    wait_until="domcontentloaded", timeout=45000,
                )
                # Wait for transcript pane to populate (8-15s typical)
                viewer_page.wait_for_timeout(12000)
                # Click transcript tab if collapsed
                try:
                    viewer_page.click(
                        "#transcriptTabHeader, [aria-controls='transcriptTabPane']",
                        timeout=3000,
                    )
                    viewer_page.wait_for_timeout(2000)
                except Exception:
                    pass
                text = viewer_page.evaluate(
                    "() => { const t = document.querySelector('#transcriptTabPane'); "
                    "return t ? t.innerText : ''; }"
                ) or ""
                if len(text) > 200:
                    txt_path.write_text(text)
                    v["transcript_size"] = len(text)
                else:
                    v["err"] = f"transcript empty ({len(text)} chars)"
            except Exception as e:
                v["err"] = str(e)[:120]
            if i % 3 == 2:
                print(f"  scraped {i+1}/{len(results['videos'])}", flush=True)
        viewer_page.close()
        (out_dir / "summary.json").write_text(json.dumps(results, indent=2))
        browser.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-id", type=int, required=True)
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--max", type=int)
    ap.add_argument("--tool-id", type=int,
                    help="Canvas external_tools id, when the tabs API does not expose it")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1
    res = scrape_folder(args.course_id, cdir, headless=not args.show,
                        max_videos=args.max, tool_id=args.tool_id)
    print(json.dumps({"summary": "see _panopto/summary.json",
                      "video_count": len(res.get("videos", [])),
                      "error": res.get("error")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
