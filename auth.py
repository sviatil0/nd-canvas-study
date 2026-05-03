"""Capture canvas.nd.edu cookies by launching Firefox and waiting for SSO login.

Usage:
    python auth.py             # opens Firefox, you sign in, cookies saved
    python auth.py --check     # validate existing cookies.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CANVAS_BASE = "https://canvas.nd.edu"
COOKIE_FILE = Path(__file__).parent / "cookies.json"
LOGIN_TIMEOUT_MS = 5 * 60 * 1000


def login_and_save() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install -r requirements.txt && playwright install firefox")
        return 1

    with sync_playwright() as p:
        try:
            browser = p.firefox.launch(headless=False)
        except Exception as e:
            print(f"Firefox launch failed ({e}); install browser: playwright install firefox")
            return 1
        ctx = browser.new_context()
        page = ctx.new_page()
        print(f"Opening {CANVAS_BASE} ... complete ND SSO + Duo in the window.")
        page.goto(f"{CANVAS_BASE}/login")

        # Wait until we land on a logged-in Canvas URL (dashboard or course list).
        try:
            page.wait_for_url(
                lambda url: url.startswith(CANVAS_BASE) and "/login" not in url,
                timeout=LOGIN_TIMEOUT_MS,
            )
            page.wait_for_selector(
                "a[href*='/courses'], #global_nav_courses_link, body.context-base",
                timeout=LOGIN_TIMEOUT_MS,
            )
        except Exception as e:
            print(f"Did not detect successful login: {e}")
            browser.close()
            return 1

        cookies = ctx.cookies()
        browser.close()

    canvas_cookies = [c for c in cookies if "canvas.nd.edu" in c.get("domain", "")]
    if not canvas_cookies:
        print("No canvas.nd.edu cookies captured.")
        return 1
    COOKIE_FILE.write_text(json.dumps(canvas_cookies, indent=2))
    print(f"Saved {len(canvas_cookies)} cookies → {COOKIE_FILE}")
    return 0


def check_cookies() -> int:
    import requests

    if not COOKIE_FILE.exists():
        print(f"No {COOKIE_FILE}")
        return 1
    raw = json.loads(COOKIE_FILE.read_text())
    jar = {c["name"]: c["value"] for c in raw}
    r = requests.get(
        f"{CANVAS_BASE}/api/v1/users/self",
        cookies=jar,
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if r.status_code == 200:
        print(f"OK: logged in as {r.json().get('name')} ({r.json().get('login_id')})")
        return 0
    print(f"FAIL {r.status_code}: {r.text[:200]}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    return check_cookies() if args.check else login_and_save()


if __name__ == "__main__":
    sys.exit(main())
