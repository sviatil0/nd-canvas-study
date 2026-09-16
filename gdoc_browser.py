"""Headless Playwright GDoc/GSlides text extractor.

Why Playwright: docs.google.com/document/d/<id>/export?format=txt rejects
plain cookie auth (returns Sign-in HTML even with valid SID/Secure-1PSID).
Loading the doc in a real browser session works because the doc UI itself
fires the proper auth handshake.

Strategy: open `https://docs.google.com/document/d/<id>/edit`, wait for
content frame, dump rendered text.

Usage:
    python gdoc_browser.py --root "/path/to/synced/folder" [--limit N]
    python gdoc_browser.py --doc-id <DOC_ID> --out /tmp/x.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).parent
COOKIE_FILE = ROOT_DIR / "secrets" / "google_cookies.json"


def _doc_id_from_pointer(p: Path) -> tuple[str | None, str]:
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return (None, "unknown")
    doc_id = data.get("doc_id")
    suffix = p.suffix.lower()
    kind = {".gdoc": "document", ".gsheet": "spreadsheets",
            ".gslides": "presentation"}.get(suffix, "unknown")
    return (doc_id, kind)


def _doc_url(doc_id: str, kind: str) -> str:
    if kind == "document":
        # mobilebasic renders pure HTML with all paragraph text — no editor
        # iframe gymnastics, no auth handshake issues.
        return f"https://docs.google.com/document/d/{doc_id}/mobilebasic"
    if kind == "presentation":
        return f"https://docs.google.com/presentation/d/{doc_id}/mobilepresent"
    if kind == "spreadsheets":
        return f"https://docs.google.com/spreadsheets/d/{doc_id}/htmlview"
    return f"https://drive.google.com/file/d/{doc_id}"


def _build_context(p, headless: bool = True):
    """Return (browser, context) with Google cookies loaded."""
    browser = p.firefox.launch(headless=headless)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) "
                   "Gecko/20100101 Firefox/135.0",
        viewport={"width": 1280, "height": 800},
    )
    if COOKIE_FILE.exists():
        raw = json.loads(COOKIE_FILE.read_text())
        # Playwright wants sameSite normalized + domain leading dot acceptable
        cookies = []
        for c in raw:
            cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".google.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "Lax").capitalize() if isinstance(c.get("sameSite"), str) else "Lax",
            })
        ctx.add_cookies(cookies)
    return browser, ctx


def fetch_doc_text(doc_id: str, kind: str, headless: bool = True,
                   timeout_ms: int = 25000) -> str:
    """Open doc in Playwright + return rendered text."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, ctx = _build_context(p, headless=headless)
        page = ctx.new_page()
        url = _doc_url(doc_id, kind)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            browser.close()
            raise RuntimeError(f"goto {url} failed: {e}")
        # Detect login redirect
        if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
            browser.close()
            raise RuntimeError(f"redirected to login: {page.url}")
        # mobilebasic renders pure HTML — small wait for fonts/images.
        page.wait_for_timeout(1500)
        try:
            text = page.evaluate("() => document.body.innerText") or ""
        except Exception:
            text = ""
        browser.close()
        return text or ""


def walk_and_extract(root: Path, headless: bool = True, force: bool = False,
                     limit: int | None = None,
                     delay_sec: float = 0.7) -> dict:
    pointers = sorted(set(
        list(root.rglob("*.gdoc")) +
        list(root.rglob("*.gslides"))
    ))
    if limit:
        pointers = pointers[:limit]
    print(f"Found {len(pointers)} pointer files under {root}")
    results = []
    ok_n = fail_n = 0
    for i, p in enumerate(pointers):
        rel = p.relative_to(root)
        out = p.with_suffix(p.suffix + ".browser.txt")
        if out.exists() and out.stat().st_size > 200 and not force:
            print(f"  [{i+1}/{len(pointers)}] [cached] {rel}", flush=True)
            ok_n += 1
            results.append({"path": str(p), "ok": True, "cached": True})
            continue
        doc_id, kind = _doc_id_from_pointer(p)
        if not doc_id:
            fail_n += 1
            print(f"  [{i+1}/{len(pointers)}] ✗ {rel}: no doc_id")
            continue
        try:
            text = fetch_doc_text(doc_id, kind, headless=headless)
        except Exception as e:
            fail_n += 1
            print(f"  [{i+1}/{len(pointers)}] ✗ {rel}: {e}")
            continue
        if len(text) < 100:
            fail_n += 1
            print(f"  [{i+1}/{len(pointers)}] ✗ {rel}: tiny ({len(text)} chars)")
            continue
        out.write_text(text)
        ok_n += 1
        print(f"  [{i+1}/{len(pointers)}] ✓ {rel}: {len(text)} chars", flush=True)
        time.sleep(delay_sec)
    return {"total": len(pointers), "ok": ok_n, "failed": fail_n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--doc-id")
    ap.add_argument("--kind", default="document",
                    choices=["document", "presentation", "spreadsheets"])
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--show", action="store_true",
                    help="run NON-headless (visible browser)")
    args = ap.parse_args()
    headless = not args.show

    if args.doc_id:
        text = fetch_doc_text(args.doc_id, args.kind, headless=headless)
        if args.out:
            Path(args.out).write_text(text)
            print(f"wrote {len(text)} chars → {args.out}")
        else:
            print(text[:2000])
        return 0

    if not args.root:
        print("--root or --doc-id required")
        return 1
    summary = walk_and_extract(Path(args.root), headless=headless,
                               force=args.force, limit=args.limit)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
