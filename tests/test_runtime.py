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
    def test_public_host_requires_explicit_admin_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "ADMIN_TOKEN"):
                admin_token_for_host("0.0.0.0")

    def test_local_host_can_use_development_admin_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(admin_token_for_host("127.0.0.1"), "local-admin")

    def test_default_paths_prefer_bundled_customer_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "knowledge/active_customer_service.jsonl"
            bundled.parent.mkdir()
            bundled.write_text("", encoding="utf-8")

            paths = default_paths(root)

            self.assertEqual(paths["knowledge"], bundled)
            self.assertEqual(paths["database"], root / "data/knowledge.db")

    def test_default_paths_prefer_private_full_index_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_index = root / "private_sources/full/rag/customer_service_full.jsonl"
            private_index.parent.mkdir(parents=True)
            private_index.write_text("", encoding="utf-8")
            bundled = root / "knowledge/active_customer_service.jsonl"
            bundled.parent.mkdir()
            bundled.write_text("", encoding="utf-8")

            paths = default_paths(root)

            self.assertEqual(paths["knowledge"], private_index)

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
