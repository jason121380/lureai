"""不掛 Volume 的持久化：把不能掉的資料快照到 PostgreSQL。

SQLite 仍是工作資料庫（FTS5 檢索都在裡面），容器重新部署時歸零沒關係——
知識索引本來就由 JSONL 重建。這裡把「不能掉的」資料——帳號、session、
稽核／用量、回饋評分、後台自訂知識——壓成 gzip JSON 快照存進 Postgres
（保留多版本及單寫入者鎖），開機時還原、之後背景執行緒定期備份（內容沒變就不上傳）。

psycopg 只在設定了 Postgres 連線時才需要（Dockerfile 會安裝）；本機開發
沒設定連線字串時整個模組是 no-op，維持零依賴。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone

SNAPSHOT_TABLE = "lureai_snapshot"
# 快照涵蓋的 SQLite 資料表；chunks 只收後台自訂（origin='custom'）的原始 payload。
DURABLE_TABLES = (
    "users", "sessions", "audits", "feedback", "model_rules",
    "conversations", "user_prefs",
)
DEFAULT_INTERVAL_SECONDS = 120


def _log(message: str) -> None:
    print(f"[pg] {message}", file=sys.stderr, flush=True)


def connection_string() -> str:
    """Zeabur 綁 PostgreSQL 服務時會注入其中一種變數；也支援手動設定。"""
    for key in ("DATABASE_URL", "POSTGRES_CONNECTION_STRING", "POSTGRES_URI", "POSTGRES_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    host = os.getenv("POSTGRES_HOST", "").strip()
    if not host:
        return ""
    user = os.getenv("POSTGRES_USERNAME") or os.getenv("POSTGRES_USER") or "postgres"
    password = os.getenv("POSTGRES_PASSWORD", "")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DATABASE") or os.getenv("POSTGRES_DB") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _load_driver():
    try:
        import psycopg  # noqa: PLC0415 - 只有設定 Postgres 時才需要

        return psycopg
    except ImportError:
        return None


class PostgresReplica:
    def __init__(self, dsn: str, driver=None, interval: int | None = None):
        self.dsn = str(dsn or "")
        self.driver = driver if driver is not None else (_load_driver() if self.dsn else None)
        try:
            self.interval = int(interval or os.getenv("PG_BACKUP_INTERVAL_SECONDS", "") or DEFAULT_INTERVAL_SECONDS)
        except ValueError:
            self.interval = DEFAULT_INTERVAL_SECONDS
        self.interval = max(15, self.interval)
        self.last_backup_at: str | None = None
        # 背景備份最後一次失敗的原因。健康檢查看這個就好，不必自己再備份一次
        # （備份要把所有 durable 表讀出來，會佔住 store 的鎖）。
        self.last_error: str | None = None
        self._last_digest: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._operation_lock = threading.RLock()
        self._writer = None
        self._eligible = False
        self.on_writer_lost = None

    @property
    def writable(self):
        return self._eligible and self._writer is not None and not self._stop.is_set()

    @classmethod
    def from_env(cls) -> "PostgresReplica":
        return cls(connection_string())

    @property
    def configured(self) -> bool:
        return bool(self.dsn)

    @property
    def enabled(self) -> bool:
        return bool(self.dsn and self.driver)

    def _connect(self):
        return self.driver.connect(
            self.dsn, autocommit=True, connect_timeout=5,
            keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=3,
            tcp_user_timeout=15000,
            options="-c statement_timeout=10000 -c lock_timeout=5000",
        )

    @staticmethod
    def _ensure_table(conn) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} "
            "(id INTEGER PRIMARY KEY, data BYTEA NOT NULL, updated_at TEXT NOT NULL)"
        )

    @staticmethod
    def _durable_tables(store):
        # Derived search/index tables are rebuilt; every other application table is durable.
        return tuple(row[0] for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid"
        ) if row[0] not in {"chunks", "app_metadata"}
            and not row[0].startswith(("sqlite_", "chunks_fts")))

    def export_snapshot(self, store) -> bytes:
        payload: dict = {"version": 1, "tables": {}, "custom_chunks": []}
        with store._lock:
            for table in self._durable_tables(store):
                rows = store.connection.execute(f"SELECT * FROM {table}").fetchall()
                payload["tables"][table] = [dict(row) for row in rows]
            for row in store.connection.execute(
                "SELECT metadata_json FROM chunks WHERE origin = 'custom' ORDER BY chunk_id"
            ).fetchall():
                try:
                    payload["custom_chunks"].append(json.loads(row["metadata_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        # mtime=0：gzip 輸出不夾時間戳，內容沒變時位元組完全相同，才能靠雜湊略過上傳。
        return gzip.compress(raw, mtime=0)

    def apply_snapshot(self, store, data: bytes) -> dict:
        payload = json.loads(gzip.decompress(data).decode("utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("tables"), dict):
            raise ValueError("unsupported or malformed snapshot")
        tables = payload["tables"]
        if set(tables) - set(self._durable_tables(store)):
            raise ValueError("snapshot contains unknown durable tables; schema upgrade required")
        if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
               for rows in tables.values()):
            raise ValueError("malformed durable rows")
        if not isinstance(payload.get("custom_chunks"), list) or any(
                not isinstance(row, dict) for row in payload["custom_chunks"]):
            raise ValueError("malformed custom chunks")
        if not all(table in tables and isinstance(tables[table], list) for table in DURABLE_TABLES):
            raise ValueError("snapshot missing durable tables")
        counts: dict[str, int] = {}
        with store._lock, store.connection:
            for table in self._durable_tables(store):
                rows = [row for row in (tables.get(table) or []) if isinstance(row, dict)]
                schema = store.connection.execute(f"PRAGMA table_info({table})").fetchall()
                known = {str(info[1]) for info in schema}
                required = {str(info[1]) for info in schema
                            if info[5] or (info[3] and info[4] is None)}
                store.connection.execute(f"DELETE FROM {table}")
                for row in rows:
                    # 欄位取交集：之後 schema 加欄位，舊快照仍能還原。
                    columns = [key for key in row.keys() if key in known]
                    if not columns or required - set(row):
                        raise ValueError(f"malformed durable row in {table}")
                    placeholders = ", ".join("?" for _ in columns)
                    store.connection.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        [row[column] for column in columns],
                    )
                counts[table] = len(rows)
        custom_rows = [row for row in (payload.get("custom_chunks") or []) if isinstance(row, dict)]
        # 快照是自訂知識的全量真相：跟 DURABLE_TABLES 一樣先清空再寫回。
        # 只 upsert 的話，資料庫裡快照沒有的那幾則（在別台刪掉的）會留著復活。
        store.clear_custom_chunks()
        for row in custom_rows:
            store.upsert_custom_chunk(row)
        counts["custom_chunks"] = len(custom_rows)
        return counts

    def _release_writer(self):
        self._eligible = False
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def restore(self, store) -> bool:
        """Acquire the only writer session before reading; errors never authorize writes."""
        with self._operation_lock:
            if self._writer is not None or self._stop.is_set():
                raise RuntimeError("replica already started or stopped")
            try:
                self._writer = self._connect()
                if not self._writer.execute("SELECT pg_try_advisory_lock(718239041)").fetchone()[0]:
                    raise RuntimeError("another instance owns the snapshot writer lock")
                self._ensure_table(self._writer)
                self._writer.execute(
                    "CREATE TABLE IF NOT EXISTS lureai_snapshot_history "
                    "(id BIGSERIAL PRIMARY KEY, data BYTEA NOT NULL, updated_at TEXT NOT NULL)"
                )
                row = self._writer.execute(f"SELECT data FROM {SNAPSHOT_TABLE} WHERE id = 1").fetchone()
                if row:
                    data = bytes(row[0])
                    self.apply_snapshot(store, data)
                    self._last_digest = hashlib.sha256(data).hexdigest()
                self._eligible = True
                return bool(row)
            except Exception:
                self._release_writer()
                raise

    def check_writer(self) -> bool:
        """Verify the lock-owning session before admitting or acknowledging writes."""
        with self._operation_lock:
            if not self.writable:
                return False
            try:
                self._writer.execute("SELECT 1")
                return True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._release_writer()
                if self.on_writer_lost:
                    self.on_writer_lost()
                return False

    def backup(self, store) -> bool:
        with self._operation_lock:
            if not self._eligible or self._writer is None:
                raise RuntimeError("successful restore and writer ownership required")
            try:
                # Check the original lock-owning session even when content is unchanged.
                self._writer.execute("SELECT 1")
                data = self.export_snapshot(store)
                digest = hashlib.sha256(data).hexdigest()
                if digest == self._last_digest:
                    return False
                now = datetime.now(timezone.utc).isoformat()
                with self._writer.transaction():
                    # Preserve a legacy head before its first replacement too.
                    self._writer.execute(
                        "INSERT INTO lureai_snapshot_history (data, updated_at) "
                        "SELECT data, updated_at FROM lureai_snapshot WHERE id = 1 "
                        "AND NOT EXISTS (SELECT 1 FROM lureai_snapshot_history)"
                    )
                    self._writer.execute(
                        "INSERT INTO lureai_snapshot_history (data, updated_at) VALUES (%s, %s)",
                        (data, now),
                    )
                    self._writer.execute(
                        f"INSERT INTO {SNAPSHOT_TABLE} (id, data, updated_at) VALUES (1, %s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
                        (data, now),
                    )
                self._last_digest = digest
                self.last_backup_at = now
                self.last_error = None
                return True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._release_writer()
                if self.on_writer_lost:
                    self.on_writer_lost()
                raise

    def probe(self) -> dict:
        """只確認「連得上、讀得到快照」，不做備份。

        健康檢查原本每次都呼叫 `backup()`，而 `export_snapshot` 會在
        `store._lock` 裡把所有 durable 表（含全部對話紀錄）讀出來——後台一進
        知識庫分頁就 health 與 chunks 同時打，chunks 只能等鎖，畫面卡在
        「載入中」。備份本來就有背景執行緒每 `interval` 跑一次，健康檢查
        只要回報它的結果就夠了。
        """
        with self._connect() as conn:
            self._ensure_table(conn)
            row = conn.execute(
                f"SELECT updated_at, octet_length(data) AS size FROM {SNAPSHOT_TABLE} WHERE id = 1"
            ).fetchone()
        if not row:
            return {"snapshot": False}
        updated_at, size = (row[0], row[1]) if not isinstance(row, dict) else (row["updated_at"], row["size"])
        return {"snapshot": True, "snapshot_updated_at": str(updated_at), "snapshot_bytes": int(size or 0)}

    def start(self, store) -> None:
        if not self.enabled or self._thread is not None:
            return

        if not self.writable:
            raise RuntimeError("successful restore and writer ownership required")
        self.backup(store)

        def loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    self.backup(store)
                except Exception as exc:  # noqa: BLE001 - 備份失敗不能弄掛服務
                    self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    _log(f"backup failed: {type(exc).__name__}: {str(exc)[:200]}")

        self._thread = threading.Thread(target=loop, daemon=True, name="pg-replica")
        self._thread.start()

    def stop(self, store=None) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        with self._operation_lock:
            try:
                if store is not None and self._eligible:
                    self.backup(store)
            finally:
                self._release_writer()
