import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage import KnowledgeStore


class AuditRetentionTests(unittest.TestCase):
    def test_add_audit_sweeps_entries_older_than_retention(self):
        """稽核只進不出的話，Postgres 快照每 120 秒都要把整張表讀出來上傳，
        幾個月後就是幾十萬列的鎖競爭。月預算看當月、後台指標看 30 天，
        過了保留期的稽核（連同指向它們的回饋）要被順手清掉。"""
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "knowledge.db")
            try:
                stale = (
                    datetime.now(timezone.utc)
                    - timedelta(days=store.AUDIT_RETENTION_DAYS + 1)
                ).isoformat()
                store.add_audit({
                    "trace_id": "old-trace", "created_at": stale,
                    "question": "舊問題", "status": "answered", "user_id": 7,
                })
                store.add_feedback("old-trace", 7, "up", stale)
                self.assertTrue(store.audit_belongs_to("old-trace", 7))

                store._AUDIT_SWEEP_EVERY = 1
                fresh = datetime.now(timezone.utc).isoformat()
                store.add_audit({
                    "trace_id": "new-trace", "created_at": fresh,
                    "question": "新問題", "status": "answered", "user_id": 7,
                })

                self.assertFalse(store.audit_belongs_to("old-trace", 7))
                self.assertTrue(store.audit_belongs_to("new-trace", 7))
                self.assertEqual(store.list_feedback(), [])
            finally:
                store.close()


class StorageHealthTests(unittest.TestCase):
    def test_health_check_does_not_abort_an_active_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "knowledge.db")
            try:
                for index in range(3):
                    store.connection.execute(
                        """
                        INSERT INTO audits (
                            trace_id, created_at, question, status, chunk_ids_json
                        ) VALUES (?, '', '', 'answered', '[]')
                        """,
                        (f"trace-{index}",),
                    )
                store.connection.commit()
                cursor = store.connection.execute("SELECT * FROM audits ORDER BY id")
                cursor.fetchone()

                store.health_check()

                try:
                    remaining = cursor.fetchall()
                except sqlite3.OperationalError as exc:
                    self.fail(f"health check aborted the active reader: {exc}")
                self.assertEqual(len(remaining), 2)
            finally:
                store.close()

    def test_health_check_does_not_wait_for_database_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "knowledge.db")
            writer = sqlite3.connect(store.db_path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                started = time.perf_counter()

                try:
                    details = store.health_check()
                except sqlite3.OperationalError as exc:
                    self.fail(f"health check waited on the active writer: {exc}")

                self.assertLess(time.perf_counter() - started, 0.5)
                self.assertEqual(details["integrity"], "ok")
                self.assertTrue(details["writable"])
            finally:
                writer.rollback()
                writer.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
