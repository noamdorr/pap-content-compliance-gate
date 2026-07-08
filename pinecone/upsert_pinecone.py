"""Chunk the Papaya knowledge-base doc(s) and upsert into a Pinecone index
with integrated embedding (same pattern as the Incredibuild build).

Run:
    PINECONE_API_KEY=... python upsert_pinecone.py
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path

from pinecone import Pinecone

INDEX_NAME = "papaya-kb"
NAMESPACE = "docs"
EMBED_MODEL = "llama-text-embed-v2"
CLOUD = "aws"
REGION = "us-east-1"

# One entry per source doc: (path, section tag)
SOURCE_DOCS = [
    (Path(__file__).resolve().parent.parent / "papaya-context-source.md", "context"),
    (Path(__file__).resolve().parent.parent / "papaya-content-analysis.md", "content_analysis"),
    (Path(__file__).resolve().parent.parent / "shahar-quotes.md", "quotes"),
]

TARGET_WORDS = 400
OVERLAP_WORDS = 50
MAX_WORDS = 700


def split_into_chunks(text: str) -> list[str]:
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    cur: list[str] = []
    cur_words = 0

    def flush() -> None:
        nonlocal cur, cur_words
        if cur:
            chunks.append("\n\n".join(cur).strip())
            cur = []
            cur_words = 0

    for para in paragraphs:
        wc = len(para.split())
        if wc == 0:
            continue
        if wc > MAX_WORDS:
            flush()
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf: list[str] = []
            buf_w = 0
            for s in sentences:
                sw = len(s.split())
                if buf_w + sw > TARGET_WORDS and buf:
                    chunks.append(" ".join(buf).strip())
                    buf, buf_w = [], 0
                buf.append(s)
                buf_w += sw
            if buf:
                chunks.append(" ".join(buf).strip())
            continue
        if cur_words + wc > TARGET_WORDS and cur:
            flush()
        cur.append(para)
        cur_words += wc
    flush()

    overlapped: list[str] = []
    for i, ch in enumerate(chunks):
        if i == 0:
            overlapped.append(ch)
            continue
        tail = " ".join(chunks[i - 1].split()[-OVERLAP_WORDS:])
        overlapped.append(f"{tail}\n\n{ch}")
    return overlapped


def record_id(doc_name: str, idx: int) -> str:
    h = hashlib.sha1(f"{doc_name}#{idx}".encode()).hexdigest()[:12]
    return f"papaya-{h}"


def main() -> int:
    api_key = os.environ.get("PINECONE_API_KEY")
    if not api_key:
        print("ERROR: set PINECONE_API_KEY", file=sys.stderr)
        return 1

    pc = Pinecone(api_key=api_key)

    existing = {i["name"] for i in pc.list_indexes()}
    if INDEX_NAME not in existing:
        print(f"creating index {INDEX_NAME} (model={EMBED_MODEL}) ...", file=sys.stderr)
        pc.create_index_for_model(
            name=INDEX_NAME,
            cloud=CLOUD,
            region=REGION,
            embed={"model": EMBED_MODEL, "field_map": {"text": "chunk_text"}},
        )
        while True:
            desc = pc.describe_index(INDEX_NAME)
            if desc.status["ready"]:
                break
            time.sleep(2)
        print("  index ready", file=sys.stderr)
    else:
        print(f"using existing index {INDEX_NAME}", file=sys.stderr)

    index = pc.Index(INDEX_NAME)

    total_records = 0
    for path, section in SOURCE_DOCS:
        if not path.exists():
            print(f"  skip (missing): {path}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else path.stem
        body = "\n".join(lines[1:]).strip()

        chunks = split_into_chunks(body)
        batch = []
        for i, ch in enumerate(chunks):
            rec = {
                "_id": record_id(path.stem, i),
                "chunk_text": ch[:50_000],
                "title": title,
                "doc": path.stem,
                "section": section,
                "chunk_idx": i,
            }
            batch.append(rec)

        for i in range(0, len(batch), 32):
            sub = batch[i : i + 32]
            for attempt in range(5):
                try:
                    index.upsert_records(namespace=NAMESPACE, records=sub)
                    break
                except Exception as e:
                    wait = 2**attempt
                    print(f"  retry after {wait}s: {str(e)[:200]}", file=sys.stderr)
                    time.sleep(wait)
        total_records += len(batch)
        print(f"  {path.name}: {len(chunks)} chunks upserted", file=sys.stderr)

    print(f"done: {total_records} records total", file=sys.stderr)

    stats = index.describe_index_stats()
    print(f"index stats: {stats}", file=sys.stderr)

    desc = pc.describe_index(INDEX_NAME)
    print(f"index host: {desc.host}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
