import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run import default_paths, default_port, load_profile, load_settings


class RuntimeTests(unittest.TestCase):
    def test_default_paths_prefer_bundled_customer_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "knowledge/active_customer_service.jsonl"
            bundled.parent.mkdir()
            bundled.write_text("", encoding="utf-8")

            paths = default_paths(root)

            self.assertEqual(paths["knowledge"], bundled)
            self.assertEqual(paths["database"], root / "data/knowledge.db")

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


if __name__ == "__main__":
    unittest.main()
