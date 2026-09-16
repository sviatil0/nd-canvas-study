"""Recursive external course-website scraper.

Mirrors a course homepage (and same-host subpages it links to) into
`<course_dir>/_external/<host>/` so the rest of the pipeline (bundle, OCR,
vectorize) can include those materials.

Usage:
    python external_scrape.py --course-dir downloads/123_paradigms \\
        --url https://nd.edu/~prof/paradigms/ --max-pages 200

Behaviour:
  - Same-host only by default (--allow-host to add more).
  - Skips already-cached files unless --force.
  - Saves PDFs to `_external/<host>/_files/<basename>` (deduped by content hash).
  - Saves HTML pages to `_external/<host>/<url-path-as-filename>.html`.
  - Writes a manifest.json with URL → local path mapping.
  - 1s polite delay between requests; honors common 4xx as "skip".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests

UA = "Mozilla/5.0 (compatible; nd-canvas-study/1.0)"
PDF_HEAD = b"%PDF"


def _safe_name(url: str) -> str:
    """Convert URL path into safe filename."""
    p = urlparse(url)
    raw = (p.path or "/") + (("?" + p.query) if p.query else "")
    if raw.endswith("/"):
        raw += "index"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("_")
    if not name.endswith(".html"):
        name += ".html"
    return name[:200]


def _norm(url: str, base: str) -> str | None:
    """Resolve relative URL against base; strip fragments. Returns absolute or None."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    abs_url = urljoin(base, url)
    abs_url, _ = urldefrag(abs_url)
    return abs_url


def _is_pdf(content: bytes, content_type: str) -> bool:
    if content[:4] == PDF_HEAD:
        return True
    return "application/pdf" in (content_type or "").lower()


def _is_html(content_type: str) -> bool:
    return "text/html" in (content_type or "").lower()


def crawl(start_url: str, course_dir: Path,
          max_pages: int = 200, allow_hosts: list[str] | None = None,
          force: bool = False, delay_sec: float = 1.0,
          allowed_extensions: tuple[str, ...] = (".pdf", ".html", ".htm", ".tex", ".txt", ".md", ".ipynb")) -> dict:
    """BFS crawl. Returns summary dict."""
    parsed_start = urlparse(start_url)
    base_host = parsed_start.netloc
    hosts = {base_host}
    if allow_hosts:
        hosts.update(allow_hosts)

    out_dir = course_dir / "_external" / base_host
    out_dir.mkdir(parents=True, exist_ok=True)
    pdfs_dir = out_dir / "_files"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest: dict = json.loads(manifest_path.read_text()) if (manifest_path.exists() and not force) else {
        "start_url": start_url,
        "base_host": base_host,
        "fetched": {},  # url -> {local_path, content_type, ts, size, kind}
    }

    queue: list[str] = [start_url]
    seen: set[str] = set(manifest["fetched"].keys()) if not force else set()
    fetched_now = 0
    failed: list[tuple[str, str]] = []

    sess = requests.Session()
    sess.headers["User-Agent"] = UA

    while queue and fetched_now < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        # Filter out non-likely-content URLs by extension
        path_lower = urlparse(url).path.lower()
        ext = Path(path_lower).suffix
        if ext and ext not in allowed_extensions:
            continue
        seen.add(url)
        try:
            r = sess.get(url, timeout=30, allow_redirects=True)
        except Exception as e:
            failed.append((url, f"req-err: {e}"))
            continue
        if r.status_code != 200:
            failed.append((url, f"http {r.status_code}"))
            continue
        ct = r.headers.get("Content-Type", "")
        body = r.content
        kind = None
        local: Path | None = None
        if _is_pdf(body, ct):
            kind = "pdf"
            h = hashlib.sha1(body).hexdigest()[:12]
            base = re.sub(r"[^a-zA-Z0-9._-]+", "_",
                          Path(urlparse(url).path).name or f"file_{h}.pdf")
            if not base.lower().endswith(".pdf"):
                base += ".pdf"
            local = pdfs_dir / f"{h}_{base}"
            if not local.exists():
                local.write_bytes(body)
        elif _is_html(ct):
            kind = "html"
            local = out_dir / _safe_name(url)
            if not local.exists() or force:
                local.write_bytes(body)
            # Extract links + queue same-host ones
            for m in re.finditer(rb'href=["\']([^"\']+)', body, re.I):
                try:
                    link = m.group(1).decode("utf-8", errors="ignore")
                except Exception:
                    continue
                nxt = _norm(link, url)
                if not nxt:
                    continue
                if urlparse(nxt).netloc not in hosts:
                    continue
                if nxt not in seen:
                    queue.append(nxt)
        else:
            kind = "other"
            # Save anyway under _files/ for completeness if small enough
            if len(body) < 10_000_000:  # 10MB cap
                base = re.sub(r"[^a-zA-Z0-9._-]+", "_",
                              Path(urlparse(url).path).name or "blob")[:120]
                if not base:
                    base = f"blob_{hashlib.sha1(body).hexdigest()[:8]}"
                local = pdfs_dir / base
                if not local.exists():
                    local.write_bytes(body)
        manifest["fetched"][url] = {
            "local_path": str(local.relative_to(course_dir)) if local else None,
            "content_type": ct,
            "size": len(body),
            "kind": kind,
            "ts": int(time.time()),
        }
        fetched_now += 1
        if delay_sec > 0:
            time.sleep(delay_sec)

    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {
        "start_url": start_url,
        "host": base_host,
        "fetched_now": fetched_now,
        "total_in_manifest": len(manifest["fetched"]),
        "failed": failed[:30],
        "local_dir": str(out_dir.relative_to(course_dir)),
    }


