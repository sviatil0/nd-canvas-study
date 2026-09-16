"""Index a course's PDFs into a local Chroma vector DB for semantic search.

    python vectorize.py --course-dir downloads/128781_statistics    # build index
    python vectorize.py --course-dir <dir> --query "anova multiple comparison"
    python vectorize.py --course-dir <dir> --query "..." --k 8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from pypdf import PdfReader

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += size - overlap
    return chunks


def iter_pdf_chunks(pdf: Path):
    try:
        reader = PdfReader(str(pdf))
    except Exception as e:
        print(f"  skip {pdf.name}: {e}")
        return
    for page_idx, page in enumerate(reader.pages):
        try:
            txt = page.extract_text() or ""
        except Exception:
            continue
        for ci, chunk in enumerate(chunk_text(txt)):
            yield {
                "text": chunk,
                "page": page_idx + 1,
                "chunk_idx": ci,
            }


PAGE_MARK_RE = re.compile(r"^--- page (\d+) ---$", re.M)


def split_ocr_pages(text: str) -> list[tuple[int, str]]:
    """[(page_number, page_text)] from an ocr_claude/ocr stitched transcript."""
    marks = list(PAGE_MARK_RE.finditer(text))
    if not marks:
        return [(1, text)]
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append((int(m.group(1)), body))
    return out


TS_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def normalize_panopto(text: str) -> str:
    """Panopto caption dumps put a bare timestamp after every spoken line.

    Embedding 1,500 characters of "0:02\n0:11\n0:15" wastes most of a chunk, so
    drop the timestamps but keep one marker every 15 lines, which is enough to
    seek back to the moment in the recording.
    """
    out: list[str] = []
    pending: str | None = None
    countdown = 0
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if TS_RE.match(ln):
            pending = ln
            continue
        if countdown <= 0 and pending:
            out.append(f"[{pending}] {ln}")
            countdown = 15
        else:
            out.append(ln)
            countdown -= 1
    return "\n".join(out)


def get_collection(course_dir: Path, reset: bool = False):
    import chromadb
    from chromadb.utils import embedding_functions

    db_path = course_dir / "chroma"
    db_path.mkdir(exist_ok=True)
    client = chromadb.PersistentClient(path=str(db_path))
    embed = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    if reset:
        # A rebuild has to drop the collection, not filter-delete it. Chunk ids
        # are path-derived, so renamed or deleted sources would otherwise linger
        # in the index forever and keep answering queries.
        try:
            client.delete_collection(name="course_pdfs")
        except Exception:
            pass
    return client.get_or_create_collection(name="course_pdfs", embedding_function=embed)


def categorize(rel_path: str) -> str:
    manifest = json.loads((Path("dummy")).read_text()) if False else None
    return ""


def iter_text_chunks(path: Path):
    try:
        text = path.read_text(errors="ignore")
    except Exception as e:
        print(f"  skip {path.name}: {e}")
        return
    for ci, chunk in enumerate(chunk_text(text)):
        yield {"text": chunk, "page": 1, "chunk_idx": ci}


def build_index(course_dir: Path) -> None:
    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first to produce manifest.")
    manifest = {r["path"]: r["category"] for r in json.loads(manifest_file.read_text())}

    coll = get_collection(course_dir, reset=True)

    docs, ids, metas = [], [], []
    # The same syllabus can arrive from Canvas Files and from two emails. Exact
    # duplicate chunks crowd out distinct material in the top-k, so keep first.
    seen_hashes: set[str] = set()

    def add(text: str, cid: str, meta: dict) -> bool:
        h = hashlib.sha1(" ".join(text.split()).encode()).hexdigest()
        if h in seen_hashes:
            return False
        seen_hashes.add(h)
        docs.append(text)
        ids.append(cid)
        metas.append(meta)
        return True

    for pdf in sorted(course_dir.rglob("*.pdf")):
        rel = str(pdf.relative_to(course_dir))
        category = manifest.get(rel, "other")
        for c in iter_pdf_chunks(pdf):
            cid = f"{rel}#p{c['page']}-{c['chunk_idx']}"
            add(c["text"], cid, {
                "source": rel,
                "page": c["page"],
                "chunk": c["chunk_idx"],
                "category": category,
            })
        print(f"  indexed {rel}")

    transcripts_dir = course_dir / "transcripts"
    if transcripts_dir.exists():
        for txt in sorted(transcripts_dir.glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            for c in iter_text_chunks(txt):
                cid = f"{rel}#c{c['chunk_idx']}"
                add(c["text"], cid, {
                    "source": rel,
                    "page": 1,
                    "chunk": c["chunk_idx"],
                    "category": "transcript",
                })
            print(f"  indexed transcript {rel}")

    # OCR text files (slide PDFs, exam PDFs, scanned textbook chapters)
    ocr_dir = course_dir / "_ocr"
    if ocr_dir.exists():
        for txt in sorted(ocr_dir.glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            content = txt.read_text(errors="ignore")
            if len(content) < 200:
                continue
            # Source path heuristic for category
            cat = "ocr_slide" if "presentation" in rel else (
                "ocr_exam" if "/exams/" in rel else "ocr")
            # ocr_claude.py stitches pages with "--- page N ---" separators.
            # Splitting on them keeps the page number in the citation, which is
            # what makes a 30-page textbook chapter usable as a source.
            # "_ocr/_email__attachments__2026-09-09_hw2.pdf.txt" -> the original
            # path. The transcribed text calls it "Assignment 2", so without this
            # header a keyword search for "hw2" never reaches its due date.
            origin = txt.stem.replace("__", "/")
            for page_no, page_text in split_ocr_pages(content):
                header = f"[source: {origin} page {page_no}]\n"
                for ci, chunk in enumerate(chunk_text(page_text)):
                    add(header + chunk, f"{rel}#p{page_no}-{ci}",
                        {"source": rel, "page": page_no, "chunk": ci,
                         "category": cat, "origin": origin})
            print(f"  indexed ocr {rel[:60]}")

    # Panopto lecture transcripts
    panopto_dir = course_dir / "_panopto"
    if panopto_dir.exists():
        sessions = {}
        summary = panopto_dir / "summary.json"
        if summary.exists():
            try:
                sessions = (json.loads(summary.read_text()) or {}).get("sessions", {}) or {}
            except Exception:
                sessions = {}
        for txt in sorted(panopto_dir.glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            content = normalize_panopto(txt.read_text(errors="ignore"))
            if len(content) < 500:
                continue
            # Filenames are <YYYY-MM-DD>_<first 8 of delivery id>.txt
            stem = txt.stem
            date = stem[:10] if re.match(r"\d{4}-\d{2}-\d{2}", stem) else ""
            short = stem.split("_")[-1].lower()
            meta_s = next((v for k, v in sessions.items() if k.startswith(short)), {})
            for ci, chunk in enumerate(chunk_text(content)):
                cid_ = f"{rel}#c{ci}"
                add(chunk, cid_, {"source": rel, "page": 1, "chunk": ci,
                                  "category": "panopto_transcript",
                                  "lecture_date": date or meta_s.get("date", ""),
                                  "lecture": meta_s.get("name", "")})
            print(f"  indexed panopto {rel[:60]}")

    # Text pulled out of Canvas-hosted .docx/.pptx by office_prep.py
    office_dir = course_dir / "_office_text"
    if office_dir.exists():
        for txt in sorted(office_dir.glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            content = txt.read_text(errors="ignore")
            if len(content) < 80:
                continue
            for ci, chunk in enumerate(chunk_text(content)):
                add(chunk, f"{rel}#c{ci}", {"source": rel, "page": 1,
                                            "chunk": ci, "category": "office_doc"})
            print(f"  indexed office doc {txt.stem[:50]}")

    # Instructor email: bodies plus text pulled from non-PDF attachments.
    # For a course like CSE 40113, where Canvas holds three files and every
    # deadline, reading and policy change arrives by mail, this is the corpus.
    email_root = course_dir / "_email"
    if email_root.exists():
        email_meta = {}
        man = email_root / "manifest.json"
        if man.exists():
            try:
                for rec in json.loads(man.read_text()):
                    email_meta[Path(rec["body_path"]).name] = rec
            except Exception:
                pass
        for txt in sorted((email_root / "messages").glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            content = txt.read_text(errors="ignore")
            if len(content) < 80:
                continue
            rec = email_meta.get(txt.name, {})
            for ci, chunk in enumerate(chunk_text(content)):
                add(chunk, f"{rel}#c{ci}", {
                    "source": rel, "page": 1, "chunk": ci, "category": "email",
                    "subject": rec.get("subject", txt.stem),
                    "sender": rec.get("from", ""),
                    "sent": rec.get("date", ""),
                })
            print(f"  indexed email {txt.stem[:60]}")
        for txt in sorted((email_root / "attachments_text").glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            content = txt.read_text(errors="ignore")
            if len(content) < 80:
                continue
            for ci, chunk in enumerate(chunk_text(content)):
                add(chunk, f"{rel}#c{ci}", {"source": rel, "page": 1, "chunk": ci,
                              "category": "email_attachment"})
            print(f"  indexed email attachment text {txt.stem[:50]}")

    # Generated topic summaries (deep AI-generated study guides)
    summaries_dir = course_dir / "bundles" / "topics"
    if summaries_dir.exists():
        for md in sorted(summaries_dir.glob("*.md")):
            rel = str(md.relative_to(course_dir))
            content = md.read_text(errors="ignore")
            if len(content) < 200:
                continue
            for ci, chunk in enumerate(chunk_text(content)):
                cid_ = f"{rel}#c{ci}"
                add(chunk, cid_, {"source": rel, "page": 1, "chunk": ci,
                                  "category": "topic_summary"})
            print(f"  indexed summary {md.stem}")

    # GDoc / activity texts under _external/google_drive_remote/document
    gdoc_dir = course_dir / "_external" / "google_drive_remote" / "document"
    if gdoc_dir.exists():
        for txt in sorted(gdoc_dir.glob("*.txt")):
            rel = str(txt.relative_to(course_dir))
            content = txt.read_text(errors="ignore")
            if len(content) < 200:
                continue
            for ci, chunk in enumerate(chunk_text(content)):
                cid_ = f"{rel}#c{ci}"
                add(chunk, cid_, {"source": rel, "page": 1, "chunk": ci,
                                  "category": "gdoc"})
            print(f"  indexed gdoc {txt.stem[:14]}")

    # External course-website HTML pages (scraped via external_scrape.py)
    ext_dir = course_dir / "_external"
    if ext_dir.exists():
        try:
            from bs4 import BeautifulSoup
            html_strip = True
        except ImportError:
            html_strip = False
        for html_file in sorted(ext_dir.rglob("*.html")):
            rel = str(html_file.relative_to(course_dir))
            try:
                raw = html_file.read_text(errors="ignore")
            except Exception:
                continue
            if html_strip:
                soup = BeautifulSoup(raw, "html.parser")
                for s in soup(["script", "style", "nav", "header", "footer"]):
                    s.decompose()
                text = soup.get_text("\n", strip=True)
            else:
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 200:
                continue
            for ci, chunk in enumerate(chunk_text(text)):
                cid = f"{rel}#c{ci}"
                add(chunk, cid, {
                    "source": rel,
                    "page": 1,
                    "chunk": ci,
                    "category": "external_web",
                })
            print(f"  indexed external HTML {rel}")

    BATCH = 200
    for i in range(0, len(docs), BATCH):
        coll.upsert(ids=ids[i : i + BATCH], documents=docs[i : i + BATCH], metadatas=metas[i : i + BATCH])
    build_bm25(course_dir, docs, ids)
    print(f"\nIndexed {len(docs)} chunks into {course_dir / 'chroma'}")




# ---------------------------------------------------------------- BM25 side
# Dense embeddings alone rank a 75-minute lecture's rambling above a four-line
# email that literally says "reading assignment". BM25 catches the literal
# match; fusing the two rankings is what makes both kinds of question work.

TOKEN_RE = re.compile(r"[a-z0-9]+")
BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def bm25_path(course_dir: Path) -> Path:
    return course_dir / "chroma" / "bm25.json"


def build_bm25(course_dir: Path, docs: list[str], ids: list[str]) -> None:
    postings: dict[str, list[list[int]]] = {}
    lengths: list[int] = []
    for i, doc in enumerate(docs):
        toks = tokenize(doc)
        lengths.append(len(toks))
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t, n in tf.items():
            postings.setdefault(t, []).append([i, n])
    payload = {"ids": ids, "lengths": lengths,
               "avgdl": (sum(lengths) / len(lengths)) if lengths else 0.0,
               "postings": postings}
    bm25_path(course_dir).write_text(json.dumps(payload))
    print(f"  bm25 index: {len(postings)} terms over {len(docs)} chunks")


def bm25_search(course_dir: Path, q: str, n: int = 40) -> list[tuple[str, float]]:
    """[(chunk_id, score)] best first. Empty when no index has been built yet."""
    path = bm25_path(course_dir)
    if not path.exists():
        return []
    try:
        idx = json.loads(path.read_text())
    except Exception:
        return []
    postings, lengths = idx["postings"], idx["lengths"]
    ids, avgdl = idx["ids"], idx["avgdl"] or 1.0
    N = len(ids)
    import math
    scores: dict[int, float] = {}
    for term in set(tokenize(q)):
        plist = postings.get(term)
        if not plist:
            continue
        df = len(plist)
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        for doc_i, tf in plist:
            dl = lengths[doc_i] or 1
            denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
            scores[doc_i] = scores.get(doc_i, 0.0) + idf * (tf * (BM25_K1 + 1)) / denom
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:n]
    return [(ids[i], sc) for i, sc in top]


def search(course_dir: Path, q: str, k: int = 8, where: dict | None = None,
           per_source_cap: int = 2) -> list[dict]:
    """Hybrid top-k: dense vectors and BM25, merged round-robin.

    Two failure modes this guards against, both seen on CSE 40113:
      * A 75-minute lecture is ~40 chunks and a course email is one, so plain
        top-k fills every slot with the same recording. Hence per_source_cap.
      * The dense model ranks rambling lecture prose above a homework PDF whose
        transcription says "Assignment 2 ... Due Date: Sept. 20". Hence BM25,
        and hence taking from both lists in turn rather than by fused score:
        a keyword winner is guaranteed a slot instead of being out-voted.
    """
    coll = get_collection(course_dir)
    pool = coll.query(query_texts=[q], n_results=max(k * 4, k), where=where)
    dense = [
        {"id": cid, "text": doc, "meta": meta, "dist": float(dist)}
        for cid, doc, meta, dist in zip(pool["ids"][0], pool["documents"][0],
                                        pool["metadatas"][0], pool["distances"][0])
    ]
    by_id = {r["id"]: r for r in dense}

    keyword: list[dict] = []
    kw_ids = [rid for rid, _ in bm25_search(course_dir, q, n=max(k * 4, k))]
    missing = [rid for rid in kw_ids if rid not in by_id]
    if missing:
        got = coll.get(ids=missing)
        for rid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"]):
            by_id[rid] = {"id": rid, "text": doc, "meta": meta, "dist": None}
    for rid in kw_ids:
        row = by_id.get(rid)
        if not row:
            continue
        if where and any(row["meta"].get(f) != v for f, v in where.items()):
            continue
        keyword.append(row)

    picked: list[dict] = []
    taken: set[str] = set()
    counts: dict[str, int] = {}

    def take(row: dict) -> bool:
        src = row["meta"].get("source", "?")
        if row["id"] in taken or counts.get(src, 0) >= per_source_cap:
            return False
        taken.add(row["id"])
        counts[src] = counts.get(src, 0) + 1
        picked.append(row)
        return True

    di = ki = 0
    while len(picked) < k and (di < len(dense) or ki < len(keyword)):
        progressed = False
        while di < len(dense):
            row = dense[di]; di += 1
            if take(row):
                progressed = True
                break
        if len(picked) >= k:
            break
        while ki < len(keyword):
            row = keyword[ki]; ki += 1
            if take(row):
                progressed = True
                break
        if not progressed:
            break
    # Still short (few distinct sources): relax the cap over what is left.
    if len(picked) < k:
        for row in dense + keyword:
            if row["id"] not in taken:
                taken.add(row["id"])
                picked.append(row)
                if len(picked) == k:
                    break
    return picked



CLAUSE_SPLIT = re.compile(r"[?;,]|\band\b", re.I)


def split_clauses(q: str) -> list[str]:
    return [c.strip() for c in CLAUSE_SPLIT.split(q) if len(c.split()) >= 3]


def multi_search(course_dir: Path, q: str, k: int = 12, where: dict | None = None,
                 per_source_cap: int = 2) -> list[dict]:
    """Retrieve for the whole question AND for each clause of it.

    "When is HW2 due, what does it cover, and what did Chen say about the
    substitution method?" is three questions. Embedded or BM25-scored as one
    string, the lecture-heavy third clause drowns the first two and the homework
    PDF never surfaces. Splitting on clause boundaries and interleaving the
    result lists gives every part of the question its own slots.
    """
    clauses = split_clauses(q)
    lists = [search(course_dir, q, k=k, where=where, per_source_cap=per_source_cap)]
    if len(clauses) > 1:
        per = max(2, k // len(clauses))
        lists += [search(course_dir, c, k=per, where=where,
                         per_source_cap=per_source_cap) for c in clauses]

    picked: list[dict] = []
    taken: set[str] = set()
    counts: dict[str, int] = {}
    cursors = [0] * len(lists)
    while len(picked) < k:
        progressed = False
        for li, lst in enumerate(lists):
            while cursors[li] < len(lst):
                row = lst[cursors[li]]
                cursors[li] += 1
                src = row["meta"].get("source", "?")
                if row["id"] in taken or counts.get(src, 0) >= per_source_cap:
                    continue
                taken.add(row["id"])
                counts[src] = counts.get(src, 0) + 1
                picked.append(row)
                progressed = True
                break
            if len(picked) >= k:
                break
        if not progressed:
            break
    return picked


def query(course_dir: Path, q: str, k: int = 5, where: dict | None = None,
          per_source_cap: int = 2) -> None:
    print(f"\nQuery: {q!r}\n" + "=" * 60)
    for r in search(course_dir, q, k=k, where=where, per_source_cap=per_source_cap):
        meta, doc = r["meta"], r["text"]
        label = meta.get("subject") or meta.get("lecture_date") or ""
        print(f"\n[{meta['category']}] {meta['source']} p{meta['page']}"
              f"{'  ' + label if label else ''}  (dist={r['dist']:.3f})")
        print("-" * 60)
        print(doc[:600].strip() + ("…" if len(doc) > 600 else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--query", help="semantic query (build index if absent)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--category", help="restrict to category (e.g. exams, homeworks)")
    ap.add_argument("--rebuild", action="store_true", help="force rebuild of index")
    ap.add_argument("--per-source", type=int, default=2,
                    help="max chunks taken from any one file (0 disables the cap)")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")

    db_path = cdir / "chroma"
    if args.rebuild or not db_path.exists() or not any(db_path.iterdir()):
        build_index(cdir)

    if args.query:
        where = {"category": args.category} if args.category else None
        query(cdir, args.query, k=args.k, where=where,
              per_source_cap=args.per_source or 10**6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
