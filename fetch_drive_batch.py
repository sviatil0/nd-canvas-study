"""Batch-fetch a list of Google Drive doc/slides IDs via Playwright mobilebasic.

Reads `<kind>\\t<doc_id>` per line from stdin or --ids-file. Saves text to
<out-dir>/<kind>/<doc_id>.txt.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from gdoc_browser import _build_context, _doc_url


def fetch(kind: str, doc_id: str, ctx, timeout_ms: int = 30000) -> tuple[str, bytes | None]:
    """Returns (text, pdf_bytes_if_slides). Slides export as PDF."""
    if kind == "presentation":
        url = f"https://docs.google.com/presentation/d/{doc_id}/export/pdf"
        try:
            resp = ctx.request.get(url, timeout=120000)
        except Exception as e:
            raise RuntimeError(f"req {url}: {e}")
        if resp.status != 200 or len(resp.body()) < 1000:
            raise RuntimeError(f"http {resp.status}, {len(resp.body())} B")
        if resp.body()[:4] != b"%PDF":
            raise RuntimeError(f"not a PDF (first 4 bytes: {resp.body()[:4]})")
        return ("", resp.body())
    # documents/spreadsheets — mobilebasic
    page = ctx.new_page()
    url = _doc_url(doc_id, kind)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as e:
        page.close()
        raise RuntimeError(f"goto {url}: {e}")
    if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
        page.close()
        raise RuntimeError(f"redirected to login")
    page.wait_for_timeout(1500)
    text = ""
    try:
        text = page.evaluate("() => document.body.innerText") or ""
    except Exception:
        pass
    page.close()
    return (text, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True,
                    help="File with `<kind>\\t<doc_id>` per line")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for line in Path(args.ids_file).read_text().splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        kind, did = line.split("\t", 1)
        items.append((kind.strip(), did.strip()))
    print(f"Fetching {len(items)} Drive items...")
    from playwright.sync_api import sync_playwright
    ok = fail = 0
    with sync_playwright() as p:
        browser, ctx = _build_context(p, headless=not args.show)
        for i, (kind, did) in enumerate(items):
            kind_dir = out / kind
            kind_dir.mkdir(parents=True, exist_ok=True)
            ext = "pdf" if kind == "presentation" else "txt"
            dest = kind_dir / f"{did}.{ext}"
            if dest.exists() and dest.stat().st_size > 500 and not args.force:
                print(f"  [{i+1}/{len(items)}] [cached] {kind}/{did[:10]}")
                ok += 1
                continue
            try:
                text, pdf_bytes = fetch(kind, did, ctx)
                if pdf_bytes:
                    dest.write_bytes(pdf_bytes)
                    ok += 1
                    print(f"  [{i+1}/{len(items)}] ✓ {kind}/{did[:10]}: {len(pdf_bytes)} B (pdf)",
                          flush=True)
                elif len(text) >= 100:
                    dest.write_text(text)
                    ok += 1
                    print(f"  [{i+1}/{len(items)}] ✓ {kind}/{did[:10]}: {len(text)} chars",
                          flush=True)
                else:
                    fail += 1
                    print(f"  [{i+1}/{len(items)}] ✗ {kind}/{did[:10]}: tiny ({len(text)})")
            except Exception as e:
                fail += 1
                print(f"  [{i+1}/{len(items)}] ✗ {kind}/{did[:10]}: {e}")
            time.sleep(0.6)
        browser.close()
    print(f"\n{ok}/{ok+fail} ok")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
