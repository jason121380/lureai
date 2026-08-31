import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Iterable


class KnowledgeStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                chunk_id TEXT NOT NULL UNIQUE,
                doc_id TEXT,
                locator TEXT NOT NULL,
                section_title TEXT,
                text TEXT NOT NULL,
                title TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_sha256 TEXT,
                category TEXT,
                access_level TEXT NOT NULL,
                customer_service_allowed INTEGER NOT NULL,
                review_status TEXT NOT NULL,
                reviewer TEXT,
                reviewed_at TEXT,
                search_text TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                title,
                section_title,
                search_text,
                tokenize='unicode61'
            );

            CREATE TABLE IF NOT EXISTS audits (
                id INTEGER PRIMARY KEY,
                trace_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                conversation_id TEXT,
                question TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                top_score REAL,
                chunk_ids_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def count_chunks(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def index_health(self) -> dict:
        with self._lock:
            chunks = int(self.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            fts_chunks = int(self.connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0])
        return {"chunks": chunks, "fts_chunks": fts_chunks}

    def indexed_chunks_for_health(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("SELECT metadata_json FROM chunks").fetchall()
        return [json.loads(row[0]) for row in rows]

    def health_check(self) -> dict:
        database_uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        probe = sqlite3.connect(database_uri, uri=True, timeout=1)
        try:
            integrity = str(probe.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            probe.close()
        writable = os.access(self.db_path, os.W_OK) and os.access(self.db_path.parent, os.W_OK)
        if writable:
            try:
                with tempfile.NamedTemporaryFile(dir=self.db_path.parent, prefix=".health-", delete=True) as handle:
                    handle.write(b"ok")
                    handle.flush()
            except OSError:
                writable = False
        size_bytes = self.db_path.stat().st_size if self.db_path.is_file() else 0
        return {"integrity": integrity, "writable": writable, "size_bytes": size_bytes}

    def add_audit(self, record: dict) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO audits (
                    trace_id, created_at, conversation_id, question, status,
                    reason, top_score, chunk_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["trace_id"], record["created_at"], record.get("conversation_id"),
                    record["question"], record["status"], record.get("reason", ""),
                    record.get("top_score"), json.dumps(record.get("chunk_ids", [])),
                ),
            )

    def list_audits(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        status_rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM audits GROUP BY status"
        ).fetchall()
        category_rows = self.connection.execute(
            "SELECT category, COUNT(*) AS count FROM chunks GROUP BY category ORDER BY count DESC"
        ).fetchall()
        return {
            "chunks": self.count_chunks(),
            "audits": sum(int(row["count"]) for row in status_rows),
            "statuses": {row["status"]: int(row["count"]) for row in status_rows},
            "categories": [{"name": row["category"] or "未分類", "count": int(row["count"])} for row in category_rows],
        }

    def get_chunk(self, chunk_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return dict(row) if row else None

    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        if not query.strip():
            return []
        rows = self.connection.execute(
            """
            SELECT chunks.*, bm25(chunks_fts, 0.0, 2.0, 1.4, 1.0) AS rank
            FROM chunks_fts
            JOIN chunks ON chunks.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_chunks(self, limit: int = 100, offset: int = 0) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM chunks ORDER BY title, locator LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_chunks(self, chunks: Iterable[dict]) -> None:
        rows = list(chunks)
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM chunks_fts")
            self.connection.execute("DELETE FROM chunks")
            for row in rows:
                cursor = self.connection.execute(
                    """
                    INSERT INTO chunks (
                        chunk_id, doc_id, locator, section_title, text, title,
                        source_file, source_sha256, category, access_level,
                        customer_service_allowed, review_status, reviewer,
                        reviewed_at, search_text, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["chunk_id"], row.get("doc_id"), row["locator"],
                        row.get("section_title", ""), row["text"], row["title"],
                        row["source_file"], row.get("source_sha256", ""),
                        row.get("category", ""), row["access_level"],
                        int(row.get("customer_service_allowed") is True),
                        row["review_status"], row.get("reviewer", ""),
                        row.get("reviewed_at", ""), row["search_text"],
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO chunks_fts(rowid, chunk_id, title, section_title, search_text) VALUES (?, ?, ?, ?, ?)",
                    (
                        cursor.lastrowid, row["chunk_id"], row["title"],
                        row.get("section_title", ""), row["search_text"],
                    ),
                )
