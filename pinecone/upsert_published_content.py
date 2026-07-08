"""Chunk the crawled Papaya blog posts and upsert into the papaya-kb Pinecone
index, under a separate 'published' namespace (kept apart from the fact
context layer in 'docs'). Backs the published_content_guide n8n tool.

Run:
    PINECONE_API_KEY=... python upsert_published_content.py
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
NAMESPACE = "published"
EMBED_MODEL = "llama-text-embed-v2"
CLOUD = "aws"
REGION = "us-east-1"

CRAWL_DIR = Path(__file__).resolve().parent / "crawled_blog"

TARGET_WORDS = 400
OVERLAP_WORDS = 50
MAX_WORDS = 700

URL_RE = re.compile(r"^<!-- url: (\S+) -->")


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


def iter_posts():
    for path in sorted(CRAWL_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines:
            continue
        url = ""
        m = URL_RE.match(lines[0])
        if m:
            url = m.group(1)
        title = ""
        for line in lines[1:6]:
            if line.startswith("# "):
                title = line[2:].strip()
                break
        body = "\n".join(lines[1:]).strip()
        if len(body) < 200:
            continue
        yield path.stem, url, title, body


def record_id(slug: str, idx: int) -> str:
    h = hashlib.sha1(f"{slug}#{idx}".encode()).hexdigest()[:12]
    return f"pub-{h}"


def main() -> int:
    api_key = os.environ.get("PINECONE_API_KEY")
    if not api_key:
        print("ERROR: set PINECONE_API_KEY", file=sys.stderr)
        return 1

    pc = Pinecone(api_key=api_key)
    index = pc.Index(INDEX_NAME)

    total_chunks = 0
    total_posts = 0
    for slug, url, title, body in iter_posts():
        total_posts += 1
        chunks = split_into_chunks(body)
        batch = []
        for i, ch in enumerate(chunks):
            batch.append(
                {
                    "_id": record_id(slug, i),
                    "chunk_text": ch[:50_000],
                    "url": url,
                    "title": title or slug,
                    "slug": slug,
                    "chunk_idx": i,
                }
            )
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
        total_chunks += len(batch)
        print(f"  {slug}: {len(chunks)} chunks", file=sys.stderr)

    print(f"done: {total_posts} posts, {total_chunks} chunks upserted", file=sys.stderr)
    print(f"NOTE: representative sample of {total_posts} most recent posts, not the full ~1,097-post archive", file=sys.stderr)

    stats = index.describe_index_stats()
    print(f"index stats: {stats}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
