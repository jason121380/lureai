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


class LegacyColumnMigrationTests(unittest.TestCase):
    def test_reopening_drops_the_legacy_customer_service_column(self):
        """客服版（2026-08-31 移除）留下的 chunks.customer_service_allowed
        沒有任何讀取者。既有資料庫在下一次開機時要被砍掉，而且砍完照常寫入。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.db"
            store = KnowledgeStore(path)
            store.connection.execute(
                "ALTER TABLE chunks ADD COLUMN customer_service_allowed INTEGER NOT NULL DEFAULT 0"
            )
            store.connection.commit()
            store.close()

            reopened = KnowledgeStore(path)
            try:
                self.assertFalse(reopened._has_column("chunks", "customer_service_allowed"))
                reopened.upsert_custom_chunk({
                    "chunk_id": "admin:migrated", "locator": "migrated",
                    "section_title": "搬完還能寫", "text": "欄位砍掉之後照常寫入。",
                    "title": "後台新增知識", "source_file": "knowledge/admin_authored.md",
                    "access_level": "internal_coaching", "review_status": "approved",
                    "search_text": "搬完 還能 寫",
                })
                self.assertIsNotNone(reopened.get_chunk("admin:migrated"))
            finally:
                reopened.close()


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


class ConversationVersionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.directory.name) / "conversation.db")
        from app.auth import AuthManager
        self.owner = AuthManager(self.store).create_or_reset_user("owner", "password-owner-for-tests")["id"]
        self.other = AuthManager(self.store).create_or_reset_user("other", "password-other-for-tests")["id"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def save(self, text="first", rev=1, expected_rev=0, owner=None, ident="c"):
        return self.store.save_conversation(owner or self.owner, ident, "title", "expert",
                                            [{"content": text}], "created", "updated", rev, expected_rev)

    def test_idempotent_retry_and_compare_and_swap(self):
        self.assertEqual(self.save()["status"], "accepted")
        self.assertEqual(self.save()["status"], "accepted")
        self.assertEqual(self.save("different")["status"], "conflict")
        self.assertEqual(self.save("legacy overwrite", 9, None)["status"], "conflict")
        self.assertEqual(self.save("second", 2, 1)["status"], "accepted")
        self.assertEqual(self.save("stale base", 99, 1)["status"], "conflict")
        self.assertEqual(self.store.list_conversations(self.owner)[0]["messages"][0]["content"], "second")

    def test_owner_boundaries_cover_deletion_and_tombstones(self):
        self.save()
        self.assertEqual(self.save(owner=self.other)["status"], "conflict")
        self.assertEqual(self.store.delete_conversation(self.other, "c")["status"], "conflict")
        self.store.delete_conversation(self.owner, "c")
        self.assertEqual(self.store.list_conversation_tombstones(self.other), [])
        self.assertEqual(self.save(owner=self.other)["status"], "conflict")
        self.assertEqual(self.save(rev=99)["status"], "deleted")

    def test_tombstones_survive_reopen_and_pruning(self):
        self.save(ident="c")
        self.save(ident="d")
        self.store.prune_conversations(self.owner, keep=0)
        self.store.close()
        self.store = KnowledgeStore(Path(self.directory.name) / "conversation.db")
        self.assertEqual({x["id"] for x in self.store.list_conversation_tombstones(self.owner)}, {"c", "d"})
        self.assertEqual(self.save(rev=99)["status"], "deleted")


class QualityScoreMetricsTests(unittest.TestCase):
    def test_reply_metrics_averages_only_scored_replies(self):
        """平均品質分數的分母是「有打分的回答」：閒聊與轉人工存 NULL，
        混進分母的話一天十句「哈囉」就能把平均沖到看不出真正的品質變化。"""
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "knowledge.db")
            try:
                now = datetime.now(timezone.utc).isoformat()
                store.add_audit({
                    "trace_id": "scored-100", "created_at": now,
                    "question": "怎麼漲價", "status": "answered", "quality_score": 100,
                })
                store.add_audit({
                    "trace_id": "scored-60", "created_at": now,
                    "question": "廣告怎麼投", "status": "answered", "quality_score": 60,
                })
                store.add_audit({
                    "trace_id": "smalltalk", "created_at": now,
                    "question": "哈囉", "status": "answered",
                })
                metrics = store.reply_metrics(since="1970-01-01T00:00:00")
                self.assertEqual(metrics["replies"], 3)
                self.assertEqual(metrics["scored"], 2)
                self.assertEqual(metrics["avg_quality_score"], 80.0)
            finally:
                store.close()

    def test_reply_metrics_with_no_scored_replies_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "knowledge.db")
            try:
                metrics = store.reply_metrics(since="1970-01-01T00:00:00")
                self.assertEqual(metrics["scored"], 0)
                self.assertIsNone(metrics["avg_quality_score"])
            finally:
                store.close()
