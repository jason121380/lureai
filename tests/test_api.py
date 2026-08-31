import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
        (root / "manifest.json").write_text(json.dumps({
            "source_files": 267,
            "markdown_files": 537,
            "conversation_cases": 270,
            "status_counts": {"protected": 15},
        }), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
