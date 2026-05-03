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

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install firefox
python manage.py migrate
```

## Pipeline pieces

Each step is a standalone script, all wired together by `prep.py`:

| Script           | Purpose                                                                    |
|------------------|----------------------------------------------------------------------------|
| `auth.py`        | Launches Firefox, waits for ND SSO + Duo, saves cookies to `cookies.json`. |
| `canvas_client.py` | Cookie-auth REST wrapper around the Canvas API.                          |
| `download.py`    | Walks Modules → files/pages/assignments. Works even when `/files` is locked. |
| `bundle.py`      | PDF → text, categorized into `lectures / homeworks / hw_keys / in_class / exams / exam_solutions / practice / tables / other`. |
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
```

The topic dictionary in `analyze.py` is statistics-specific. For a non-stats course, edit `TOPICS` in `analyze.py` (each entry is a topic name → list of regex aliases). The rest of the pipeline is subject-agnostic.

## Notes on the vector index

- Embedding model: `sentence-transformers/all-MiniLM-L6-v2` (downloads ~80 MB on first run, runs locally, no API key).
- Chunking: 1500-char chunks with 200-char overlap, one chunk per page.
- Each chunk's metadata stores `source` (relative path), `page`, `chunk`, and `category`, so every search hit cites the exact file.

## Caveats

- Canvas session cookies expire when you log out. Re-run `python prep.py <class>` and it'll auto-prompt for re-login.
- Some courses (Statistics is one) lock down `/api/v1/courses/<id>/files`. The Modules walker handles this by fetching files individually via `/api/v1/files/<id>`.
- Quizzes often return 403 for question listing — that's a Canvas restriction, not a bug.
- PDF text extraction quality depends on the source PDF; image-only scans yield empty text.
- `cookies.json`, `downloads/`, `chroma/`, `db.sqlite3`, `.env` are all gitignored. Verify before pushing.