def classify_link_action(url: str, frequency: int = 1, total_unique: int = 1) -> tuple[str, str]:
    """Decide what to do with a discovered URL.

    Returns (action, reason). Actions:
      - 'full_scrape'   : recursive same-host crawl (likely course homepage)
      - 'single_page'   : fetch this exact URL only (one-off resource)
      - 'download_file' : direct file download (PDF, slides, archive)
      - 'google_doc'    : pass to google_scrape (Drive/Docs/Slides)
      - 'skip'          : platform/junk URL, ignore
    """
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    full = url.lower()

    skip_hosts = {
        "canvas.nd.edu", "www.canvas.nd.edu", "instructure.com",
        "www.gradescope.com", "gradescope.com", "lti.int.turnitin.com",
        "lti-gradescope.int.turnitin.com", "turnitin.com",
        "zoom.us", "us02web.zoom.us", "us04web.zoom.us",
        "youtube.com", "www.youtube.com", "youtu.be",
        "doi.org", "www.amazon.com", "amazon.com",
        "cengage.com", "webex.com", "microsoft.com", "office.com",
        "w3.org", "www.w3.org", "schema.org",
        "facebook.com", "twitter.com", "x.com", "linkedin.com",
        "support.google.com", "policies.google.com", "myaccount.google.com",
        "fonts.googleapis.com", "fonts.gstatic.com",
    }
    if host in skip_hosts:
        return ("skip", f"platform/junk host {host}")

    # Google Drive family
    if "drive.google.com" in host or "docs.google.com" in host:
        return ("google_doc", f"Google Drive/Docs ({host})")

    # Direct file URLs
    file_exts = (".pdf", ".pptx", ".ppt", ".docx", ".xlsx", ".zip",
                 ".tar.gz", ".tgz", ".ipynb", ".tex")
    if any(path.endswith(e) for e in file_exts):
        return ("download_file", f"file extension {Path(path).suffix}")

    # GitHub/GitLab repos: full scrape unhelpful (HTML noise); skip & note
    if "github.com" in host or "gitlab.com" in host:
        return ("single_page", "git repo — page captures README")

    # Heuristics for "main course site":
    course_keywords = (
        "paradigms", "syllabus", "course", "class",
        "lecture", "homework", "assignment", "schedule",
        "30332", "30440", "20211", "30246", "20110", "30321",
        "/teaching/", "/~",  # personal-site indicators
    )
    is_edu = host.endswith(".edu") or host.endswith(".org")
    has_course_kw = any(kw in full for kw in course_keywords)
    short_path = path.strip("/").count("/") <= 2

    # Strong indicators of a course/teaching homepage
    is_teaching_path = "/teaching/" in path or "/courses/" in path or "/~" in path
    is_github_io = host.endswith(".github.io")
    if is_teaching_path:
        return ("full_scrape", f"explicit teaching path on {host}")
    if is_github_io and any(kw in path for kw in ("paradigms", "course", "class",
                                                   "30332", "30440", "20211")):
        return ("full_scrape", f"github.io course site")
    if is_github_io:
        return ("full_scrape", f"github.io personal site (likely course)")
    if (is_edu or has_course_kw) and short_path and frequency >= 2:
        return ("full_scrape", f"edu/course-keyword + short path + freq={frequency}")
    if is_edu and short_path:
        return ("single_page", "edu site, short path, low frequency — fetch homepage only")
    if is_edu:
        return ("single_page", "edu site — single resource page")

    # Anything else: just save the page
    return ("single_page", "default — fetch single page")


