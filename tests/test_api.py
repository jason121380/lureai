import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
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
            "logo.svg": '<svg aria-label="lure ai"></svg>',
            "manifest.webmanifest": '{"name":"lure ai"}',
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
        self.context.auth.create_or_reset_user("designer", "designer-password")
        self.server = create_server("127.0.0.1", 0, self.context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

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
            with self.client.open(request, timeout=3) as response:
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
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        status, body = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["citations"][0]["locator"], "aftercare-1")

    def test_chat_requires_login(self):
        status, body = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "authentication_required")

    def test_login_cookie_unlocks_current_user_and_logout(self):
        status, body = self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["username"], "designer")
        self.assertEqual(self.request("GET", "/api/auth/me")[1]["user"]["username"], "designer")

        self.assertEqual(self.request("POST", "/api/auth/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/auth/me")[0], 401)

    def test_public_host_login_cookie_is_secure(self):
        payload = json.dumps({
            "username": "designer", "password": "designer-password",
        }).encode()
        request = urllib.request.Request(
            self.base + "/api/auth/login",
            data=payload,
            headers={"Content-Type": "application/json", "Host": "hairbrain.zeabur.app"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=3) as response:
            cookie = response.headers.get("Set-Cookie", "")

        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)

    def test_login_is_rate_limited_per_account_and_client(self):
        for _ in range(5):
            status, _body = self.request("POST", "/api/auth/login", {
                "username": "unknown-user", "password": "wrong-password",
            })
            self.assertEqual(status, 401)

        status, body = self.request("POST", "/api/auth/login", {
            "username": "unknown-user", "password": "wrong-password",
        })

        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "too_many_attempts")

    def test_admin_can_create_or_reset_user(self):
        status, body = self.request("POST", "/api/admin/users", {
            "username": "new-designer", "password": "strong-password",
        }, token="secret-token")

        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["username"], "new-designer")
        self.assertNotIn("password", body["user"])
        status, users = self.request("GET", "/api/admin/users", token="secret-token")
        self.assertEqual(status, 200)
        self.assertIn("new-designer", [item["username"] for item in users["items"]])

    def test_usage_is_private_to_authenticated_user(self):
        self.assertEqual(self.request("GET", "/api/usage")[0], 401)
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })

        with patch.object(
            self.context.service.answerer,
            "answer",
            return_value=("依照設計師示範方向吹整。[1]", "llm", "used", {
                "input_tokens": 1000, "output_tokens": 200,
            }),
        ):
            self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})

        status, body = self.request("GET", "/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(body["input_tokens"], 1000)
        self.assertEqual(body["output_tokens"], 200)
        self.assertGreater(body["spend_twd"], 0)
        self.assertIn("progress_percent", body)

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
        }), patch.object(
            self.context.service.answerer,
            "check_model_access",
            return_value={"reachable": True, "api": "responses"},
        ):
            status, body = self.request("GET", "/api/admin/health", token="secret-token")

        self.assertEqual(status, 200)
        self.assertIn(body["status"], ("ok", "warning"))
        self.assertIn("checked_at", body)
        self.assertEqual(body["summary"]["total"], 8)
        checks = {item["id"]: item for item in body["checks"]}
        self.assertEqual(set(checks), {
            "server", "api", "frontend", "database", "auth", "rag", "knowledge", "llm",
        })
        for item in checks.values():
            self.assertIn(item["status"], ("ok", "warning", "error"))
            self.assertIsInstance(item["latency_ms"], int)
            self.assertGreaterEqual(item["latency_ms"], 0)
            self.assertTrue(item["message"])
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertEqual(checks["rag"]["details"]["chunks"], 1)
        self.assertEqual(checks["knowledge"]["details"]["records"], 1)
        self.assertEqual(checks["frontend"]["details"]["assets"], 8)
        self.assertEqual(checks["auth"]["status"], "ok")
        self.assertEqual(checks["auth"]["details"]["users"], 1)
        self.assertTrue(checks["llm"]["details"]["reachable"])
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
