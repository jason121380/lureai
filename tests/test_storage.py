import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.storage import KnowledgeStore


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
