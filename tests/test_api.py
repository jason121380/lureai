import http.client
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


class ServerTestCase(unittest.TestCase):
    """起一台真的 HTTP server 的共用底座；其他測試檔可以直接繼承。"""

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
            "logo.png": b"\x89PNG\r\n\x1a\n" + b"logo",
            "favicon.png": b"\x89PNG\r\n\x1a\n" + b"icon",
            "manifest.webmanifest": '{"name":"LUREAI"}',
            "vendor/lucide.min.js": "const lucide = {};",
        }
        for relative, content in frontend_assets.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
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

    def fresh_client(self):
        """換一組 cookie＝模擬另一台裝置登入同一個帳號。"""
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(self, method, path, payload=None, token=None, extra_headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Admin-Token"] = token
        headers.update(extra_headers or {})
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.client.open(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())


class ApiTests(ServerTestCase):
    def test_health_reports_indexed_chunks(self):
        status, body = self.request("GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["chunks"], 1)
        self.assertEqual(body["profile"], "designer_coach")

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

    def test_login_limit_survives_a_forged_forwarded_for(self):
        """每次換一個 X-Forwarded-For 就換到一把新鑰匙，等於沒有上限。

        雲端一律是內網 proxy 連進來，所以那個標頭是採信的；擋得住這件事的是
        「只看帳號」那把鑰匙——它偽造不掉。
        """
        for index in range(5):
            status, _body = self.request(
                "POST", "/api/auth/login",
                {"username": "unknown-user", "password": "wrong-password"},
                extra_headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            self.assertEqual(status, 401)

        status, body = self.request(
            "POST", "/api/auth/login",
            {"username": "unknown-user", "password": "wrong-password"},
            extra_headers={"X-Forwarded-For": "203.0.113.99"},
        )

        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "too_many_attempts")

    def test_web_chat_never_runs_the_line_tone(self):
        """`line` 是寫給 LINE 出口的：沒有標點、要拆成多則、引用在出口才剝掉。

        網頁照單全收的話畫面會出現一段沒有標點的文字，而且那條路還跳過了引用
        守門——同一個 tone 在兩條路上的出口行為並不一樣。
        """
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        status, body = self.request("POST", "/api/chat", {
            "message": "燙髮後怎麼整理？", "tone": "line",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["tone"], "expert")

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

    def test_feedback_is_stored_and_visible_to_admin(self):
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        _status, chat = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})

        status, body = self.request("POST", "/api/feedback", {
            "trace_id": chat["trace_id"], "rating": "down",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        # 重按改評分：同一人同一則回答只留一票。
        self.request("POST", "/api/feedback", {"trace_id": chat["trace_id"], "rating": "up"})

        status, listing = self.request("GET", "/api/admin/feedback", token="secret-token")
        self.assertEqual(status, 200)
        rows = [row for row in listing["items"] if row["trace_id"] == chat["trace_id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rating"], "up")
        self.assertIn("question", rows[0])

    def test_feedback_rejects_bad_rating_and_requires_login(self):
        self.assertEqual(self.request("POST", "/api/feedback", {"trace_id": "t", "rating": "up"})[0], 401)
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        status, _body = self.request("POST", "/api/feedback", {"trace_id": "t", "rating": "meh"})
        self.assertEqual(status, 400)

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
        self.assertEqual(body["summary"]["total"], 9)
        checks = {item["id"]: item for item in body["checks"]}
        self.assertEqual(set(checks), {
            "server", "api", "frontend", "database", "persistence",
            "auth", "rag", "knowledge", "llm",
        })
        for item in checks.values():
            self.assertIn(item["status"], ("ok", "warning", "error"))
            self.assertIsInstance(item["latency_ms"], int)
            self.assertGreaterEqual(item["latency_ms"], 0)
            self.assertTrue(item["message"])
        self.assertEqual(checks["database"]["status"], "ok")
        # 沒設定 Postgres 時要警告「重新部署會歸零」，而不是靜靜通過。
        self.assertEqual(checks["persistence"]["status"], "warning")
        self.assertEqual(checks["persistence"]["details"]["storage"], "sqlite-only")
        self.assertEqual(checks["rag"]["details"]["chunks"], 1)
        self.assertEqual(checks["knowledge"]["details"]["records"], 1)
        self.assertEqual(checks["frontend"]["details"]["assets"], 9)
        self.assertEqual(checks["auth"]["status"], "ok")
        self.assertEqual(checks["auth"]["details"]["users"], 1)
        self.assertTrue(checks["llm"]["details"]["reachable"])
        serialized = json.dumps(body)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("unit-test-api-key", serialized)
        self.assertNotIn(str(Path(self.temp.name)), serialized)

    def test_admin_role_session_grants_admin_access(self):
        self.context.auth.create_or_reset_user("boss", "1234", role="admin")
        self.request("POST", "/api/auth/login", {"username": "boss", "password": "1234"})

        status, body = self.request("GET", "/api/admin/stats")
        self.assertEqual(status, 200)
        self.assertEqual(body["chunks"], 1)

        status, body = self.request("POST", "/api/admin/users", {
            "username": "front-desk", "password": "9999", "role": "user",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "user")

    def test_regular_user_session_cannot_access_admin(self):
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })

        status, body = self.request("GET", "/api/admin/stats")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_cross_origin_post_is_rejected(self):
        payload = json.dumps({
            "username": "designer", "password": "designer-password",
        }).encode()
        request = urllib.request.Request(
            self.base + "/api/auth/login",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example.com",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        self.assertEqual(status, 403)

    def test_chat_is_rate_limited_per_user(self):
        from app.auth import RequestRateLimiter

        self.context.chat_limiter = RequestRateLimiter(max_requests=2, window_seconds=60)
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        for _ in range(2):
            status, _body = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})
            self.assertEqual(status, 200)

        status, body = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})
        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "rate_limited")

    def test_chat_over_budget_disables_model_generation(self):
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        over_budget = {
            "input_tokens": 1, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 1,
            "spend_twd": 999999.0,
        }
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://api.openai.com",
            "LLM_API_KEY": "unit-test-api-key",
            "LLM_MODEL": "test-model",
        }), patch(
            "urllib.request.urlopen", side_effect=AssertionError("model must not be called")
        ), patch.object(self.context.store, "usage_totals", return_value=over_budget):
            status, body = self.request("POST", "/api/chat", {"message": "燙髮後怎麼整理？"})

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["answer_mode"], "extractive")
        self.assertEqual(body["model_status"], "budget_exhausted")

    def test_admin_can_author_edit_and_delete_knowledge(self):
        status, body = self.request("POST", "/api/admin/knowledge", {
            "section_title": "新客第一次回流的追蹤節奏",
            "category": "售後與回流",
            "text": "服務後第三天主動關心整理狀況，不推銷；第六週再給明確的下一步。",
        }, token="secret-token")

        self.assertEqual(status, 200)
        chunk_id = body["chunk"]["chunk_id"]
        self.assertTrue(chunk_id.startswith("admin:"))

        # The new knowledge is immediately retrievable.
        hits = self.context.retriever.retrieve("新客回流的追蹤節奏", limit=5)
        self.assertIn(chunk_id, [hit.chunk_id for hit in hits])

        # Editing keeps the same id.
        status, body = self.request("POST", "/api/admin/knowledge", {
            "chunk_id": chunk_id,
            "section_title": "新客回流節奏（修訂）",
            "text": "服務後第三天關心整理狀況，第六週給下一步建議與可預約時段。",
        }, token="secret-token")
        self.assertEqual(status, 200)
        self.assertEqual(body["chunk"]["chunk_id"], chunk_id)
        self.assertEqual(self.context.store.get_chunk(chunk_id)["section_title"], "新客回流節奏（修訂）")

        status, _ = self.request("POST", "/api/admin/knowledge/delete", {"chunk_id": chunk_id}, token="secret-token")
        self.assertEqual(status, 200)
        self.assertIsNone(self.context.store.get_chunk(chunk_id))

    def test_reindex_keeps_admin_authored_knowledge(self):
        status, body = self.request("POST", "/api/admin/knowledge", {
            "section_title": "後台知識應保留",
            "text": "重建索引時，後台新增的知識不應該被匯入檔覆蓋掉。",
        }, token="secret-token")
        chunk_id = body["chunk"]["chunk_id"]

        status, _ = self.request("POST", "/api/admin/reindex", {}, token="secret-token")

        self.assertEqual(status, 200)
        self.assertIsNotNone(self.context.store.get_chunk(chunk_id))
        self.assertIsNotNone(self.context.store.get_chunk("chunk-1"))

    def test_admin_chunks_can_be_filtered_by_domain(self):
        self.request("POST", "/api/admin/knowledge", {
            "section_title": "私訊回覆節奏",
            "category": "私訊流程",
            "domain": "coaching",
            "text": "顧客私訊後一小時內先回一句，再問清楚想要的效果。",
        }, token="secret-token")

        status, body = self.request("GET", "/api/admin/chunks?domain=coaching", token="secret-token")
        self.assertEqual(status, 200)
        self.assertTrue(body["items"])
        self.assertTrue(all(item["domain"] == "coaching" for item in body["items"]))

        status, body = self.request("GET", "/api/admin/chunks?domain=operations", token="secret-token")
        self.assertEqual(status, 200)
        self.assertTrue(all(item["domain"] == "operations" for item in body["items"]))

    def test_admin_stats_split_knowledge_into_the_two_domains(self):
        status, body = self.request("GET", "/api/admin/stats", token="secret-token")

        self.assertEqual(status, 200)
        domains = body["composition"]["domains"]
        self.assertEqual(
            [item["label"] for item in domains],
            ["店務營運管理", "設計師一對一行銷輔導"],
        )
        self.assertEqual(sum(item["count"] for item in domains), body["chunks"])

    def test_admin_knowledge_rejects_editing_imported_chunks(self):
        status, body = self.request("POST", "/api/admin/knowledge", {
            "chunk_id": "chunk-1", "section_title": "覆寫匯入知識", "text": "不應該被允許。",
        }, token="secret-token")

        self.assertEqual(status, 400)
        self.assertIn("後台建立", body["message"])

    def test_admin_quality_report_flags_unreadable_chunks(self):
        status, body = self.request("GET", "/api/admin/knowledge/quality", token="secret-token")

        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        self.assertIn("counts", body)
        self.assertIn("fragment", body["labels"])

    def test_removed_admin_endpoints_are_gone(self):
        self.assertEqual(self.request("POST", "/api/admin/retrieve", {"message": "x"}, token="secret-token")[0], 404)
        self.assertEqual(self.request("GET", "/api/admin/audits", token="secret-token")[0], 404)

    def test_chat_stream_returns_ndjson_result(self):
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })
        payload = json.dumps({"message": "燙髮後怎麼整理？"}).encode()
        request = urllib.request.Request(
            self.base + "/api/chat/stream",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.client.open(request, timeout=5) as response:
            self.assertIn("application/x-ndjson", response.headers.get("Content-Type", ""))
            lines = [line for line in response.read().decode("utf-8").splitlines() if line.strip()]

        events = [json.loads(line) for line in lines]
        result = events[-1]
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["citations"][0]["locator"], "aftercare-1")

    def test_chat_title_requires_login(self):
        status, body = self.request("POST", "/api/chat/title", {"message": "燙髮", "answer": "x"})

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "authentication_required")

    def test_chat_title_falls_back_to_question_without_model(self):
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })

        question = "燙髮後怎麼整理比較不會亂翹呢？我每天都很困擾"
        status, body = self.request("POST", "/api/chat/title", {
            "message": question,
            "answer": "依照設計師示範方向吹整。",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["title"], question[:20])
        self.assertEqual(body["model_status"], "not_configured")
        audits = self.context.store.list_audits(5)
        self.assertIn("title", [item["status"] for item in audits])

    def test_clean_admin_route_serves_admin_page(self):
        with self.client.open(self.base + "/admin", timeout=3) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("admin-shell", body)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))
            self.assertIn("nosniff", response.headers.get("X-Content-Type-Options", ""))
            self.assertTrue(response.headers.get("Content-Security-Policy"))

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


