import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app.server import AppContext, create_server

from tests.test_ingest import approved_chunk


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        source = root / "knowledge.jsonl"
        source.write_text(json.dumps(approved_chunk(
            locator="aftercare-1",
            title="燙髮居家照護",
            text="燙髮後整理時，依照設計師示範方向吹整。",
        ), ensure_ascii=False), encoding="utf-8")
        self.source = source
        (root / "manifest.json").write_text(json.dumps({
            "source_files": 267,
            "markdown_files": 537,
            "conversation_cases": 270,
            "status_counts": {"protected": 15},
        }), encoding="utf-8")
        frontend_assets = {
            "index.html": '<textarea id="prompt"></textarea><script src="chat.js"></script>',
            "admin.html": '<div id="admin-shell"></div><script src="admin.js"></script>',
            "app.css": ".chat-main {} .admin-shell {}",
            "chat.js": 'fetch("/api/chat")',
            "admin.js": 'fetch("/api/admin/health")',
            "vendor/lucide.min.js": "const lucide = {};",
        }
        for relative, content in frontend_assets.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.context = AppContext.create(
            db_path=root / "knowledge.db",
            knowledge_path=source,
            static_dir=root,
            admin_token="secret-token",
        )
        self.server = create_server("127.0.0.1", 0, self.context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.context.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, token=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Admin-Token"] = token
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health_reports_indexed_chunks(self):
        status, body = self.request("GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["chunks"], 1)
        self.assertEqual(body["profile"], "customer_service")

    def test_chat_returns_grounded_answer(self):
        status, body = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["citations"][0]["locator"], "aftercare-1")

    def test_admin_endpoint_rejects_wrong_token(self):
        status, body = self.request("GET", "/api/admin/stats")

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_admin_stats_accepts_correct_token(self):
        status, body = self.request("GET", "/api/admin/stats", token="secret-token")

        self.assertEqual(status, 200)
        self.assertEqual(body["chunks"], 1)
        self.assertEqual(body["pipeline"]["source_files"], 267)
        self.assertEqual(body["pipeline"]["conversation_cases"], 270)

    def test_admin_health_rejects_missing_token(self):
        status, body = self.request("GET", "/api/admin/health")

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_admin_health_reports_all_components_without_secrets(self):
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://api.openai.com",
            "LLM_API_KEY": "unit-test-api-key",
            "LLM_MODEL": "test-model",
        }):
            status, body = self.request("GET", "/api/admin/health", token="secret-token")

        self.assertEqual(status, 200)
        self.assertIn(body["status"], ("ok", "warning"))
        self.assertIn("checked_at", body)
        self.assertEqual(body["summary"]["total"], 7)
        checks = {item["id"]: item for item in body["checks"]}
        self.assertEqual(set(checks), {
            "server", "api", "frontend", "database", "rag", "knowledge", "llm",
        })
        for item in checks.values():
            self.assertIn(item["status"], ("ok", "warning", "error"))
            self.assertIsInstance(item["latency_ms"], int)
            self.assertGreaterEqual(item["latency_ms"], 0)
            self.assertTrue(item["message"])
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertEqual(checks["rag"]["details"]["chunks"], 1)
        self.assertEqual(checks["knowledge"]["details"]["records"], 1)
        self.assertEqual(checks["frontend"]["details"]["assets"], 6)
        serialized = json.dumps(body)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("unit-test-api-key", serialized)
        self.assertNotIn(str(Path(self.temp.name)), serialized)

    def test_admin_health_detects_knowledge_source_and_index_drift(self):
        self.source.write_text(json.dumps(approved_chunk(
            locator="replacement-1",
            title="已更換的知識",
            text="來源檔已經更換，但資料庫尚未重新建立索引。",
        ), ensure_ascii=False), encoding="utf-8")

        status, body = self.request("GET", "/api/admin/health", token="secret-token")

        checks = {item["id"]: item for item in body["checks"]}
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "error")
        self.assertEqual(checks["knowledge"]["status"], "error")
        self.assertFalse(checks["knowledge"]["details"]["in_sync"])

    def test_admin_health_detects_broken_chat_service_chain(self):
        self.context.service.retriever = None

        status, body = self.request("GET", "/api/admin/health", token="secret-token")

        checks = {item["id"]: item for item in body["checks"]}
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "error")
        self.assertEqual(checks["api"]["status"], "error")
        self.assertIn("retrieval", checks["api"]["details"]["unavailable"])


if __name__ == "__main__":
    unittest.main()
