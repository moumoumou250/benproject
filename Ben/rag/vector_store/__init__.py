"""Transactional SQLite vector persistence; exact cosine retrieval for local KBs."""

import json
import math
import sqlite3
from contextlib import contextmanager


class VectorStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("PRAGMA journal_mode=PERSIST")
            db.execute(
                "CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, text TEXT, metadata TEXT, vector TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            yield db

    def replace(self, rows, signature):
        with self.connect() as db:
            db.execute("DELETE FROM chunks")
            db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?)", rows)
            db.execute("INSERT OR REPLACE INTO config VALUES ('signature', ?)", (signature,))

    def status(self):
        with self.connect() as db:
            count = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            sig = db.execute("SELECT value FROM config WHERE key='signature'").fetchone()
            return {"chunks": count, "signature": sig[0] if sig else None}

    def search(self, vector, signature, k, source, min_score):
        with self.connect() as db:
            sig = db.execute("SELECT value FROM config WHERE key='signature'").fetchone()
            if sig and sig[0] != signature:
                raise ValueError("Embedding configuration changed; run python -m Ben.rag ingest")
            rows = db.execute("SELECT id, text, metadata, vector FROM chunks").fetchall()
        results = []
        for chunk_id, text, meta, raw in rows:
            metadata = json.loads(meta)
            if source and metadata["source"] != source:
                continue
            other = json.loads(raw)
            if len(other) != len(vector):
                raise ValueError("Embedding dimension mismatch; rebuild index")
            norm = math.sqrt(sum(x * x for x in vector) * sum(x * x for x in other))
            score = sum(a * b for a, b in zip(vector, other, strict=True)) / norm if norm else 0
            if score >= min_score:
                results.append({"chunk_id": chunk_id, "content": text, "score": score, **metadata})
        return sorted(results, key=lambda item: item["score"], reverse=True)[:k]
