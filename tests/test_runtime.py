import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.server import AppContext
from run import admin_token_for_host, default_paths, default_port, load_profile, load_settings

from tests.test_ingest import approved_chunk


class RuntimeTests(unittest.TestCase):
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
            internal = approved_chunk(
                access_level="internal_coaching",
                customer_service_allowed=False,
                rag_allowed=True,
            )
            source.write_text(json.dumps(internal, ensure_ascii=False), encoding="utf-8")
            first = AppContext.create(
                database, source, static, "token", access_level="internal_coaching"
            )
            first.close()

            with self.assertRaises(ValueError):
                AppContext.create(
                    database, source, static, "token", access_level="customer_service"
                )


if __name__ == "__main__":
    unittest.main()
