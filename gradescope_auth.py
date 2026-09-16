"""Capture gradescope.com cookies via Playwright Firefox + ND SSO.

Usage:
    python gradescope_auth.py             # opens Firefox, you sign in, cookies saved
    python gradescope_auth.py --check     # validate existing cookies
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GS_BASE = "https://www.gradescope.com"
COOKIE_FILE = Path(__file__).parent / "secrets" / "gradescope_cookies.json"
LOGIN_TIMEOUT_MS = 5 * 60 * 1000


def login_and_save() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright missing. pip install playwright && playwright install firefox")
        return 1

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.firefox.launch(headless=False)
        except Exception as e:
            print(f"Firefox launch failed ({e}); run: playwright install firefox")
            return 1
        ctx = browser.new_context()
        page = ctx.new_page()
        print(f"Opening {GS_BASE} — log in with your school (ND) → SSO + Duo.")
        print("After dashboard loads, this script auto-captures cookies.")
        page.goto(f"{GS_BASE}/login")

        try:
            # Logged in once we're on gradescope.com (not login/saml/okta).
            page.wait_for_url(
                lambda url: (
                    url.startswith(GS_BASE)
                    and "/login" not in url
                    and "/saml" not in url
                    and "okta" not in url
                ),
                timeout=LOGIN_TIMEOUT_MS,
            )
            # Confirm we see the courses dashboard (or any signed-in page).
            page.wait_for_selector(
                "a[href*='/courses/'], .courseList, .courseBox, .userMenu, h1",
                timeout=LOGIN_TIMEOUT_MS,
            )
            page.goto(f"{GS_BASE}/account", wait_until="load")
        except Exception as e:
            print(f"Login not detected: {e}")
            # Save whatever we have anyway
            try:
                cookies = ctx.cookies()
                if cookies:
                    gs_cookies = [c for c in cookies if "gradescope.com" in c.get("domain", "")]
                    if gs_cookies:
                        COOKIE_FILE.write_text(json.dumps(gs_cookies, indent=2))
                        print(f"Saved {len(gs_cookies)} cookies anyway → {COOKIE_FILE}")
            except Exception:
                pass
            browser.close()
            return 1

        cookies = ctx.cookies()
        browser.close()

    gs_cookies = [c for c in cookies if "gradescope.com" in c.get("domain", "")]
    if not gs_cookies:
        print("No gradescope.com cookies captured.")
        return 1
    COOKIE_FILE.write_text(json.dumps(gs_cookies, indent=2))
    print(f"Saved {len(gs_cookies)} cookies → {COOKIE_FILE}")
    return 0


def check_cookies() -> int:
    import requests

    if not COOKIE_FILE.exists():
        print(f"No {COOKIE_FILE}")
        return 1
    raw = json.loads(COOKIE_FILE.read_text())
    jar = {c["name"]: c["value"] for c in raw}
    r = requests.get(
        f"{GS_BASE}/account",
        cookies=jar,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
        timeout=15,
        allow_redirects=False,
    )
    if r.status_code == 200 and "log out" in r.text.lower():
        print("OK: gradescope session valid.")
        return 0
    print(f"Session invalid: HTTP {r.status_code}. Re-run without --check to log in.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    return check_cookies() if args.check else login_and_save()


if __name__ == "__main__":
    sys.exit(main())
