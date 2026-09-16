# nd-canvas-study

End-to-end exam-prep tool for Notre Dame Canvas courses. Give it a class name and it will:

1. Log into Canvas (Firefox SSO + Duo, captured automatically by Playwright).
2. Download every PDF, page, assignment, and module from the course.
3. Extract text and bundle it into LLM-injectable categorized files.
4. Cross-reference exam/practice problems against homework/in-class material to find under-prepared topics.
5. Split the exam PDFs into individual problems, classify each by topic, and link back to the prep files that cover it.
6. Index every chunk into a local Chroma vector DB so you can semantic-search the corpus.
7. Optionally serve everything in a local Django + Bootstrap UI.

## One-command flow

```bash
python prep.py "statistics"
```

Output (paths, top priorities, study plan, vector index ready):

```
$ python prep.py statistics
✓ Canvas session OK
Matched 'statistics' → [128781] Statistics (score 1.00)
... downloads, bundles, gap analysis, problem extraction, vector index ...

Top priorities (exam-heavy vs prep-light):
  anova                           exam= 26  prep= 37  gap=+14.80pp
  descriptive_statistics          exam= 53  prep=288  gap=+7.72pp
  regression_simple               exam= 19  prep= 65  gap=+6.81pp
  ...
```

Useful flags:

```bash
python prep.py --list                         # list all your active courses
python prep.py "statistics" --skip-sync       # don't redownload
python prep.py "statistics" --ask "tukey HSD multiple comparisons"
python prep.py "statistics" --serve           # also launch the Django UI
```

## Setup (one-time)

```bash
./setup.sh
```

Creates the venv, installs Python deps, installs Playwright Firefox, runs Django migrations, and tells you what other system tools are missing (Poppler for PDF rendering, Claude CLI).

## Launch

```bash
./start.sh                       # start UI on http://127.0.0.1:8000 and open browser
./start.sh prep "statistics"     # full pipeline for a class
./start.sh prep "statistics" --ask "tukey HSD"
./start.sh auth                  # re-login when cookies expire
./start.sh stop                  # stop UI
./start.sh status                # is UI running?
./start.sh help                  # show all commands
```

## Pipeline pieces

Each step is a standalone script, all wired together by `prep.py`:

| Script           | Purpose                                                                    |
|------------------|----------------------------------------------------------------------------|
| `auth.py`        | Launches Firefox, waits for ND SSO + Duo, saves cookies to `cookies.json`. |
| `canvas_client.py` | Cookie-auth REST wrapper around the Canvas API.                          |
| `download.py`    | Walks Modules → files/pages/assignments. Works even when `/files` is locked. |
| `email_ingest.py` | Gmail → `_email/`: instructor messages, attachments, and PDF renders of Office attachments. Uses the existing gmail-mcp OAuth tokens, read-only. |
| `office_prep.py` | Renders Canvas-hosted `.docx/.pptx` into PDF (LibreOffice) plus `_office_text/*.txt`, so Office files reach the index. |
| `bundle.py`      | PDF → text, categorized into `lectures / homeworks / hw_keys / in_class / exams / exam_solutions / practice / tables / textbook / other`. |
| `ocr_claude.py`  | Claude-vision transcription of scanned or math-heavy PDFs (`--domain algorithms|statistics|generic`, `--all` for every category). |
| `panopto_lti_scraper.py` | Launches the Canvas Panopto LTI tab in Playwright, lists the folder, pulls each lecture's caption transcript into `_panopto/<date>_<id>.txt`. |
| `analyze.py`     | Topic-frequency gap report: which topics show up more in exams than in HW. |
| `problems.py`    | Splits exam PDFs into individual problems, classifies each by topic, links back to the prep files for review. |
| `vectorize.py`   | Builds a local Chroma vector index using `all-MiniLM-L6-v2` embeddings. Supports semantic queries with optional category filter. |
| `prep.py`        | Orchestrator. Class name → everything above.                              |

## Output layout

```
downloads/<id>_<slug>/
  course.json
  modules.json
  modules/<chapter>/<files>.pdf
  modules/<chapter>/pages/<slug>.html
  bundles/
    lectures.md, homeworks.md, hw_keys.md, in_class.md
    exams.md, exam_solutions.md, practice.md, tables.md, other.md
    MASTER.md, manifest.json
    topic_gap_report.md, topic_gap.json
    STUDY_PLAN.md, problems.json
  _email/                  # Gmail ingest: messages/, attachments/, attachments_text/
  _office_text/            # text pulled out of Canvas .docx/.pptx
  _ocr/                    # Claude/Tesseract page transcriptions, one .txt per PDF
  _panopto/                # <YYYY-MM-DD>_<delivery-id>.txt lecture transcripts
  _textbook/               # scanned textbook chapters, plus index.json describing coverage
  chroma/                  # vector index
```

