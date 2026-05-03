"""Sync class dates to Google Calendar.

Reads dates from CLASS_INFO.md (Important dates section) + Canvas assignments
(due_at field). Uses ADC for Calendar API.

One-time scopes setup:
    gcloud auth application-default login \
        --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/calendar.events

Usage:
    python calendar_sync.py --course-dir downloads/128781_statistics
    python calendar_sync.py --course-dir <dir> --calendar primary --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"),
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:,?\s*(20\d{2}))?\b", re.I),
]
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(text: str, default_year: int = 2026) -> str | None:
    text = text.strip()
    # ISO YYYY-MM-DD
    m = re.match(r"^(20\d{2})-(\d{1,2})-(\d{1,2})$", text)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])).date().isoformat()
        except ValueError:
            pass
    # MM/DD/YYYY or MM/DD
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", text)
    if m:
        y = int(m[3]) if m[3] else default_year
        if y < 100:
            y += 2000
        try:
            return datetime(y, int(m[1]), int(m[2])).date().isoformat()
        except ValueError:
            pass
    # Month name
    m = re.match(r"^([a-zA-Z]+)\.?\s+(\d{1,2})(?:,?\s*(20\d{2}))?$", text)
    if m:
        mon = MONTHS.get(m[1][:3].lower())
        if mon:
            y = int(m[3]) if m[3] else default_year
            try:
                return datetime(y, mon, int(m[2])).date().isoformat()
            except ValueError:
                pass
    return None


def extract_events_from_class_info(md: str) -> list[dict]:
    """Find lines under '## Important dates' or 'Date & Time:' lines anywhere."""
    events: list[dict] = []
    in_dates = False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_dates = "important date" in s.lower()
            continue
        # Bullets in dates section: "- 2026-05-04: Final exam"
        if in_dates and s.startswith("- "):
            body = s[2:].strip()
            colon = body.find(":")
            if colon > 0:
                date_str = body[:colon].strip().strip("`")
                title = body[colon + 1:].strip()
                iso = parse_date(date_str)
                if iso and title:
                    events.append({"date": iso, "title": title, "src": "class_info"})
        # "Date & Time: Monday, May 4 from 10:30 am – 12:30 pm"
        m = re.search(
            r"Date\s*&\s*Time:?\s*\*?\*?\s*[A-Za-z]+,?\s*"
            r"([A-Za-z]+\.?\s+\d{1,2})(?:,?\s*(20\d{2}))?"
            r"\s*(?:from|at)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
            line, re.I)
        if m:
            iso = parse_date(f"{m[1]} {m[2] or 2026}".strip())
            if iso:
                events.append({"date": iso, "time": m[3].strip().lower(),
                               "title": "Exam (from class info)", "src": "class_info"})
    return events


def extract_events_from_assignments(course_dir: Path) -> list[dict]:
    f = course_dir / "assignments.json"
    if not f.exists():
        return []
    out = []
    try:
        for a in json.loads(f.read_text()):
            due = a.get("due_at")
            name = a.get("name") or f"assignment {a.get('id')}"
            if not due:
                continue
            try:
                dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            except Exception:
                continue
            out.append({
                "date": dt.date().isoformat(),
                "time": dt.strftime("%I:%M %p").lower(),
                "title": name,
                "src": "canvas_assignment",
                "datetime_iso": dt.isoformat(),
            })
    except Exception:
        pass
    return out


def get_calendar_service():
    from google.auth import default
    from googleapiclient.discovery import build
    creds, _ = default(scopes=["https://www.googleapis.com/auth/calendar.events"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def upsert_event(service, calendar_id: str, event: dict, course_label: str,
                 dry_run: bool = False) -> str:
    title = f"[{course_label}] {event['title']}"
    # Check existing by extended-property tag
    tag = f"ndcanvas:{course_label}:{event['date']}:{event['title'][:40]}"
    body = {
        "summary": title,
        "description": f"Auto-synced from Canvas via nd-canvas-study.\nSource: {event.get('src','')}",
        "extendedProperties": {"private": {"ndcanvas_tag": tag}},
    }
    if event.get("datetime_iso"):
        end = datetime.fromisoformat(event["datetime_iso"]) + timedelta(hours=1)
        body["start"] = {"dateTime": event["datetime_iso"]}
        body["end"] = {"dateTime": end.isoformat()}
    else:
        body["start"] = {"date": event["date"]}
        body["end"] = {"date": event["date"]}

    if dry_run:
        return f"DRY: would upsert {title} on {event['date']}"

    existing = service.events().list(
        calendarId=calendar_id,
        privateExtendedProperty=f"ndcanvas_tag={tag}",
        maxResults=1,
    ).execute().get("items", [])
    if existing:
        ev = service.events().update(
            calendarId=calendar_id, eventId=existing[0]["id"], body=body,
        ).execute()
        return f"UPDATED {title}"
    ev = service.events().insert(calendarId=calendar_id, body=body).execute()
    return f"CREATED {title}  →  {ev.get('htmlLink','')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--calendar", default="primary")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cdir = Path(args.course_dir)
    label = cdir.name.split("_", 1)[1] if "_" in cdir.name else cdir.name

    info_md = ""
    info_path = cdir / "bundles" / "CLASS_INFO.md"
    if info_path.exists():
        info_md = info_path.read_text()

    events = extract_events_from_class_info(info_md) + extract_events_from_assignments(cdir)
    # Dedupe by (date, title)
    seen = set()
    uniq = []
    for e in events:
        key = (e["date"], e["title"][:60])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)

    print(f"Found {len(uniq)} unique events for {label}:")
    for e in uniq:
        print(f"  {e['date']:12}  {e['title'][:80]:80}  ({e['src']})")

    if args.dry_run:
        print("\n--dry-run set, not touching Calendar.")
        return 0

    if not uniq:
        return 0

    service = get_calendar_service()
    print()
    for e in uniq:
        try:
            print("  " + upsert_event(service, args.calendar, e, label))
        except Exception as exc:
            print(f"  FAIL {e['title']}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