def _classify_external_url(url: str) -> str:
    """Tag URL by source kind: drive, slides, docs, sheets, github, web, file, other."""
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if "drive.google.com" in host:
        if "/folders/" in path:
            return "drive_folder"
        return "drive_file"
    if "docs.google.com" in host:
        if "/presentation/" in path:
            return "gslides"
        if "/document/" in path:
            return "gdoc"
        if "/spreadsheets/" in path:
            return "gsheet"
        if "/forms/" in path:
            return "gform"
        return "gdocs_other"
    if "github.com" in host or "gitlab.com" in host:
        return "git"
    if path.endswith((".pdf",)):
        return "pdf"
    return "web"


def score_main_course_site(urls_with_freq: dict[str, int]) -> list[tuple[str, int, str]]:
    """Score candidate course-homepage URLs.

    Heuristic: a course homepage is referenced many times, lives on .edu/.org,
    has a short path (looks like a hub), and is non-Google (we treat Drive
    links as material, not the homepage).
    """
    scored: list[tuple[str, int, str]] = []
    for url, freq in urls_with_freq.items():
        kind = _classify_external_url(url)
        if kind in ("drive_folder", "drive_file", "gslides", "gdoc",
                    "gsheet", "gform", "gdocs_other"):
            continue  # these are materials, not homepages
        host = urlparse(url).netloc.lower()
        path = urlparse(url).path
        score = freq * 10
        # Edu/personal-site bonus
        if host.endswith(".edu") or "/~" in path or "/teaching/" in path:
            score += 50
        # Course-name keyword bonus
        if any(kw in (host + path).lower() for kw in
               ("paradigms", "programming", "course", "syllabus", "class",
                "lecture", "30332", "20211", "30440", "30246")):
            score += 30
        # Penalize deep / specific URLs
        depth = path.strip("/").count("/")
        score -= depth * 5
        # Penalize anchor-only or query-heavy
        if "?" in url or "#" in url:
            score -= 20
        scored.append((url, score, kind))
    scored.sort(key=lambda r: -r[1])
    return scored


def detect_main_course_site(course_dir: Path) -> tuple[str, str] | None:
    """Return (best_url, kind) most likely to be the main course site, or None.

    Counts URL occurrences across all Canvas content, then scores them.
    """
    urls = detect_course_urls_from_canvas(course_dir)
    if not urls:
        return None
    # Count frequency by re-scanning (detect_course_urls returns unique already;
    # do a second pass for counts).
    pat = re.compile(rb'https?://[a-zA-Z0-9._-]+(?:\.[a-zA-Z]{2,})+(?:/[^"\'<> )]*)?', re.I)
    counts: dict[str, int] = {}
    candidates: list[Path] = []
    for n in ("course.json", "syllabus.json", "announcements.json",
              "modules.json", "assignments.json", "discussions.json",
              "front_page.json", "front_page.html"):
        p = course_dir / n
        if p.exists():
            candidates.append(p)
    candidates.extend(course_dir.glob("modules/**/pages/*.html"))
    candidates.extend(course_dir.glob("modules/**/pages/*.json"))
    candidates.extend(course_dir.glob("assignments/*.json"))
    candidates.extend(course_dir.glob("assignments/*.html"))
    candidates.extend(course_dir.glob("announcements/*.html"))
    candidates.extend(course_dir.glob("announcements/*.json"))
    candidates.extend(course_dir.glob("discussions/*.html"))
    candidates.extend(course_dir.glob("discussions/*.json"))
    for f in candidates:
        try:
            data = f.read_bytes()
        except Exception:
            continue
        for m in pat.finditer(data):
            url = m.group(0).decode("utf-8", errors="ignore").rstrip(".,;'\")")
            if url in urls:
                counts[url] = counts.get(url, 0) + 1
    scored = score_main_course_site(counts)
    if not scored:
        return None
    best_url, best_score, kind = scored[0]
    if best_score < 30:  # threshold — need decent confidence
        return None
    return (best_url, kind)


