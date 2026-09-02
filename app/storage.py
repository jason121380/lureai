import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Iterable

from .domains import DEFAULT_DOMAIN, DOMAIN_ORDER, domain_of, label as domain_label


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
                metadata_json TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'file',
                domain TEXT NOT NULL DEFAULT 'operations',
                aliases TEXT NOT NULL DEFAULT ''
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

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY,
                trace_id TEXT NOT NULL,
                user_id INTEGER,
                rating TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(trace_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_rules (
                rule_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                tone TEXT NOT NULL DEFAULT '',
                messages_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );
            """
        )
        self._ensure_column("chunks", "origin", "TEXT NOT NULL DEFAULT 'file'")
        self._ensure_column("chunks", "domain", f"TEXT NOT NULL DEFAULT '{DEFAULT_DOMAIN}'")
        self._ensure_column("chunks", "aliases", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'user'")
        self._ensure_column("audits", "user_id", "INTEGER")
        self._ensure_column("audits", "input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "cached_input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "cache_write_input_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "output_tokens", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "cost_twd", "REAL NOT NULL DEFAULT 0")
        # 語氣與品質重打次數：後台總覽要量得到「這個模式好不好」與「重打率」。
        self._ensure_column("audits", "tone", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("audits", "retries", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("audits", "model", "TEXT NOT NULL DEFAULT ''")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS audits_user_created_idx ON audits(user_id, created_at)"
        )
        self.connection.commit()

    def reply_metrics(self, since: str) -> dict:
        """後台總覽要看得到的三個數字：查不到資料的比例、品質重打率、平均輸入量。

        全部從既有的稽核算，不需要另外埋點。查不到資料的比例是「這個產品
        什麼時候在裝死」最直接的指標。
        """
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN reason IN ('no_results', 'low_confidence') THEN 1 ELSE 0 END) AS fallbacks,
                    SUM(CASE WHEN retries > 0 THEN 1 ELSE 0 END) AS retried,
                    AVG(input_tokens) AS avg_input_tokens
                FROM audits WHERE created_at >= ? AND status <> 'title'
                """,
                (since,),
            ).fetchone()
            votes = self.connection.execute(
                # 同一張卡上寫著「最近 30 天」，這一列卻查了全部期間——
                # 半年前的評分會一直把數字往上（或往下）拉，看不出這個月變好還變壞。
                "SELECT rating, COUNT(*) AS count FROM feedback"
                " WHERE created_at >= ? GROUP BY rating",
                (since,),
            ).fetchall()
        total = int(row["total"] or 0)
        counted = {str(vote["rating"]): int(vote["count"]) for vote in votes}
        graded = sum(counted.values())
        return {
            "replies": total,
            "fallback_rate": round(int(row["fallbacks"] or 0) / total, 4) if total else 0.0,
            "retry_rate": round(int(row["retried"] or 0) / total, 4) if total else 0.0,
            "avg_input_tokens": int(row["avg_input_tokens"] or 0),
            "thumbs_up_rate": round(counted.get("up", 0) / graded, 4) if graded else 0.0,
            "thumbs_down_rate": round(counted.get("down", 0) / graded, 4) if graded else 0.0,
            "graded": graded,
        }

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

    def model_rules(self) -> dict[str, str]:
        """後台改過的規則；沒改過的不會有列，由 app/tuning.py 補預設值。"""
        with self._lock:
            rows = self.connection.execute("SELECT rule_id, text FROM model_rules").fetchall()
        return {row["rule_id"]: row["text"] for row in rows}

    def save_model_rule(self, rule_id: str, text: str, updated_at: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO model_rules(rule_id, text, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(rule_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
                (rule_id, text, updated_at),
            )

    def delete_model_rule(self, rule_id: str) -> None:
        """還原預設就是把覆寫刪掉，讓 tuning 的預設值重新生效。"""
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM model_rules WHERE rule_id = ?", (rule_id,))

    def clear_model_rules(self) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM model_rules")

    # ---- 對話紀錄：存伺服器，換裝置也看得到（使用者決定要存資料庫）----

    def list_conversations(self, user_id: int, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT conversation_id, title, tone, messages_json, created_at, updated_at "
                "FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        conversations = []
        for row in rows:
            try:
                messages = json.loads(row["messages_json"])
            except (TypeError, ValueError):
                messages = []
            conversations.append({
                "id": row["conversation_id"],
                "title": row["title"],
                "tone": row["tone"],
                "messages": messages if isinstance(messages, list) else [],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            })
        return conversations

    def save_conversation(
        self, user_id: int, conversation_id: str, title: str, tone: str,
        messages: list, created_at: str, updated_at: str,
    ) -> None:
        payload = json.dumps(messages, ensure_ascii=False)
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO conversations"
                "(conversation_id, user_id, title, tone, messages_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "title = excluded.title, tone = excluded.tone, "
                "messages_json = excluded.messages_json, updated_at = excluded.updated_at "
                # 別人的對話不會被蓋掉：user_id 對不上就不動。
                "WHERE conversations.user_id = excluded.user_id",
                (conversation_id, user_id, title, tone, payload, created_at, updated_at),
            )

    def delete_conversation(self, user_id: int, conversation_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, user_id),
            )

    def prune_conversations(self, user_id: int, keep: int = 100) -> None:
        """一個帳號只留最近 N 段，避免無上限長大。"""
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM conversations WHERE user_id = ? AND conversation_id NOT IN ("
                "SELECT conversation_id FROM conversations WHERE user_id = ? "
                "ORDER BY updated_at DESC LIMIT ?)",
                (user_id, user_id, keep),
            )

    # ---- 個人偏好（語氣等）：跟著帳號走，換裝置不用重設 ----

    def user_prefs(self, user_id: int) -> dict[str, str]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT key, value FROM user_prefs WHERE user_id = ?", (user_id,)
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_user_pref(self, user_id: int, key: str, value: str, updated_at: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO user_prefs(user_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (user_id, key, value, updated_at),
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
                    cost_twd, model, tone, retries
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["trace_id"], record["created_at"], record.get("conversation_id"),
                    record["question"], record["status"], record.get("reason", ""),
                    record.get("top_score"), json.dumps(record.get("chunk_ids", [])),
                    record.get("user_id"), int(record.get("input_tokens", 0)),
                    int(record.get("cached_input_tokens", 0)),
                    int(record.get("cache_write_input_tokens", 0)),
                    int(record.get("output_tokens", 0)), float(record.get("cost_twd", 0)),
                    str(record.get("model", "")), str(record.get("tone", "")),
                    int(record.get("retries", 0)),
                ),
            )

    def add_feedback(self, trace_id: str, user_id: int | None, rating: str, created_at: str) -> None:
        """每人對每則回答一票，重按就更新（讚改倒讚）。"""
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO feedback (trace_id, user_id, rating, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(trace_id, user_id) DO UPDATE SET rating = excluded.rating, created_at = excluded.created_at
                """,
                (trace_id, user_id, rating, created_at),
            )

    def list_feedback(self, limit: int = 100) -> list[dict]:
        """回饋列表（附上稽核裡的問題與狀態），給後台看哪些回答要加強。"""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT f.trace_id, f.user_id, f.rating, f.created_at,
                       a.question, a.status, a.reason, a.top_score
                FROM feedback f
                LEFT JOIN audits a ON a.trace_id = f.trace_id
                ORDER BY f.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def knowledge_composition(self) -> dict:
        """Chunk counts by domain, category and origin, for the admin overview."""
        with self._lock:
            categories = self.connection.execute(
                "SELECT domain, COALESCE(NULLIF(category, ''), '未分類') AS name, "
                "COUNT(*) AS count FROM chunks GROUP BY domain, name ORDER BY count DESC"
            ).fetchall()
            origins = self.connection.execute(
                "SELECT origin, COUNT(*) AS count FROM chunks GROUP BY origin"
            ).fetchall()
            sources = self.connection.execute(
                "SELECT COUNT(DISTINCT source_file) AS count FROM chunks"
            ).fetchone()
        by_domain: dict[str, list[dict]] = {key: [] for key in DOMAIN_ORDER}
        for row in categories:
            key = str(row["domain"]) if str(row["domain"]) in by_domain else DEFAULT_DOMAIN
            by_domain[key].append({"name": row["name"], "count": int(row["count"])})
        return {
            "domains": [
                {
                    "key": key,
                    "label": domain_label(key),
                    "count": sum(item["count"] for item in by_domain[key]),
                    "categories": by_domain[key],
                }
                for key in DOMAIN_ORDER
            ],
            "origins": {str(row["origin"]): int(row["count"]) for row in origins},
            "source_files": int(sources["count"]),
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

    def list_chunks(
        self,
        limit: int = 100,
        offset: int = 0,
        origin: str = "",
        domain: str = "",
    ) -> list[dict]:
        # Filter in SQL so the limit applies to the filtered set, not to the
        # first N rows of the whole corpus.
        where = []
        params: list = []
        if origin:
            where.append("origin = ?")
            params.append(origin)
        if domain:
            where.append("domain = ?")
            params.append(domain)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM chunks {clause} ORDER BY title, locator LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def related_chunks(
        self,
        category: str = "",
        domain: str = "",
        source_file: str = "",
        exclude_ids: Iterable[str] = (),
        limit: int = 12,
    ) -> list[dict]:
        """Neighbouring knowledge: same category first, then same playbook, then same domain."""
        excluded = [str(chunk_id) for chunk_id in exclude_ids]
        placeholders = ",".join("?" for _ in excluded) or "''"
        with self._lock:
            rows = self.connection.execute(
                f"""
                SELECT chunk_id, locator, section_title, category, domain, source_file
                FROM chunks
                WHERE chunk_id NOT IN ({placeholders})
                ORDER BY (category = ?) DESC, (source_file = ?) DESC, (domain = ?) DESC, locator
                LIMIT ?
                """,
                (*excluded, category, source_file, domain, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _insert_chunk(self, row: dict, origin: str) -> None:
        cursor = self.connection.execute(
            """
            INSERT INTO chunks (
                chunk_id, doc_id, locator, section_title, text, title,
                source_file, source_sha256, category, access_level,
                customer_service_allowed, review_status, reviewer,
                reviewed_at, search_text, metadata_json, origin, domain, aliases
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["chunk_id"], row.get("doc_id"), row["locator"],
                row.get("section_title", ""), row["text"], row["title"],
                row["source_file"], row.get("source_sha256", ""),
                row.get("category", ""), row["access_level"],
                int(row.get("customer_service_allowed") is True),
                row["review_status"], row.get("reviewer", ""),
                row.get("reviewed_at", ""), row["search_text"],
                json.dumps(row, ensure_ascii=False), origin, domain_of(row),
                # 一個問法一行：問法索引是人工寫的「這句話問的是哪一塊知識」，
                # 用空白接起來就分不出邊界，也就比不出「整句正好是這個問法」。
                "\n".join(row.get("aliases") or []) if isinstance(row.get("aliases"), list)
                else str(row.get("aliases") or ""),
            ),
        )
        self.connection.execute(
            "INSERT INTO chunks_fts(rowid, chunk_id, title, section_title, search_text) VALUES (?, ?, ?, ?, ?)",
            (
                cursor.lastrowid, row["chunk_id"], row["title"],
                row.get("section_title", ""), row["search_text"],
            ),
        )

    def _delete_chunk_rows(self, where: str, params: tuple) -> None:
        """Delete chunks matching a predicate along with their FTS entries."""
        ids = [
            int(row[0])
            for row in self.connection.execute(f"SELECT id FROM chunks WHERE {where}", params)
        ]
        for chunk_row_id in ids:
            self.connection.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_row_id,))
        self.connection.execute(f"DELETE FROM chunks WHERE {where}", params)

    def replace_chunks(self, chunks: Iterable[dict]) -> None:
        """Replace file-sourced chunks; knowledge authored in the admin survives."""
        rows = list(chunks)
        with self._lock, self.connection:
            self._delete_chunk_rows("origin = ?", ("file",))
            for row in rows:
                self._insert_chunk(row, "file")

    def upsert_custom_chunk(self, row: dict) -> None:
        with self._lock, self.connection:
            self._delete_chunk_rows("chunk_id = ?", (row["chunk_id"],))
            self._insert_chunk(row, "custom")

    def delete_custom_chunk(self, chunk_id: str) -> bool:
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT id FROM chunks WHERE chunk_id = ? AND origin = 'custom'", (chunk_id,)
            ).fetchone()
            if not existing:
                return False
            self._delete_chunk_rows("chunk_id = ? AND origin = 'custom'", (chunk_id,))
        return True

    def all_chunk_payloads(self) -> list[dict]:
        """Every indexed chunk as its original payload, for export."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT metadata_json FROM chunks ORDER BY origin DESC, title, locator"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
