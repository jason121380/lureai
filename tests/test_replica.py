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
            status, _message, details = _persistence_check(connected)
            self.assertEqual(status, "ok")
            self.assertEqual(details["storage"], "postgres")
            self.assertTrue(details["restored_on_boot"])
            # 檢查時真的寫了一次，最後備份時間要有值。
            self.assertTrue(details["last_backup_at"])

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
