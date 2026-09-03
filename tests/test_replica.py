import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.auth import AuthManager
from app.ingest import ingest_jsonl
from app.replica import PostgresReplica, connection_string
from app.server import build_custom_chunk
from app.storage import KnowledgeStore

from tests.test_ingest import approved_chunk


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """最小的 psycopg 介面替身：單列快照存進共用 dict。"""

    def __init__(self, storage):
        self.storage = storage

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("CREATE TABLE"):
            return FakeResult()
        if sql.startswith("SELECT data"):
            return FakeResult((self.storage["data"],) if "data" in self.storage else None)
        if sql.startswith("SELECT updated_at"):
            # 健康檢查的輕量探測：只讀時間與大小，不寫。
            if "data" not in self.storage:
                return FakeResult(None)
            return FakeResult((self.storage.get("updated_at", ""), len(self.storage["data"])))
        if sql.startswith("INSERT INTO lureai_snapshot"):
            self.storage["data"] = params[0]
            self.storage["updated_at"] = params[1]
            return FakeResult()
        raise AssertionError(f"unexpected sql: {sql}")


class FakeDriver:
    def __init__(self):
        self.storage = {}

    def connect(self, _dsn, autocommit=True):
        assert autocommit
        return FakeConn(self.storage)


def make_store(root: Path, name: str) -> KnowledgeStore:
    store = KnowledgeStore(root / name)
    source = root / f"{name}.jsonl"
    source.write_text(
        json.dumps(approved_chunk(
            chunk_id="file-1", locator="file-1", title="檔案知識",
            text="燙髮後整理時，依照設計師示範方向吹整。",
        ), ensure_ascii=False),
        encoding="utf-8",
    )
    ingest_jsonl(store, source)
    return store


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_round_trip_restores_durable_data(self):
        driver = FakeDriver()
        source = make_store(self.root, "source.db")
        auth = AuthManager(source)
        user = auth.create_or_reset_user("designer", "pass-1234", role="admin")
        source.add_feedback("trace-1", user["id"], "down", "2026-09-01T00:00:00+00:00")
        source.upsert_custom_chunk(build_custom_chunk({
            "section_title": "後台新增的知識",
            "category": "自訂",
            "domain": "coaching",
            "text": "這是一則後台新增、必須在重新部署後活下來的知識。",
        }, "internal_coaching"))

        replica = PostgresReplica("postgresql://fake", driver=driver, interval=999)
        self.assertTrue(replica.backup(source))
        # 內容沒變就不重複上傳。
        self.assertFalse(replica.backup(source))
        source.close()

        target = make_store(self.root, "target.db")
        fresh = PostgresReplica("postgresql://fake", driver=driver, interval=999)
        self.assertTrue(fresh.restore(target))

        users = AuthManager(target).list_users()
        self.assertEqual([item["username"] for item in users], ["designer"])
        self.assertEqual(users[0]["role"], "admin")
        feedback = target.list_feedback()
        self.assertEqual(feedback[0]["rating"], "down")
        customs = target.list_chunks(limit=1000, origin="custom")
        self.assertEqual(len(customs), 1)
        self.assertEqual(customs[0]["section_title"], "後台新增的知識")
        # 檔案知識不進快照，還原後仍是目標庫自己 ingest 的那份。
        self.assertEqual(len(target.list_chunks(limit=1000, origin="file")), 1)
        target.close()

    def test_restore_does_not_resurrect_custom_chunks_deleted_elsewhere(self):
        """快照是自訂知識的全量真相：目標庫裡快照沒有的那幾則（在別台刪掉的）
        還原後必須消失，只 upsert 不清空會讓刪掉的知識復活。"""
        driver = FakeDriver()
        source = make_store(self.root, "source.db")
        source.upsert_custom_chunk(build_custom_chunk({
            "section_title": "留下來的知識",
            "category": "自訂",
            "domain": "coaching",
            "text": "這一則在快照裡，必須在還原之後活著。",
        }, "internal_coaching"))
        replica = PostgresReplica("postgresql://fake", driver=driver, interval=999)
        self.assertTrue(replica.backup(source))
        source.close()

        target = make_store(self.root, "target.db")
        target.upsert_custom_chunk(build_custom_chunk({
            "section_title": "已在別台刪掉的知識",
            "category": "自訂",
            "domain": "coaching",
            "text": "這一則不在快照裡，代表已經被刪掉，不可以復活。",
        }, "internal_coaching"))
        fresh = PostgresReplica("postgresql://fake", driver=driver, interval=999)
        self.assertTrue(fresh.restore(target))

        customs = target.list_chunks(limit=1000, origin="custom")
        self.assertEqual([item["section_title"] for item in customs], ["留下來的知識"])
        target.close()

    def test_restore_returns_false_when_snapshot_is_empty(self):
        target = make_store(self.root, "empty.db")
        replica = PostgresReplica("postgresql://fake", driver=FakeDriver(), interval=999)
        self.assertFalse(replica.restore(target))
        target.close()

    def test_health_check_reports_postgres_state(self):
        from types import SimpleNamespace

        from app.health import _persistence_check

        store = make_store(self.root, "health.db")
        try:
            unset = SimpleNamespace(store=store, replica=None, restored_from_replica=False)
            status, message, details = _persistence_check(unset)
            self.assertEqual(status, "warning")
            self.assertEqual(details["storage"], "sqlite-only")
            self.assertIn("重新部署", message)

            connected = SimpleNamespace(
                store=store,
                replica=PostgresReplica("postgresql://fake", driver=FakeDriver(), interval=999),
                restored_from_replica=True,
            )
            # 還沒有任何快照時是 warning，不是 ok——連得上但東西還沒落地。
            status, _message, details = _persistence_check(connected)
            self.assertEqual(status, "warning")
            self.assertEqual(details["storage"], "postgres")
            self.assertTrue(details["restored_on_boot"])
            self.assertFalse(details["snapshot"])

            # **健康檢查不可以自己備份**：備份會在 store 的鎖裡把所有 durable 表
            # 讀出來，後台的知識庫分頁會被它卡住（畫面停在「載入中」）。
            connected.replica.backup(store)
            status, _message, details = _persistence_check(connected)
            self.assertEqual(status, "ok")
            self.assertTrue(details["snapshot"])
            self.assertTrue(details["snapshot_updated_at"])
            self.assertGreater(details["snapshot_bytes"], 0)
        finally:
            store.close()

    def test_health_check_does_not_run_a_backup(self):
        """健康檢查只探測，不備份——備份會佔住 store 的鎖。"""
        from types import SimpleNamespace

        from app.health import _persistence_check

        store = make_store(self.root, "no-backup.db")
        try:
            replica = PostgresReplica("postgresql://fake", driver=FakeDriver(), interval=999)
            replica.backup(store)
            before = replica.last_backup_at
            calls = []
            original = replica.export_snapshot
            replica.export_snapshot = lambda target: (calls.append(target), original(target))[1]

            _persistence_check(SimpleNamespace(
                store=store, replica=replica, restored_from_replica=True))

            self.assertEqual(calls, [], "健康檢查不應該讀出整份快照")
            self.assertEqual(replica.last_backup_at, before)
        finally:
            store.close()

    def test_health_check_surfaces_a_failing_background_backup(self):
        from types import SimpleNamespace

        from app.health import _persistence_check

        store = make_store(self.root, "backup-error.db")
        try:
            replica = PostgresReplica("postgresql://fake", driver=FakeDriver(), interval=999)
            replica.backup(store)
            replica.last_error = "OperationalError: connection refused"

            status, message, details = _persistence_check(SimpleNamespace(
                store=store, replica=replica, restored_from_replica=True))

            self.assertEqual(status, "error")
            self.assertIn("背景備份", message)
            self.assertIn("connection refused", details["error"])
        finally:
            store.close()

    def test_health_check_reports_a_broken_connection(self):
        from types import SimpleNamespace

        from app.health import _persistence_check

        store = make_store(self.root, "broken.db")
        try:

            class BrokenDriver:
                def connect(self, _dsn, autocommit=True):
                    raise OSError("connection refused")

            broken = SimpleNamespace(
                store=store,
                replica=PostgresReplica("postgresql://fake", driver=BrokenDriver(), interval=999),
                restored_from_replica=False,
            )
            status, _message, details = _persistence_check(broken)
            self.assertEqual(status, "error")
            self.assertIn("OSError", details["error"])
        finally:
            store.close()

    def test_disabled_without_connection_string(self):
        replica = PostgresReplica("", driver=None)
        self.assertFalse(replica.configured)
        self.assertFalse(replica.enabled)

    def test_connection_string_composed_from_zeabur_variables(self):
        with patch.dict("os.environ", {
            "DATABASE_URL": "",
            "POSTGRES_CONNECTION_STRING": "",
            "POSTGRES_HOST": "db.internal",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USERNAME": "lure",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DATABASE": "brain",
        }, clear=False):
            self.assertEqual(connection_string(), "postgresql://lure:secret@db.internal:5433/brain")

    def test_connection_string_prefers_full_url(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://a:b@c:5432/d"}, clear=False):
            self.assertEqual(connection_string(), "postgresql://a:b@c:5432/d")


if __name__ == "__main__":
    unittest.main()
