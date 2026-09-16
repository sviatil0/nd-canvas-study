"""Capture google.com cookies via Playwright Firefox + ND SSO.

Usage:
    python google_auth.py             # opens Firefox, log in to Google → cookies saved
    python google_auth.py --check     # validate existing cookies

Cookies stored at secrets/google_cookies.json (gitignored).
Used by google_scrape.py for downloading Drive Docs/Slides via web export.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GOOGLE_BASE = "https://accounts.google.com"
COOKIE_FILE = Path(__file__).parent / "secrets" / "google_cookies.json"
# Long timeout per wait — user may take time on SSO + Duo. Polled in loop too.
WAIT_STEP_MS = 60 * 1000  # 60 sec per polling step
MAX_WAIT_TOTAL_MIN = 30   # give up after 30 min total


def _drive_session_valid(cookies: list) -> bool:
    """Check if cookies actually give a logged-in Drive session.

    Strategy: HEAD on /drive/my-drive. If response is 302 → ServiceLogin,
    invalid. If 200, valid. Also test a known-good signed-in endpoint:
    drive.google.com/embeddedfolderview returns 200 only when authed.
    """
    import requests
    jar = {c["name"]: c["value"] for c in cookies}
    try:
        r = requests.get("https://drive.google.com/drive/my-drive",
                         cookies=jar, headers={"User-Agent": "Mozilla/5.0"},
                         timeout=10, allow_redirects=False)
    except Exception:
        return False
    # 200 = authed, 302 to ServiceLogin = not authed
    if r.status_code == 200:
        return True
    if r.status_code in (301, 302, 303, 307):
        loc = r.headers.get("Location", "")
        if "ServiceLogin" in loc or "accounts.google.com" in loc:
            return False
        # Some other redirect — follow once
        try:
            r2 = requests.get(loc if loc.startswith("http") else f"https://drive.google.com{loc}",
                              cookies=jar, headers={"User-Agent": "Mozilla/5.0"},
                              timeout=10, allow_redirects=False)
            if r2.status_code == 200:
                return True
            loc2 = r2.headers.get("Location", "")
            return "ServiceLogin" not in loc2 and "accounts.google.com" not in loc2
        except Exception:
            return False
    return False


def login_and_save() -> int:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("Playwright missing. pip install playwright && playwright install firefox")
        return 1
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    import time as _t
    with sync_playwright() as p:
        try:
            browser = p.firefox.launch(headless=False)
        except Exception as e:
            print(f"Firefox launch failed ({e}); run: playwright install firefox")
            return 1
        ctx = browser.new_context()
        page = ctx.new_page()
        print("Opening Google login. Complete @nd.edu SSO + Duo in the window.")
        print(f"Polls every {WAIT_STEP_MS//1000}s until session valid (max {MAX_WAIT_TOTAL_MIN} min).")
        page.goto("https://drive.google.com/")
        # Poll: every 60s, capture cookies, validate via HTTP, stop when valid.
        deadline = _t.time() + MAX_WAIT_TOTAL_MIN * 60
        attempts = 0
        while _t.time() < deadline:
            attempts += 1
            cookies = ctx.cookies()
            google_cookies = [c for c in cookies if "google.com" in c.get("domain", "")]
            if google_cookies and _drive_session_valid(google_cookies):
                COOKIE_FILE.write_text(json.dumps(google_cookies, indent=2))
                print(f"\n✓ Validated! Saved {len(google_cookies)} cookies → {COOKIE_FILE}")
                browser.close()
                return 0
            try:
                page.wait_for_timeout(WAIT_STEP_MS)
            except PWTimeout:
                pass
            print(f"  [{attempts}] still waiting for valid Drive session…", flush=True)
        # Final attempt — save whatever we have, even if not validated.
        cookies = ctx.cookies()
        google_cookies = [c for c in cookies if "google.com" in c.get("domain", "")]
        if google_cookies:
            COOKIE_FILE.write_text(json.dumps(google_cookies, indent=2))
            print(f"⚠ Timeout after {MAX_WAIT_TOTAL_MIN} min. Saved {len(google_cookies)} cookies anyway.")
        else:
            print("No google.com cookies captured.")
        browser.close()
        return 1 if not google_cookies else 0


def check_cookies() -> int:
    import requests
    if not COOKIE_FILE.exists():
        print(f"No {COOKIE_FILE}")
        return 1
    raw = json.loads(COOKIE_FILE.read_text())
    jar = {c["name"]: c["value"] for c in raw}
    r = requests.get(
        "https://drive.google.com/drive/my-drive",
        cookies=jar,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
        timeout=15, allow_redirects=False,
    )
    if r.status_code in (200, 302) and "/ServiceLogin" not in r.headers.get("Location", ""):
        print(f"OK: google session valid (status {r.status_code}).")
        return 0
    print(f"Session invalid: HTTP {r.status_code}. Re-run without --check.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    return check_cookies() if args.check else login_and_save()


if __name__ == "__main__":
    sys.exit(main())
