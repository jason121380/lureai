import http.client
import threading
import time
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from tests.test_api import ServerTestCase
from types import SimpleNamespace

from app.server import AppContext
from run import admin_token_for_host, default_paths, default_port, load_profile, load_settings

from tests.test_ingest import approved_chunk


class WriterFenceTests(ServerTestCase):
    def test_idle_keepalive_connection_does_not_block_shutdown(self):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        client.request("GET", "/api/health")
        client.getresponse().read()
        self.server.shutdown()
        closed = threading.Event()
        worker = threading.Thread(target=lambda: (self.server.server_close(), closed.set()))
        worker.start()
        promptly_closed = closed.wait(0.5)
        client.close()
        worker.join(3)
        self.assertTrue(promptly_closed, "idle keepalive pinned server_close")

    def test_active_worker_has_finite_drain_grace(self):
        entered, release = threading.Event(), threading.Event()
        original = self.context.auth.login
        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)
        self.context.auth.login = blocked
        request = threading.Thread(target=lambda: self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password"}))
        request.start()
        self.assertTrue(entered.wait(2))
        self.server.shutdown()
        self.server.drain_timeout = 0.05
        errors, done = [], threading.Event()
        def close():
            try:
                self.server.server_close()
            except TimeoutError:
                errors.append("timeout")
            finally:
                done.set()
        closer = threading.Thread(target=close)
        closer.start()
        bounded = done.wait(0.3)
        release.set()
        request.join(3)
        closer.join(3)
        self.assertTrue(bounded, "active handler drain was unbounded")
        self.assertEqual(errors, ["timeout"])

    def test_unqualified_replica_rejects_login_before_session_creation(self):
        self.context.replica = SimpleNamespace(configured=True, writable=False)
        status, _ = self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password"})
        self.assertEqual(status, 503)
        self.assertEqual(self.context.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)


class RuntimeTests(unittest.TestCase):
    def test_drain_timeout_exits_without_final_backup_or_closing_live_store(self):
        import signal
        from run import main
        context = Mock()
        replica = Mock(configured=False, enabled=False)
        server = Mock()
        server.server_close.side_effect = TimeoutError("workers busy")
        old = signal.getsignal(signal.SIGTERM)
        try:
            with patch("run.AppContext.create", return_value=context), patch(
                    "run.PostgresReplica.from_env", return_value=replica), patch(
                    "run.create_server", return_value=server), patch("run.os._exit", side_effect=SystemExit(1)) as exit_process:
                with self.assertRaises(SystemExit):
                    main([])
            exit_process.assert_called_once_with(1)
            replica.stop.assert_not_called()
            context.close.assert_not_called()
        finally:
            signal.signal(signal.SIGTERM, old)

    def test_sigterm_drains_server_before_final_backup_and_close(self):
        import signal
        from run import main
        events = []
        context = Mock()
        replica = Mock(configured=False, enabled=False)
        replica.stop.side_effect = lambda *_: events.append("backup")
        context.close.side_effect = lambda: events.append("close")
        server = Mock()
        server.server_close.side_effect = lambda: events.append("drain")
        server.serve_forever.side_effect = lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        old = signal.getsignal(signal.SIGTERM)
        with patch("run.AppContext.create", return_value=context), patch(
                "run.PostgresReplica.from_env", return_value=replica), patch("run.create_server", return_value=server):
            self.assertEqual(main([]), 0)
        self.assertEqual(events, ["drain", "backup", "close"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), old)

    def test_restore_failure_never_starts_http_or_backup(self):
        from run import main
        context = Mock()
        replica = Mock(configured=True, enabled=True)
        replica.restore.side_effect = RuntimeError("restore failed")
        with patch("run.AppContext.create", return_value=context), patch(
                "run.PostgresReplica.from_env", return_value=replica), patch("run.create_server") as server:
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                main([])
        server.assert_not_called()
        replica.start.assert_not_called()
        context.close.assert_called_once()

    def test_public_host_without_a_token_falls_back_to_a_random_one(self):
        # 少一個環境變數不可以讓整站打不開；後台走帳號登入，
        # header 權杖變成沒人知道的隨機值等於關掉那條路。
        with patch.dict(os.environ, {}, clear=True):
            first = admin_token_for_host("0.0.0.0")
            second = admin_token_for_host("0.0.0.0")

        self.assertGreaterEqual(len(first), 32)
        self.assertNotEqual(first, "local-admin")
        self.assertNotEqual(first, second)

    def test_configured_admin_token_wins(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "from-env"}, clear=True):
            self.assertEqual(admin_token_for_host("0.0.0.0"), "from-env")

    def test_local_host_can_use_development_admin_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(admin_token_for_host("127.0.0.1"), "local-admin")

    def test_default_paths_use_bundled_coaching_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            paths = default_paths(root)

            self.assertEqual(paths["knowledge"], root / "knowledge/designer_coaching_process.jsonl")
            self.assertEqual(paths["database"], root / "data/designer_coach.db")

    def test_default_paths_prefer_private_full_index_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_index = root / "private_sources/full/rag/designer_coach_full.jsonl"
            private_index.parent.mkdir(parents=True)
            private_index.write_text("", encoding="utf-8")

            paths = default_paths(root)

            self.assertEqual(paths["knowledge"], private_index)

    def test_customer_profile_is_removed(self):
        with self.assertRaisesRegex(ValueError, "未知的知識 profile"):
            load_profile("customer_service")

    def test_environment_can_override_knowledge_path(self):
        with patch.dict(os.environ, {"KNOWLEDGE_JSONL": "/tmp/custom.jsonl"}):
            paths = default_paths(Path("/tmp/project"))

        self.assertEqual(paths["knowledge"], Path("/tmp/custom.jsonl"))

    def test_designer_coach_uses_isolated_knowledge_and_database(self):
        paths = default_paths(Path("/tmp/project"), profile="designer_coach")

        self.assertEqual(paths["knowledge"], Path("/tmp/project/knowledge/designer_coaching_process.jsonl"))
        self.assertEqual(paths["database"], Path("/tmp/project/data/designer_coach.db"))

    def test_designer_coach_profile_is_internal(self):
        profile = load_profile("designer_coach")

        self.assertEqual(profile["access_level"], "internal_coaching")
        self.assertEqual(profile["assistant_name"], "AI 輔導教練")

    def test_zeabur_port_is_used_when_app_port_is_not_set(self):
        with patch.dict(os.environ, {"PORT": "4321"}, clear=True):
            self.assertEqual(default_port(), 4321)

    def test_app_port_overrides_platform_port(self):
        with patch.dict(os.environ, {"APP_PORT": "8766", "PORT": "4321"}, clear=True):
            self.assertEqual(default_port(), 8766)

    def test_settings_file_loads_retrieval_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"retrieval":{"minimum_score":0.8,"top_k":4}}')

            settings = load_settings(path)

        self.assertEqual(settings["retrieval"]["minimum_score"], 0.8)
        self.assertEqual(settings["retrieval"]["top_k"], 4)

    def test_context_reindexes_database_when_bundled_knowledge_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "knowledge.jsonl"
            database = root / "knowledge.db"
            static = root / "static"
            static.mkdir()
            source.write_text(json.dumps(approved_chunk(), ensure_ascii=False), encoding="utf-8")
            first = AppContext.create(database, source, static, "token")
            first.close()
            replacement = approved_chunk(chunk_id="replacement", text="新的部署知識")
            source.write_text(json.dumps(replacement, ensure_ascii=False), encoding="utf-8")

            second = AppContext.create(database, source, static, "token")
            try:
                self.assertEqual(second.store.count_chunks(), 1)
                self.assertIsNotNone(second.store.get_chunk("replacement"))
                self.assertIsNone(second.store.get_chunk("chunk-1"))
            finally:
                second.close()

    def test_context_fails_closed_when_database_access_level_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "knowledge.jsonl"
            database = root / "knowledge.db"
            static = root / "static"
            static.mkdir()
            internal = approved_chunk()
            source.write_text(json.dumps(internal, ensure_ascii=False), encoding="utf-8")
            first = AppContext.create(
                database, source, static, "token", access_level="internal_coaching"
            )
            first.close()

            with self.assertRaises(ValueError):
                AppContext.create(
                    database, source, static, "token", access_level="restricted"
                )


if __name__ == "__main__":
    unittest.main()
