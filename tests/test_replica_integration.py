"""Run with TEST_DATABASE_URL pointing to a disposable PostgreSQL database."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from app.replica import PostgresReplica
from tests.test_replica import make_store


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "dedicated TEST_DATABASE_URL not set")
class PostgresIntegrationTests(unittest.TestCase):
    def test_real_session_lock_history_and_lost_connection_fence(self):
        import psycopg

        dsn = os.environ["TEST_DATABASE_URL"]
        schema = "replica_test_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA "{schema}"')
            class Driver:
                @staticmethod
                def connect(dsn, **kwargs):
                    kwargs["options"] += f" -c search_path={schema}"
                    return psycopg.connect(dsn, **kwargs)
            first = PostgresReplica(dsn, driver=Driver())
            second = PostgresReplica(dsn, driver=Driver())
            try:
                with tempfile.TemporaryDirectory() as directory:
                    store = make_store(Path(directory), "source.db")
                    try:
                        self.assertFalse(first.restore(store))
                        first.backup(store)
                        with self.assertRaises(RuntimeError):
                            second.restore(store)
                        store.add_feedback("trace", None, "up", "now")
                        first.stop(store)
                        self.assertTrue(second.restore(store))
                        count = second._writer.execute("SELECT count(*) FROM lureai_snapshot_history").fetchone()[0]
                        self.assertEqual(count, 2)
                        second._writer.close()
                        with self.assertRaises(Exception):
                            second.backup(store)
                        self.assertFalse(second.writable)
                        with self.assertRaises(RuntimeError):
                            second.backup(store)
                    finally:
                        store.close()
            finally:
                first.stop()
                second.stop()
                admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
