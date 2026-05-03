"""Index a course's PDFs into a local Chroma vector DB for semantic search.

    python vectorize.py --course-dir downloads/128781_statistics    # build index
    python vectorize.py --course-dir <dir> --query "anova multiple comparison"
    python vectorize.py --course-dir <dir> --query "..." --k 8
"""
from __future__ import annotations

import argparse
import json
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


def get_collection(course_dir: Path):
    import chromadb
    from chromadb.utils import embedding_functions

    db_path = course_dir / "chroma"
    db_path.mkdir(exist_ok=True)
    client = chromadb.PersistentClient(path=str(db_path))
    embed = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    return client.get_or_create_collection(name="course_pdfs", embedding_function=embed)


def categorize(rel_path: str) -> str:
    manifest = json.loads((Path("dummy")).read_text()) if False else None
    return ""


def build_index(course_dir: Path) -> None:
    manifest_file = course_dir / "bundles" / "manifest.json"
    if not manifest_file.exists():
        sys.exit("Run bundle.py first to produce manifest.")
    manifest = {r["path"]: r["category"] for r in json.loads(manifest_file.read_text())}

    coll = get_collection(course_dir)
    # Reset to keep idempotent
    try:
        coll.delete(where={"$exists": "source"})
    except Exception:
        pass

    docs, ids, metas = [], [], []
    for pdf in sorted(course_dir.rglob("*.pdf")):
        rel = str(pdf.relative_to(course_dir))
        category = manifest.get(rel, "other")
        for c in iter_pdf_chunks(pdf):
            cid = f"{rel}#p{c['page']}-{c['chunk_idx']}"
            docs.append(c["text"])
            ids.append(cid)
            metas.append({
                "source": rel,
                "page": c["page"],
                "chunk": c["chunk_idx"],
                "category": category,
            })
        print(f"  indexed {rel}")

    BATCH = 200
    for i in range(0, len(docs), BATCH):
        coll.upsert(ids=ids[i : i + BATCH], documents=docs[i : i + BATCH], metadatas=metas[i : i + BATCH])
    print(f"\nIndexed {len(docs)} chunks into {course_dir / 'chroma'}")


def query(course_dir: Path, q: str, k: int = 5, where: dict | None = None) -> None:
    coll = get_collection(course_dir)
    res = coll.query(query_texts=[q], n_results=k, where=where)
    print(f"\nQuery: {q!r}\n" + "=" * 60)
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        print(f"\n[{meta['category']}] {meta['source']} p{meta['page']}  (dist={dist:.3f})")
        print("-" * 60)
        print(doc[:600].strip() + ("…" if len(doc) > 600 else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course-dir", required=True)
    ap.add_argument("--query", help="semantic query (build index if absent)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--category", help="restrict to category (e.g. exams, homeworks)")
    ap.add_argument("--rebuild", action="store_true", help="force rebuild of index")
    args = ap.parse_args()
    cdir = Path(args.course_dir)
    if not cdir.is_dir():
        sys.exit(f"Not a directory: {cdir}")

    db_path = cdir / "chroma"
    if args.rebuild or not db_path.exists() or not any(db_path.iterdir()):
        build_index(cdir)

    if args.query:
        where = {"category": args.category} if args.category else None
        query(cdir, args.query, k=args.k, where=where)
    return 0


if __name__ == "__main__":
    sys.exit(main())
