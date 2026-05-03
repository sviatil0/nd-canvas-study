"""Real-browser Panopto transcript scraper (LTI launch path).

For each delivery_id, opens the Canvas external_tools/retrieve URL in
Playwright Firefox so the LTI launch binds Panopto to the Canvas course.
Then queries Panopto's DeliveryInfo.aspx (in the page's authed context)
and walks the JSON for caption track URLs (HasCaptions / AvailableCaptions).

NOTE: This works only if the instructor enabled captions on the video.
For ACMS 30440 Spring 2026, every video returns
  HasCaptions: false, AvailableCaptions: []
so this script will write 0 transcripts. The pipeline is correct; the
source material has no captions to scrape.

Usage:
    python panopto_browser.py --course-dir downloads/128781_statistics
    python panopto_browser.py --course-dir <dir> --limit 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from panopto import (  # noqa: E402
    extract_delivery_ids,
    fetch_page_body,
    find_panopto_page_slugs,
    list_panopto_pages,
    load_canvas_session,
)


def collect_delivery_ids(cdir: Path) -> list[tuple[str, str]]:
    """Return [(slug, delivery_id), ...]."""
    course_id = int(cdir.name.split("_", 1)[0])
    canvas = load_canvas_session()
    sources: dict[str, str] = {}
    for p in list_panopto_pages(course_id, canvas):
        sources[p["url"]] = fetch_page_body(course_id, p["url"], canvas)
    for slug in find_panopto_page_slugs(cdir):
        if slug in sources:
            continue
        try:
            sources[slug] = fetch_page_body(course_id, slug, canvas)
        except Exception:
            pass
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for slug, body in sources.items():
        for vid in extract_delivery_ids(body):
            if vid in seen:
                continue
            seen.add(vid)
            out.append((slug, vid))
    return out


def parse_srt(srt_text: str) -> str:
    lines: list[str] = []
    for ln in srt_text.splitlines():
        ln = ln.strip()
        if not ln or ln.isdigit() or "-->" in ln or ln.startswith("WEBVTT"):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def parse_caption_json(data) -> str:
    """Panopto sometimes returns JSON: {Captions: [{Caption: "..."}]} etc."""
    out: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                t = item.get("Caption") or item.get("Text") or item.get("text")
                if t:
                    out.append(str(t).strip())
    elif isinstance(data, dict):
        caps = data.get("Captions") or data.get("captions") or data.get("d")
        if isinstance(caps, list):
            return parse_caption_json(caps)
    return "\n".join(out).strip()


def scrape(cdir: Path, limit: int | None = None) -> int:
    from playwright.sync_api import sync_playwright

    course_id = int(cdir.name.split("_", 1)[0])
    out_dir = cdir / "transcripts"
    out_dir.mkdir(exist_ok=True)

    deliveries = collect_delivery_ids(cdir)
    if limit:
        deliveries = deliveries[:limit]
    print(f"{len(deliveries)} videos to process")

    canvas_cookies = json.loads((ROOT / "cookies.json").read_text())
    panopto_cookies_file = ROOT / "panopto_cookies.json"
    panopto_cookies = json.loads(panopto_cookies_file.read_text()) if panopto_cookies_file.exists() else []

    written = 0
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        ctx = browser.new_context()

        # Inject both sets of cookies
        all_cookies = []
        for c in canvas_cookies + panopto_cookies:
            d = c.get("domain") or ""
            if not d.startswith("."):
                d = "." + d if d else d
            all_cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": d,
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": (c.get("sameSite") or "None").capitalize(),
            })
        try:
            ctx.add_cookies(all_cookies)
        except Exception as e:
            print(f"cookie inject warning: {e}")

        page = ctx.new_page()
        captured: dict[str, bytes] = {}

        def on_response(resp):
            url = resp.url
            if "Caption.ashx" in url or "Transcript" in url or "captionsApi" in url.lower():
                if any(vid in url for vid in [d[1] for d in deliveries]):
                    pass  # delivery id may not match session id
                try:
                    body = resp.body()
                    captured[url] = body
                except Exception:
                    pass

        page.on("response", on_response)

        for slug, vid in deliveries:
            dest = out_dir / f"{slug}__{vid}.txt"
            if dest.exists() and dest.stat().st_size > 0:
                print(f"  skip cached {vid}")
                continue
            captured.clear()
            launch = (
                f"https://canvas.nd.edu/courses/{course_id}/external_tools/retrieve"
                f"?display=borderless&url="
                + quote(
                    f"https://notredame.hosted.panopto.com/Panopto/LTI/LTI.aspx"
                    f"?custom_context_delivery={vid}",
                    safe="",
                )
            )
            print(f"\n[{slug} / {vid}]")
            print(f"  launching ...")
            try:
                page.goto(launch, wait_until="networkidle", timeout=60_000)
            except Exception as e:
                print(f"  navigate failed: {e}")
                continue

            # Try to derive sessionId from URL or page
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            cur = page.url
            m = re.search(r"id=([0-9a-f-]{36})", cur)
            session_id = m.group(1) if m else None
            if not session_id:
                # Try extracting from page content
                content = page.content()
                m2 = re.search(r"sessionId\s*[:=]\s*['\"]([0-9a-f-]{36})", content, re.I)
                if m2:
                    session_id = m2.group(1)
            print(f"  session_id={session_id}")

            text: str | None = None
            # Try DeliveryInfo (LTI-bound, returns JSON with caption URLs)
            if session_id:
                info_url = (
                    f"https://notredame.hosted.panopto.com/Panopto/Pages/Viewer/DeliveryInfo.aspx"
                    f"?deliveryId={session_id}&isLiveNotes=false&refreshAuthCookie=true"
                    f"&isActiveBroadcast=false&isEditing=false"
                    f"&isKollectiveAgentInstalled=false&isEmbed=false&responseType=json"
                )
                try:
                    info = page.evaluate(
                        """async (u) => {
                            const r = await fetch(u, {credentials: 'include', method: 'POST'});
                            return {status: r.status, text: await r.text()};
                        }""",
                        info_url,
                    )
                    if info.get("status") == 200:
                        try:
                            data = json.loads(info["text"])
                        except Exception:
                            data = {}
                        delivery = (data or {}).get("Delivery") or {}
                        # Captions usually at Delivery.OcrLanguageDataList or Captions
                        cap_urls = []
                        # Hunt for any caption-track URL inside the JSON
                        def walk(obj):
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    if isinstance(v, str) and ("caption" in v.lower() or v.endswith(".vtt") or v.endswith(".srt")):
                                        cap_urls.append(v)
                                    walk(v)
                            elif isinstance(obj, list):
                                for it in obj:
                                    walk(it)
                        walk(data)
                        for u in cap_urls:
                            full = u if u.startswith("http") else f"https://notredame.hosted.panopto.com{u}"
                            try:
                                cap = page.evaluate(
                                    """async (u) => {
                                        const r = await fetch(u, {credentials: 'include'});
                                        return {status: r.status, text: await r.text()};
                                    }""",
                                    full,
                                )
                                if cap.get("status") == 200 and cap.get("text"):
                                    parsed = parse_srt(cap["text"])
                                    if not parsed:
                                        try:
                                            parsed = parse_caption_json(json.loads(cap["text"]))
                                        except Exception:
                                            pass
                                    if parsed and len(parsed) > 100:
                                        text = parsed
                                        break
                            except Exception:
                                pass
                except Exception as e:
                    print(f"  DeliveryInfo failed: {e}")

            # Fallback: try common Panopto transcript endpoints directly
            if not text and session_id:
                for ep_url in [
                    f"https://notredame.hosted.panopto.com/Panopto/Pages/Transcript.aspx?id={session_id}&format=text",
                    f"https://notredame.hosted.panopto.com/Panopto/Services/Captions/CaptionFile.svc/{session_id}/0",
                ]:
                    try:
                        r = page.evaluate(
                            """async (u) => {
                                const r = await fetch(u, {credentials: 'include'});
                                return {status: r.status, text: await r.text()};
                            }""",
                            ep_url,
                        )
                        if r.get("status") == 200 and r.get("text") and "404" not in r["text"][:200]:
                            parsed = parse_srt(r["text"])
                            if parsed and len(parsed) > 200:
                                text = parsed
                                break
                    except Exception:
                        pass

            if not text and captured:
                for url, body in captured.items():
                    raw = body.decode("utf-8", errors="ignore")
                    if "<html" in raw[:200].lower() or "404" in raw[:200]:
                        continue
                    parsed = parse_srt(raw)
                    if not parsed:
                        try:
                            parsed = parse_caption_json(json.loads(raw))
                        except Exception:
                            parsed = ""
                    if parsed and len(parsed) > 200:
                        text = parsed
                        break

            if text:
                dest.write_text(text)
                print(f"  ✓ wrote {len(text)} chars")
                written += 1
            else:
                print(f"  ✗ no captions captured")

        browser.close()
    print(f"\nDone. Wrote {written} transcripts → {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--limit", type=int, help="cap number of videos (for testing)")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")
    return scrape(cdir, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
