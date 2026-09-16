"""Pull all Gradescope submissions for a course → cache in course_dir/_gradescope/.

Usage:
    python gradescope_pull.py --course-id 1219021 --course-dir downloads/128781_statistics
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gradescope_client import pull_course, list_courses


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-id", type=int, help="Gradescope course id")
    ap.add_argument("--course-dir", required=True,
                    help="Local Canvas course dir (downloads/<canvas_cid>_<slug>)")
    ap.add_argument("--list", action="store_true",
                    help="List GS courses you can access")
    args = ap.parse_args()

    if args.list:
        for c in list_courses():
            print(f"  {c['id']}  {c['name']}")
        return 0

    if not args.course_id:
        print("--course-id required (use --list to see options)")
        return 1
    cdir = Path(args.course_dir)
    cdir.mkdir(parents=True, exist_ok=True)

    def cb(i, n, name):
        print(f"[{i}/{n}] {name}", flush=True)

    summary = pull_course(args.course_id, cdir, progress_cb=cb)
    print(f"\nDone. {summary['wrong_total']} wrong-question incidents across "
          f"{len(summary['assignments'])} assignments.")
    print(f"Cache: {cdir / '_gradescope'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