class ConnectionTests(ServerTestCase):
    """靜態檔要能共用同一條連線。

    預設的 HTTP/1.0 會讓每一個檔（CSS、JS、logo、icon）各開一條 TCP 連線，
    在雲端還要各做一次 TLS 交握；再加上 backlog 只有 5，同時湧進來就有連線
    被作業系統丟掉——症狀是「HTML 出來了但 CSS 沒有、分頁一直轉」。
    """

    def test_responses_are_http_1_1(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/app.css")
        response = connection.getresponse()
        response.read()

        self.assertEqual(response.version, 11)
        connection.close()

    def test_one_connection_serves_several_files(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        for path in ("/app.css", "/app.css", "/app.css"):
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            # 沒讀完就換下一個請求會炸；讀得完代表 Content-Length 是對的。
            self.assertTrue(response.read())
            self.assertFalse(response.will_close, "連線被關掉了，keep-alive 沒生效")
        connection.close()

    def test_the_socket_backlog_is_not_the_default_five(self):
        # 一頁的靜態檔加上其他分頁很容易超過 5。
        self.assertGreaterEqual(type(self.server).request_queue_size, 64)

    def test_streaming_still_ends_by_itself(self):
        """串流沒有 Content-Length，靠關連線收尾。

        改成 HTTP/1.1 之後如果忘了送 Connection: close，瀏覽器會一直等下一則
        訊息，畫面就是回答出來了但游標一直轉。
        """
        self.request("POST", "/api/auth/login", {"username": "designer", "password": "designer-password"})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        jar = next(h.cookiejar for h in self.client.handlers if hasattr(h, "cookiejar"))
        cookie = "; ".join(f"{item.name}={item.value}" for item in jar)
        connection.request(
            "POST", "/api/chat/stream",
            body=json.dumps({"message": "顧客不滿意怎麼處理？"}).encode(),
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Connection"), "close")
        # read() 讀到 EOF 才會回來；沒收尾的話這裡會卡到 timeout。
        self.assertTrue(response.read())
        connection.close()


class KnowledgeUploadTests(ServerTestCase):
    """後台「新增知識」改成拖檔上傳，這條是分析用的端點。"""

    DOC = (
        "# 客訴當下的處理原則\n\n"
        "客人反映顏色不對時，先確認是光線問題還是真的沒到位。不要當場說「其實這樣很好看」，"
        "那會讓客人覺得你在唬他。先承認他看到的事實，再給技術面的說明。\n\n"
        "## 什麼時候可以安排修補\n\n"
        "頭髮的狀況允許再上一次色才排修補。一週內連續兩次漂染會讓髮尾斷裂，寧可等一週，"
        "也不要當場硬做。等待期間給客人一支溫和的洗髮精。\n"
    )

    def test_a_document_is_analysed_into_chunks(self):
        status, body = self.request(
            "POST", "/api/admin/knowledge/analyze",
            {"name": "客訴手冊.md", "text": self.DOC}, token="secret-token",
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "rules")  # 測試環境沒有模型
        self.assertTrue(body["items"])
        for item in body["items"]:
            self.assertTrue(item["section_title"])
            self.assertTrue(item["text"])

    def test_an_empty_file_is_rejected_with_a_readable_message(self):
        status, body = self.request(
            "POST", "/api/admin/knowledge/analyze",
            {"name": "空的.txt", "text": "   "}, token="secret-token",
        )

        self.assertEqual(status, 400)
        self.assertIn("讀不到文字", body["message"])

    def test_an_oversized_file_is_rejected_before_the_model_runs(self):
        status, body = self.request(
            "POST", "/api/admin/knowledge/analyze",
            {"name": "很大.txt", "text": "字" * 60001}, token="secret-token",
        )

        self.assertEqual(status, 400)
        self.assertIn("6", body["message"])

    def test_analysis_needs_an_admin(self):
        status, _body = self.request(
            "POST", "/api/admin/knowledge/analyze",
            {"name": "a.md", "text": self.DOC},
        )

        self.assertEqual(status, 401)

    def test_the_analysed_chunks_can_be_saved_as_they_are(self):
        """分析出來的東西要能直接存進去——欄位對不上就白做了。"""
        _status, body = self.request(
            "POST", "/api/admin/knowledge/analyze",
            {"name": "客訴手冊.md", "text": self.DOC}, token="secret-token",
        )

        for item in body["items"]:
            status, saved = self.request("POST", "/api/admin/knowledge", item, token="secret-token")
            self.assertEqual(status, 200, saved)
            self.assertTrue(saved["chunk"]["chunk_id"].startswith("admin:"))

    def test_a_big_document_gets_through_the_request_size_limit(self):
        """一般端點的 64KB 上限約等於兩萬個中文字，文件一定會超過。

        這條只有 admin 打得到，所以單獨放寬；其他路徑不能跟著變寬。
        """
        big = "# 標題\n\n" + ("完整句子的一段內容。" * 200 + "\n\n") * 12

        status, body = self.request(
            "POST", "/api/admin/knowledge/analyze",
            {"name": "很長的教材.md", "text": big}, token="secret-token",
        )

        self.assertEqual(status, 200, body)
        self.assertTrue(body["items"])

    def test_other_endpoints_keep_the_small_limit(self):
        status, body = self.request(
            "POST", "/api/admin/knowledge",
            {"section_title": "測試", "text": "字" * 60000}, token="secret-token",
        )

        self.assertEqual(status, 400)
        self.assertIn("大小", body["message"])