def detect_course_urls_from_canvas(course_dir: Path) -> list[str]:
    """Scan Canvas-downloaded JSON / HTML for external course-site links.

    Looks at: course.json (syllabus_body), syllabus.json, modules.json,
    announcements.json, page HTMLs. Returns sorted unique list.
    """
    urls: set[str] = set()
    skip_hosts = {
        "canvas.nd.edu", "www.canvas.nd.edu", "instructure.com",
        "www.gradescope.com", "gradescope.com",
        "zoom.us", "youtube.com", "www.youtube.com",
        "google.com", "www.google.com", "drive.google.com",
        "doi.org", "www.amazon.com", "amazon.com",
        "cengage.com", "webex.com", "microsoft.com",
        "w3.org", "www.w3.org",
    }
    candidates: list[Path] = []
    for n in ("course.json", "syllabus.json", "announcements.json",
              "modules.json", "assignments.json", "discussions.json",
              "front_page.json", "front_page.html"):
        p = course_dir / n
        if p.exists():
            candidates.append(p)
    candidates.extend(course_dir.glob("modules/**/pages/*.html"))
    candidates.extend(course_dir.glob("modules/**/pages/*.json"))
    candidates.extend(course_dir.glob("assignments/*.json"))
    candidates.extend(course_dir.glob("assignments/*.html"))
    candidates.extend(course_dir.glob("announcements/*.html"))
    candidates.extend(course_dir.glob("announcements/*.json"))
    candidates.extend(course_dir.glob("discussions/*.html"))
    candidates.extend(course_dir.glob("discussions/*.json"))
    pat = re.compile(rb'https?://[a-zA-Z0-9._-]+(?:\.[a-zA-Z]{2,})+(?:/[^"\'<> )]*)?', re.I)
    for f in candidates:
        try:
            data = f.read_bytes()
        except Exception:
            continue
        for m in pat.finditer(data):
            url = m.group(0).decode("utf-8", errors="ignore").rstrip(".,;'\")\\")
            host = urlparse(url).netloc.lower()
            if host in skip_hosts:
                continue
            # Skip very long query strings (signed S3 URLs etc.)
            if len(url) > 250:
                continue
            urls.add(url)
    return sorted(urls)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--url", help="Starting URL to crawl. If omitted, --detect lists candidates.")
    ap.add_argument("--detect", action="store_true",
                    help="List candidate external URLs from Canvas content.")
    ap.add_argument("--auto", action="store_true",
                    help="Auto-detect main course site and scrape it.")
    ap.add_argument("--auto-all", action="store_true",
                    help="Walk EVERY link from Canvas, classify each, run "
                         "appropriate action (full-scrape/single-page/download).")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--allow-host", action="append", default=[],
                    help="Additional same-domain host to allow (repeatable).")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        print(f"Not a directory: {cdir}")
        return 1

    if args.detect:
        urls = detect_course_urls_from_canvas(cdir)
        print(f"Found {len(urls)} candidate external URLs:")
        for u in urls:
            print(" ", u)
        best = detect_main_course_site(cdir)
        if best:
            print(f"\nBest-guess main course site: {best[0]}  (kind={best[1]})")
        else:
            print("\nNo high-confidence main course site detected.")
        return 0

    if args.auto_all or args.auto:
        # Per-URL classified scrape: walk every external link from Canvas,
        # decide action per link, execute it.
        all_urls = detect_course_urls_from_canvas(cdir)
        # Frequency count for scoring full_scrape decisions
        pat = re.compile(rb'https?://[a-zA-Z0-9._-]+(?:\.[a-zA-Z]{2,})+(?:/[^"\'<> )]*)?', re.I)
        counts: dict[str, int] = {}
        for f_p in (
            list(cdir.glob("*.json")) +
            list(cdir.glob("modules/**/*.html")) +
            list(cdir.glob("modules/**/*.json")) +
            list(cdir.glob("assignments/*.html")) +
            list(cdir.glob("assignments/*.json")) +
            list(cdir.glob("announcements/*.html")) +
            list(cdir.glob("announcements/*.json"))
        ):
            try:
                data = f_p.read_bytes()
            except Exception:
                continue
            for m in pat.finditer(data):
                u = m.group(0).decode("utf-8", errors="ignore").rstrip(".,;'\")")
                if u in all_urls:
                    counts[u] = counts.get(u, 0) + 1
        report = {"discovered": len(all_urls), "actions": {}, "details": []}
        sess = requests.Session()
        sess.headers["User-Agent"] = UA
        for url in all_urls:
            freq = counts.get(url, 1)
            action, reason = classify_link_action(url, freq, len(all_urls))
            report["actions"].setdefault(action, 0)
            report["actions"][action] += 1
            print(f"[{action:14}] {url[:90]}  ({reason})")
            try:
                if action == "full_scrape":
                    crawl(url, cdir, max_pages=args.max_pages,
                          allow_hosts=args.allow_host, force=args.force,
                          delay_sec=args.delay)
                elif action == "single_page":
                    # Fetch one page only — reuse crawl with max_pages=1
                    crawl(url, cdir, max_pages=1,
                          allow_hosts=args.allow_host, force=args.force,
                          delay_sec=args.delay)
                elif action == "download_file":
                    r = sess.get(url, timeout=120, allow_redirects=True)
                    if r.status_code == 200 and len(r.content) > 100:
                        out_dir = cdir / "_external" / urlparse(url).netloc / "_files"
                        out_dir.mkdir(parents=True, exist_ok=True)
                        h = hashlib.sha1(r.content).hexdigest()[:10]
                        name = re.sub(r"[^a-zA-Z0-9._-]+", "_",
                                      Path(urlparse(url).path).name or f"file_{h}")
                        (out_dir / f"{h}_{name}").write_bytes(r.content)
                # google_doc handled by google_scrape.py separately
            except Exception as e:
                report["details"].append({"url": url, "error": str(e)[:200]})
        # SECOND PASS: scan freshly-scraped HTML for Drive/Docs links + dispatch them
        ext_dir = cdir / "_external"
        drive_urls: set[str] = set()
        if ext_dir.exists():
            for html in ext_dir.rglob("*.html"):
                try:
                    text = html.read_text(errors="ignore")
                except Exception:
                    continue
                for m in re.finditer(r'https?://(?:docs|drive)\.google\.com/[^\s"\'<>)]+', text):
                    u = m.group(0).rstrip(".,;'\")\\")
                    drive_urls.add(u)
        if drive_urls:
            print(f"\nFound {len(drive_urls)} Google Drive/Docs links inside scraped pages.")
            try:
                sys.path.insert(0, str(Path(__file__).parent))
                from google_scrape import fetch_many
                gs_summary = fetch_many(sorted(drive_urls), cdir)
                print(f"Google scrape result: {gs_summary}")
                report["actions"]["google_doc"] = gs_summary.get("ok", 0)
            except RuntimeError as e:
                print(f"Skipping Google Drive: {e}")
            except Exception as e:
                print(f"Google scrape failed: {e}")
        print()
        print(json.dumps(report["actions"], indent=2))
        return 0

    if not args.url:
        print("--url required (or use --detect / --auto)")
        return 1

    print(f"Crawling {args.url} → {cdir}/_external/")
    summary = crawl(
        args.url, cdir,
        max_pages=args.max_pages,
        allow_hosts=args.allow_host,
        force=args.force,
        delay_sec=args.delay,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