`STUDY_PLAN.md` is the file you actually want to read before the exam — topics ranked by exam-problem count, each with sample problem stems (with source PDF + page) and the list of HW/in-class files to review.

## Web UI

```bash
python manage.py runserver
# http://127.0.0.1:8000
```

- `/` — auth, downloaded courses, downloadable courses
- `/course/<id>/` — files, bundles, gap report, study plan
  - "Build LLM bundles" / "Run gap analysis" / "Build vector index" buttons
- `/course/<id>/ask/?q=...` — semantic search across the course (with category filter)

## Switching to a different class

Pure CLI flow is parameterized by class name (or course id):

```bash
python prep.py "computer architecture"
python prep.py "computer architecture" --ask "pipeline hazards"

# A course that lives in email and lecture recordings rather than on Canvas:
python prep.py "algorithms" --with-email --email-since 2026/08/01 \
    --with-panopto --panopto-tool-id 5829 --claude-ocr --domain algorithms
```

The topic dictionary in `analyze.py` is statistics-specific. For a non-stats course, edit `TOPICS` in `analyze.py` (each entry is a topic name → list of regex aliases). The rest of the pipeline is subject-agnostic.

## Retrieval

`vectorize.search()` is hybrid, and `vectorize.multi_search()` wraps it for questions that ask more than one thing:

- **Dense** — Chroma / `all-MiniLM-L6-v2` over every chunk.
- **Keyword** — BM25 over the same chunks, persisted to `chroma/bm25.json` at index time. Without it, dense similarity ranks rambling lecture prose above a homework PDF whose OCR reads "Assignment 2 … Due Date: Sept. 20".
- **Round-robin merge, not score fusion** — slots alternate between the two lists, so a keyword winner is guaranteed a place instead of being out-voted.
- **Per-source cap** (default 2) — a 75-minute lecture is ~40 chunks and an email is 1; without the cap one recording fills every slot.
- **Clause splitting** — "When is HW2 due, what does it cover, and what did Chen say about substitution?" is retrieved as three queries and interleaved.

Courses indexed before BM25 existed keep working; `bm25_search` returns nothing until the next `--rebuild` and retrieval falls back to dense-only.

## Notes on the vector index

- Embedding model: `sentence-transformers/all-MiniLM-L6-v2` (downloads ~80 MB on first run, runs locally, no API key).
- Chunking: 1500-char chunks with 200-char overlap, one chunk per page.
- Each chunk's metadata stores `source` (relative path), `page`, `chunk`, and `category`, so every search hit cites the exact file. Email chunks also carry `subject`, `sender`, and `sent`; Panopto chunks carry `lecture_date`.
- Categories in the index: `lectures, homeworks, hw_keys, exams, exam_solutions, practice, tables, textbook, other, ocr, ocr_slide, ocr_exam, transcript, panopto_transcript, email, email_attachment, office_doc, topic_summary, gdoc, external_web`.
- Identical chunks are indexed once. The same syllabus typically arrives from Canvas Files and from two separate emails; without the hash check those duplicates crowd out distinct material in the top-k.
- Panopto captions interleave a bare timestamp after every spoken line. `normalize_panopto()` strips them, keeping one `[m:ss]` marker every 15 lines so a hit can still be seeked to in the recording.

## Caveats

- Canvas session cookies expire when you log out. Re-run `python prep.py <class>` and it'll auto-prompt for re-login.
- Some courses (Statistics is one) lock down `/api/v1/courses/<id>/files`. The Modules walker handles this by fetching files individually via `/api/v1/files/<id>`.
- Quizzes often return 403 for question listing — that's a Canvas restriction, not a bug.
- PDF text extraction quality depends on the source PDF; image-only scans yield empty text. Run `ocr_claude.py` on those; a 30-page scanned CLRS chapter transcribes in about 90 seconds at 8 workers.
- ND's Panopto rejects cookie-only transcript endpoints (`panopto.py` returns ErrorCode 6). The working path is `panopto_lti_scraper.py`, which launches the Canvas LTI tab so Panopto sees a course-scoped session. If the Canvas tabs API does not expose the tool, pass `--tool-id` (the number in `/courses/<id>/external_tools/<tool-id>`).
- `email_ingest.py` unions its `--query` arguments. Broad course-number queries pull in personal mail that merely mentions the course; negate the noisy senders (`-from:me -subject:rundown`) rather than indexing it.
- `cookies.json`, `downloads/`, `chroma/`, `db.sqlite3`, `.env` are all gitignored. Verify before pushing.
