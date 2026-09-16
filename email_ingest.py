"""Pull a course's email traffic out of Gmail into the course folder.

Instructor email is course material: reading assignments, homework PDFs,
policy clarifications, deadline changes. None of it is on Canvas. This
module fetches those messages plus their attachments into

    <course-dir>/_email/messages/*.txt          headers + plain-text body
    <course-dir>/_email/attachments/*           original files (+ PDF renders)
    <course-dir>/_email/attachments_text/*.txt  text extracted from non-PDFs
    <course-dir>/_email/manifest.json           one record per message

so that bundle.py categorizes the PDFs, ocr_claude.py transcribes them, and
vectorize.py indexes the bodies alongside slides and transcripts.

Auth rides on the existing gmail-mcp OAuth tokens in ~/.config/gmail-mcp;
nothing new to authorize. Read-only scopes only.

Usage:
    python email_ingest.py --course-dir downloads/139705_algorithms \
        --account nd --query 'from:dchen@nd.edu' --query '"CSE 40113"'
    python email_ingest.py --course-dir <dir> --account nd --query '...' --since 2026/08/01
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

GMAIL_MCP_SRC = Path.home() / "Documents" / "code" / "gmail-mcp" / "src"
CONFIG_DIR = Path.home() / ".config" / "gmail-mcp"
# Office formats LibreOffice can render; converting gives ocr_claude.py a PDF
# to transcribe instead of leaving a binary the pipeline ignores.
OFFICE_EXT = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".rtf", ".odt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".heic"}
# Signature logos and tracking pixels ride along on nearly every message.
MIN_IMAGE_BYTES = 20_000
SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def _credentials(alias: str):
    """Reuse gmail-mcp's token store, including its refresh-and-save path."""
    sys.path.insert(0, str(GMAIL_MCP_SRC))
    try:
        from gmail_mcp import authstore  # type: ignore

        return authstore.get_credentials(alias)
    except Exception as e:  # noqa: BLE001 - fall back to a read-only load
        print(f"  (gmail_mcp.authstore unavailable: {e}; loading token directly)")
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(
            str(CONFIG_DIR / "tokens" / f"{alias}.json")
        )
        if not creds.valid:
            creds.refresh(Request())
        return creds


def gmail_service(alias: str):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(alias), cache_discovery=False)


def slugify(s: str, n: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:n].strip("-").lower() or "untitled"


def search_ids(svc, queries: list[str], since: str | None, max_results: int) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for q in queries:
        full_q = f"({q})" + (f" after:{since}" if since else "")
        page = None
        while True:
            resp = (
                svc.users()
                .messages()
                .list(userId="me", q=full_q, maxResults=100, pageToken=page)
                .execute()
            )
            for m in resp.get("messages", []):
                if m["id"] not in seen:
                    seen.add(m["id"])
                    ids.append(m["id"])
            page = resp.get("nextPageToken")
            if not page or len(ids) >= max_results:
                break
        print(f"  query {full_q!r} → {len(ids)} unique ids so far")
    return ids[:max_results]


def header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def walk_parts(payload: dict):
    """Yield every MIME part, depth first."""
    yield payload
    for part in payload.get("parts", []) or []:
        yield from walk_parts(part)


def extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to HTML stripped of markup."""
    plain, html = [], []
    for part in walk_parts(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(_decode(data).decode("utf-8", errors="replace"))
        elif mime == "text/html":
            html.append(_decode(data).decode("utf-8", errors="replace"))
    if plain:
        return "\n".join(plain).strip()
    if html:
        raw = "\n".join(html)
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(raw, "html.parser")
            for s in soup(["script", "style"]):
                s.decompose()
            return soup.get_text("\n", strip=True)
        except ImportError:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
    return ""


def office_to_pdf(src: Path, out_dir: Path) -> Path | None:
    exe = SOFFICE if Path(SOFFICE).exists() else "soffice"
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
            capture_output=True, timeout=180, check=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    soffice failed on {src.name}: {e}")
        return None
    pdf = out_dir / (src.stem + ".pdf")
    return pdf if pdf.exists() else None


def image_to_pdf(src: Path, out_dir: Path) -> Path | None:
    try:
        from PIL import Image

        img = Image.open(src)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        pdf = out_dir / (src.stem + ".pdf")
        img.save(pdf, "PDF", resolution=200)
        return pdf
    except Exception as e:  # noqa: BLE001
        print(f"    image→pdf failed on {src.name}: {e}")
        return None


def docx_text(src: Path) -> str:
    try:
        import docx  # python-docx

        d = docx.Document(str(src))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(t for t in parts if t.strip())
    except Exception as e:  # noqa: BLE001
        return f"[docx extract failed: {e}]"


def ingest(course_dir: Path, alias: str, queries: list[str], since: str | None,
           max_results: int, force: bool) -> dict:
    svc = gmail_service(alias)
    root = course_dir / "_email"
    msg_dir = root / "messages"
    att_dir = root / "attachments"
    txt_dir = root / "attachments_text"
    for d in (msg_dir, att_dir, txt_dir):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Searching {alias} mailbox…")
    ids = search_ids(svc, queries, since, max_results)
    print(f"\n{len(ids)} messages to fetch\n")

    manifest: list[dict] = []
    claimed: dict[str, str] = {}  # attachment filename -> owning message id
    for i, mid in enumerate(ids, 1):
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg.get("payload", {})
        subject = header(payload, "Subject") or "(no subject)"
        date_raw = header(payload, "Date")
        try:
            dt = parsedate_to_datetime(date_raw)
            date_str = dt.strftime("%Y-%m-%d")
            iso = dt.isoformat()
        except Exception:  # noqa: BLE001
            date_str, iso = "0000-00-00", date_raw
        stem = f"{date_str}_{mid[:10]}_{slugify(subject)}"
        body_file = msg_dir / f"{stem}.txt"

        record = {
            "id": mid,
            "thread_id": msg.get("threadId"),
            "date": iso,
            "subject": subject,
            "from": header(payload, "From"),
            "to": header(payload, "To"),
            "cc": header(payload, "Cc"),
            "body_path": str(body_file.relative_to(course_dir)),
            "attachments": [],
        }

        if force or not body_file.exists():
            body = extract_body(payload)
            body_file.write_text(
                f"# {subject}\n\n"
                f"From: {record['from']}\n"
                f"To: {record['to']}\n"
                f"Cc: {record['cc']}\n"
                f"Date: {iso}\n"
                f"Gmail-Message-Id: {mid}\n"
                f"Gmail-Thread-Id: {record['thread_id']}\n"
                f"\n---\n\n{body}\n"
            )

        for part in walk_parts(payload):
            filename = part.get("filename") or ""
            body_meta = part.get("body") or {}
            att_id = body_meta.get("attachmentId")
            if not filename or not att_id:
                continue
            ext = Path(filename).suffix.lower()
            size = body_meta.get("size", 0)
            if ext in IMAGE_EXT and size < MIN_IMAGE_BYTES:
                continue  # signature logo / tracking pixel
            # No message-id in the filename: bundle.py categorizes on the name,
            # and a hex id like "1a05e259" trips its /e\d+_/ exam pattern.
            base = f"{date_str}_{slugify(Path(filename).stem, 50)}"
            dest = att_dir / f"{base}{ext}"
            n = 2
            while claimed.get(dest.name, mid) != mid:
                dest = att_dir / f"{base}-{n}{ext}"
                n += 1
            claimed[dest.name] = mid
            if force or not dest.exists():
                data = (
                    svc.users().messages().attachments()
                    .get(userId="me", messageId=mid, id=att_id).execute()
                )
                dest.write_bytes(_decode(data["data"]))
            entry = {"filename": filename, "path": str(dest.relative_to(course_dir)),
                     "bytes": dest.stat().st_size}

            # Give the rest of the pipeline something it can read.
            if ext in OFFICE_EXT:
                pdf = office_to_pdf(dest, att_dir) if (force or not (att_dir / (dest.stem + ".pdf")).exists()) else att_dir / (dest.stem + ".pdf")
                if pdf and pdf.exists():
                    entry["pdf_path"] = str(pdf.relative_to(course_dir))
                if ext in {".docx", ".doc"}:
                    t = txt_dir / (dest.stem + ".txt")
                    if force or not t.exists():
                        t.write_text(docx_text(dest))
                    entry["text_path"] = str(t.relative_to(course_dir))
            elif ext in IMAGE_EXT:
                pdf = image_to_pdf(dest, att_dir)
                if pdf:
                    entry["pdf_path"] = str(pdf.relative_to(course_dir))
            record["attachments"].append(entry)

        manifest.append(record)
        n_att = len(record["attachments"])
        tag = f"  {n_att} att" if n_att else ""
        print(f"  [{i}/{len(ids)}] {date_str}  {subject[:62]:<62}{tag}")

    manifest.sort(key=lambda r: r["date"], reverse=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total_att = sum(len(r["attachments"]) for r in manifest)
    print(f"\nWrote {len(manifest)} messages and {total_att} attachments → {root}")
    return {"messages": len(manifest), "attachments": total_att}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--account", default="nd", help="gmail-mcp account alias")
    ap.add_argument("--query", action="append", required=True,
                    help="Gmail search query; repeatable, results are unioned")
    ap.add_argument("--since", help="Gmail after: date, e.g. 2026/08/01")
    ap.add_argument("--max", type=int, default=500)
    ap.add_argument("--force", action="store_true", help="refetch even if cached")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    ingest(cdir, args.account, args.query, args.since, args.max, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
