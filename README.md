# nd-canvas-study

Download Notre Dame Canvas course material via the REST API, bundle it into LLM-friendly text packs, and cross-reference exam vs homework topic frequency to find under-prepared areas. Includes a small Django + Bootstrap UI.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install firefox
python manage.py migrate
```

## CLI usage

### 1. Log in (auto cookie capture)

Launches a real Firefox window. Complete ND SSO + Duo. Cookies are saved to `cookies.json`.

```bash
python auth.py            # log in
python auth.py --check    # verify session is alive
```

### 2. List + download a course

```bash
python download.py                                 # list all active courses
python download.py --course 128781 --name statistics
```

Output tree:

```
downloads/<id>_<slug>/
  course.json
  modules.json
  modules/<module-name>/
    <pdf files...>
    pages/<slug>.html
    assignments/<id>_<slug>.html
    quizzes/<id>.json
  assignments.json
  discussions.json
```

The downloader uses the Modules walker as the primary spine, so it works even when the course's bulk Files endpoint is locked down (Statistics is one such course).

### 3. Build LLM-injectable bundles

```bash
python bundle.py --course-dir downloads/128781_statistics
```

Produces `downloads/<course>/bundles/`:

- `lectures.md`, `homeworks.md`, `hw_keys.md`, `in_class.md`, `exams.md`, `exam_solutions.md`, `practice.md`, `tables.md`, `other.md`
- Each bundle has `## SOURCE: <relative path>` separators so an LLM can cite which file a passage came from.
- `MASTER.md` and `manifest.json` index everything.

### 4. Topic-gap analysis

```bash
python analyze.py --course-dir downloads/128781_statistics
```

Counts hits for ~25 statistics topics (ANOVA, regression, hypothesis testing, distributions, …) across the exam corpus and the prep corpus. Reports the topics that take a larger share of exam content than of HW/in-class content — i.e., things you've practiced the least relative to how often they're tested.

Output: `bundles/topic_gap_report.md` and `bundles/topic_gap.json`.

### 5. Web UI

```bash
python manage.py runserver
# http://127.0.0.1:8000
```

- `/` — auth state, downloaded courses, remote courses available to download
- `/course/<id>/` — files, bundles, gap report; buttons to re-sync, rebuild bundles, re-run analysis

## Switching to a different class

Everything is parameterized by Canvas course ID. To prep for a different course, just:

```bash
python download.py --course <other_id> --name <slug>
python bundle.py --course-dir downloads/<other_id>_<slug>
python analyze.py --course-dir downloads/<other_id>_<slug>
```

The topic dictionary in `analyze.py` is statistics-specific; replace `TOPICS` with the relevant keyword aliases for another subject.

## Files

- `auth.py` — Playwright Firefox SSO capture
- `canvas_client.py` — Canvas REST wrapper (cookie auth)
- `download.py` — Modules-walker downloader
- `bundle.py` — PDF → categorized text bundles
- `analyze.py` — exam vs prep topic-frequency gap
- `web/`, `ui/` — Django + Bootstrap frontend
- `cookies.json`, `downloads/` — local data, gitignored

## Notes

- Cookie session expires when Canvas logs you out. Re-run `python auth.py`.
- Quizzes often return 403 even with a valid session — Canvas restricts question listing.
- PDF text extraction quality depends on the source PDF; image-only scans yield empty text.
