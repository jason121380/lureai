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
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
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
                chunk_ids_json TEXT NOT NULL,
                user_id INTEGER,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_twd REAL NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS sessions_token_hash_idx ON sessions(token_hash);
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'user'")
        self._ensure_column("audits", "user_id", "INTEGER")
        self._ensure_column("audits", "input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "cached_input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "cache_write_input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "output_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "cost_twd", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("audits", "model", "TEXT NOT NULL DEFAULT ''")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS audits_user_created_idx ON audits(user_id, created_at)"
        )
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"]) for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            try:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    def close(self) -> None:
        self.connection.close()

    def count_chunks(self) -> int:
        with self._lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def get_metadata(self, key: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM app_metadata WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO app_metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

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
                    reason, top_score, chunk_ids_json, user_id, input_tokens,
                    cached_input_tokens, cache_write_input_tokens, output_tokens,
                    cost_twd, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["trace_id"], record["created_at"], record.get("conversation_id"),
                    record["question"], record["status"], record.get("reason", ""),
                    record.get("top_score"), json.dumps(record.get("chunk_ids", [])),
                    record.get("user_id"), int(record.get("input_tokens", 0)),
                    int(record.get("cached_input_tokens", 0)),
                    int(record.get("cache_write_input_tokens", 0)),
                    int(record.get("output_tokens", 0)), float(record.get("cost_twd", 0)),
                    str(record.get("model", "")),
                ),
            )

    def usage_totals(self, user_id: int, start_at: str, end_at: str) -> dict:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(cache_write_input_tokens), 0) AS cache_write_input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_twd), 0) AS spend_twd
                FROM audits
                WHERE user_id = ? AND created_at >= ? AND created_at < ?
                """,
                (user_id, start_at, end_at),
            ).fetchone()
        return {
            "input_tokens": int(row["input_tokens"]),
            "cached_input_tokens": int(row["cached_input_tokens"]),
            "cache_write_input_tokens": int(row["cache_write_input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "spend_twd": float(row["spend_twd"]),
        }

    def list_audits(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self._lock:
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
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        return dict(row) if row else None

    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        if not query.strip():
            return []
        with self._lock:
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
        with self._lock:
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
