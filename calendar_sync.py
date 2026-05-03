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


def to_ics(events: list[dict], course_label: str, out_path: Path) -> Path:
    """Generate .ics file usable in Google/Apple/Outlook/anywhere."""
    from datetime import datetime as _dt
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//nd-canvas-study//{course_label}//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:[{course_label}] ND Canvas",
    ]
    now = _dt.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for i, e in enumerate(events):
        date_compact = e["date"].replace("-", "")
        uid = f"ndcanvas-{course_label}-{date_compact}-{i}@local"
        title = e["title"].replace("\n", " ").replace(",", "\\,")
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{now}")
        if e.get("datetime_iso"):
            dt = _dt.fromisoformat(e["datetime_iso"])
            start = dt.strftime("%Y%m%dT%H%M%S")
            end = (dt.replace(hour=(dt.hour + 1) % 24)).strftime("%Y%m%dT%H%M%S")
            lines.append(f"DTSTART;TZID=America/New_York:{start}")
            lines.append(f"DTEND;TZID=America/New_York:{end}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{date_compact}")
            lines.append(f"DTEND;VALUE=DATE:{date_compact}")
        lines.append(f"SUMMARY:[{course_label}] {title}")
        lines.append(f"DESCRIPTION:Auto-generated from Canvas. Source: {e.get('src', '')}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


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
    ap.add_argument("--ics", action="store_true",
                    help="Skip API call, just write .ics file")
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

    if args.ics:
        ics_path = cdir / "bundles" / f"{label}.ics"
        to_ics(uniq, label, ics_path)
        print(f"\nWrote {len(uniq)} events to {ics_path}")
        print(f"Import via: open '{ics_path}'  (Mac default Calendar app)")
        print(f"  or upload to https://calendar.google.com/calendar/r/settings/export")
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
