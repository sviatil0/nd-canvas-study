"""One-command exam prep for a Canvas class.

    python prep.py "statistics"
    python prep.py "statistics" --skip-sync   # don't redownload
    python prep.py --list                     # list active courses
    python prep.py --serve                    # also launch Django UI

Pipeline:
    1. Validate cookies; auto-login via Firefox if missing/stale.
    2. Resolve fuzzy class name → course id.
    3. Download course (modules walker).
    4. Build LLM bundles (categorized PDF text).
    5. Run topic-frequency gap analysis.
    6. Extract individual problems and emit STUDY_PLAN.md.
    7. Print top priorities + paths.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).parent
COOKIE_FILE = ROOT / "cookies.json"
DOWNLOADS = ROOT / "downloads"
PYTHON = sys.executable


def run(args: list[str], check: bool = True) -> int:
    print(f"$ {' '.join(args[1:] if args[0] == PYTHON else args)}")
    r = subprocess.run(args, cwd=ROOT)
    if check and r.returncode != 0:
        sys.exit(r.returncode)
    return r.returncode


def cookies_valid() -> bool:
    if not COOKIE_FILE.exists():
        return False
    try:
        import requests
        jar = {c["name"]: c["value"] for c in json.loads(COOKIE_FILE.read_text())}
        r = requests.get(
            "https://canvas.nd.edu/api/v1/users/self",
            cookies=jar, timeout=10,
            headers={"Accept": "application/json"},
        )
        return r.status_code == 200
    except Exception:
        return False


def ensure_login() -> None:
    if cookies_valid():
        print("✓ Canvas session OK")
        return
    print("Canvas session missing/stale — launching Firefox login…")
    run([PYTHON, "auth.py"])
    if not cookies_valid():
        sys.exit("Login did not complete.")


def list_courses() -> list[dict]:
    sys.path.insert(0, str(ROOT))
    from canvas_client import CanvasClient
    jar = {c["name"]: c["value"] for c in json.loads(COOKIE_FILE.read_text())}
    return CanvasClient(jar).list_courses()


def fuzzy_pick(courses: list[dict], query: str) -> dict:
    q = query.lower().strip()
    scored = []
    for c in courses:
        name = (c.get("name") or "").lower()
        code = (c.get("course_code") or "").lower()
        score = max(
            SequenceMatcher(None, q, name).ratio(),
            SequenceMatcher(None, q, code).ratio(),
            1.0 if q in name or q in code else 0.0,
        )
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    top = scored[0]
    if top[0] < 0.3:
        print("No good match. Candidates:")
        for s, c in scored[:10]:
            print(f"  {s:.2f}  {c['id']}  {c.get('name')}")
        sys.exit(1)
    print(f"Matched '{query}' → [{top[1]['id']}] {top[1].get('name')} (score {top[0]:.2f})")
    return top[1]


def slugify(s: str) -> str:
    from slugify import slugify as _s
    return _s(s, max_length=80) or "course"


def find_course_dir(cid: int) -> Path | None:
    if not DOWNLOADS.exists():
        return None
    for d in DOWNLOADS.iterdir():
        if d.is_dir() and d.name.startswith(f"{cid}_"):
            return d
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="class name (fuzzy match)")
    ap.add_argument("--list", action="store_true", help="list active courses and exit")
    ap.add_argument("--skip-sync", action="store_true", help="don't redownload")
    ap.add_argument("--skip-bundle", action="store_true")
    ap.add_argument("--skip-analyze", action="store_true")
    ap.add_argument("--skip-vector", action="store_true", help="skip building vector index")
    ap.add_argument("--with-panopto", action="store_true", help="also pull Panopto transcripts (interactive login)")
    ap.add_argument("--with-summaries", action="store_true", help="also generate concept summaries (slow)")
    ap.add_argument("--serve", action="store_true", help="launch Django UI after")
    ap.add_argument("--ask", help="after pipeline, run a semantic query against the index")
    args = ap.parse_args()

    ensure_login()
    courses = list_courses()

    if args.list or not args.query:
        print(f"\n{len(courses)} active courses:")
        for c in courses:
            print(f"  {c['id']}  {c.get('course_code','?'):<25}  {c.get('name')}")
        return 0

    course = fuzzy_pick(courses, args.query)
    cid = int(course["id"])
    name = slugify(course.get("name") or args.query)

    if not args.skip_sync:
        run([PYTHON, "download.py", "--course", str(cid), "--name", name])

    cdir = find_course_dir(cid)
    if not cdir:
        sys.exit(f"Download dir for course {cid} not found.")

    if not args.skip_bundle:
        # OCR scanned/handwritten exam PDFs first so bundles + analyze get clean text
        run([PYTHON, "bundle.py", "--course-dir", str(cdir)])
        run([PYTHON, "ocr.py", "--course-dir", str(cdir)], check=False)
        # Re-bundle so OCR'd text replaces garbled pypdf output
        run([PYTHON, "bundle.py", "--course-dir", str(cdir)])
    if not args.skip_analyze:
        run([PYTHON, "analyze.py", "--course-dir", str(cdir)])
    run([PYTHON, "problems.py", "--course-dir", str(cdir)])
    if args.with_panopto:
        run([PYTHON, "panopto.py", "--course-dir", str(cdir)])
    if not args.skip_vector:
        run([PYTHON, "vectorize.py", "--course-dir", str(cdir), "--rebuild"])
    if args.with_summaries:
        run([PYTHON, "summarize_topics.py", "--course-dir", str(cdir)])
    run([PYTHON, "render_graph.py", "--course-dir", str(cdir)], check=False)
    run([PYTHON, "likelihood.py", "--course-dir", str(cdir)], check=False)

    plan = cdir / "bundles" / "STUDY_PLAN.md"
    gap = cdir / "bundles" / "topic_gap_report.md"

    print("\n" + "=" * 60)
    print(f"Course folder: {cdir}")
    print(f"Study plan:    {plan}")
    print(f"Gap report:    {gap}")
    print(f"Bundles dir:   {cdir / 'bundles'}")
    print("=" * 60)

    # Print top 5 priorities
    gap_json = cdir / "bundles" / "topic_gap.json"
    if gap_json.exists():
        rows = json.loads(gap_json.read_text())
        top = [r for r in rows if r["share_gap_pp"] > 0 and r["exam_hits"] >= 3][:8]
        print("\nTop priorities (exam-heavy vs prep-light):")
        for r in top:
            print(f"  {r['topic']:30}  exam={r['exam_hits']:3}  prep={r['prep_hits']:3}  gap=+{r['share_gap_pp']:.2f}pp")

    if args.ask:
        run([PYTHON, "vectorize.py", "--course-dir", str(cdir), "--query", args.ask, "--k", "5"])

    if args.serve:
        print("\nLaunching Django UI on http://127.0.0.1:8000 (Ctrl-C to stop)…")
        subprocess.run([PYTHON, "manage.py", "runserver"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
