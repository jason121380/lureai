import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.humanize import DEFAULT_STYLE, normalize_style, postprocess, reply_delay, strip_citations
from app.server import AppContext, create_server

from tests.test_ingest import approved_chunk


class StubAnswerer:
    """固定回一段「模型產出」，用來驗證出口的拆則與去引用。"""

    model_enabled = True
    model_name = "stub-model"

    def __init__(self, text: str, mode: str = "llm"):
        self.text = text
        self.mode = mode
        self.extra_instruction = ""
        self.include_followups = None
        self.tone = ""

    def answer(
        self, _question, _hits, history=None, allow_model=True, tone="expert",
        extra_instruction="", include_followups=True,
    ):
        self.extra_instruction = extra_instruction
        self.include_followups = include_followups
        self.tone = tone
        return self.text, self.mode, "used", {
            "input_tokens": 100, "output_tokens": 20,
        }


class HumanizeTests(unittest.TestCase):
    def test_unknown_values_fall_back_to_defaults(self):
        style = normalize_style({"delay": "instant", "length": "epic", "tone": "angry"})

        self.assertEqual(style["delay"], DEFAULT_STYLE["delay"])
        self.assertEqual(style["length"], DEFAULT_STYLE["length"])
        self.assertEqual(style["tone"], DEFAULT_STYLE["tone"])

    def test_override_keeps_stored_values_for_missing_keys(self):
        stored = normalize_style({"tone": "calm", "length": "long"})
        style = normalize_style({"tone": "lively"}, base=stored)

        self.assertEqual(style["tone"], "lively")
        self.assertEqual(style["length"], "long")

    def test_citations_are_stripped_before_sending(self):
        self.assertEqual(strip_citations("先看回覆率 [1]"), "先看回覆率")

    def test_postprocess_splits_lines_and_drops_punctuation(self):
        style = normalize_style({"no_punct": True, "split_long": True})

        parts = postprocess("先看回覆率，這週抓 20 則 [1]\n明天再回報給我 [2]", style)

        self.assertEqual(parts, ["先看回覆率 這週抓 20 則", "明天再回報給我"])

    def test_postprocess_caps_at_three_messages(self):
        style = normalize_style({"split_long": True})

        parts = postprocess("一 [1]\n二\n三\n四", style)

        self.assertEqual(len(parts), 3)

    def test_postprocess_keeps_every_line_when_capping(self):
        """超過 3 則要把中間併起來，不能砍掉尾巴——收尾的問題不能消失。"""
        style = normalize_style({"split_long": True})

        parts = postprocess("可以先用這個貼文範例\n最近想整理髮型的你\n這週有 2 個名額\n要一起改嗎", style)

        self.assertEqual(parts[0], "可以先用這個貼文範例")
        self.assertEqual(parts[-1], "要一起改嗎")
        self.assertIn("最近想整理髮型的你", parts[1])
        self.assertIn("這週有 2 個名額", parts[1])

    def test_postprocess_merges_lines_when_split_is_off(self):
        style = normalize_style({"split_long": False, "no_punct": False})

        parts = postprocess("先看回覆率。[1]\n再看預約數。", style)

        self.assertEqual(parts, ["先看回覆率。 再看預約數。"])

    def test_delay_stays_inside_the_reply_token_window(self):
        for name in ("none", "short", "natural", "slow"):
            delay = reply_delay(normalize_style({"delay": name}))
            self.assertGreaterEqual(delay, 0)
            self.assertLessEqual(delay, 30)


class BotApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        source = root / "knowledge.jsonl"
        source.write_text(json.dumps(approved_chunk(
            locator="aftercare-1",
            title="燙髮居家照護",
            text="燙髮後整理時，依照設計師示範方向吹整。",
        ), ensure_ascii=False), encoding="utf-8")
        self.context = AppContext.create(
            db_path=root / "knowledge.db",
            knowledge_path=source,
            static_dir=root,
            admin_token="secret-token",
            bot_token="bot-token",
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

    def request(self, method, path, payload=None, token="bot-token"):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Bot-Token"] = token
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_bot_endpoints_reject_wrong_token(self):
        self.assertEqual(self.request("GET", "/api/bot/health", token="nope")[0], 401)
        self.assertEqual(self.request("GET", "/api/bot/health", token=None)[0], 401)
        self.assertEqual(
            self.request("POST", "/api/bot/reply", {"message": "嗨"}, token="nope")[0], 401
        )

    def test_health_reports_the_brain_state(self):
        status, body = self.request("GET", "/api/bot/health")

        self.assertEqual(status, 200)
        self.assertEqual(body["chunks"], 1)
        self.assertIn("model_enabled", body)
        self.assertEqual(body["style"], DEFAULT_STYLE)

    def test_style_is_stored_and_read_back(self):
        status, body = self.request("POST", "/api/bot/style", {"style": {
            "tone": "calm", "length": "medium", "no_punct": False,
            "extra_prompt": "先問對方的店在哪",
        }})

        self.assertEqual(status, 200)
        self.assertEqual(body["style"]["tone"], "calm")

        status, body = self.request("GET", "/api/bot/style")
        self.assertEqual(status, 200)
        self.assertEqual(body["style"]["length"], "medium")
        self.assertFalse(body["style"]["no_punct"])
        self.assertEqual(body["style"]["extra_prompt"], "先問對方的店在哪")

    def test_reply_returns_line_ready_messages(self):
        stub = StubAnswerer("先看這週回覆率 [1]\n明天回報給我 [1]")
        self.context.service.answerer = stub

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "燙髮後怎麼整理？",
            "conversation_id": "C123",
            "context": {
                "group_name": "台中一店", "speaker": "小美", "stage": "開權限",
                "recent": ["設計師 小美: 版面弄好了", "輔導 阿明: 很棒"],
            },
            "style": {"delay": "none"},
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["messages"], ["先看這週回覆率", "明天回報給我"])
        self.assertEqual(body["delay_seconds"], 0.0)
        self.assertNotIn("[1]", body["answer"])
        self.assertEqual(body["citations"][0]["locator"], "aftercare-1")
        self.assertEqual(stub.tone, "line")
        self.assertFalse(stub.include_followups)
        self.assertIn("台中一店", stub.extra_instruction)
        self.assertIn("小美", stub.extra_instruction)
        self.assertIn("版面弄好了", stub.extra_instruction)

    def test_reply_stays_silent_on_sensitive_topics(self):
        self.context.service.answerer = StubAnswerer("不該送出的內容 [1]")

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "我要退費，可以幫我處理嗎？", "conversation_id": "C123",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "escalated")
        self.assertEqual(body["messages"], [])

    def test_reply_stays_silent_when_knowledge_does_not_cover_it(self):
        self.context.service.answerer = StubAnswerer("不該送出的內容 [1]")

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "這台冰箱多少錢", "conversation_id": "C123",
        })

        self.assertEqual(status, 200)
        self.assertNotEqual(body["status"], "answered")
        self.assertEqual(body["messages"], [])

    def test_boundary_reply_is_sent_as_is(self):
        self.context.service.answerer = StubAnswerer("不該用到的模型回覆 [1]")

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "你是AI嗎？", "conversation_id": "C123",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertTrue(body["messages"])
        self.assertNotIn("不該用到的模型回覆", " ".join(body["messages"]))

    def test_extractive_fallback_never_reaches_line(self):
        self.context.service.answerer = StubAnswerer("知識庫原文傾印", mode="extractive")

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "燙髮後怎麼整理？", "conversation_id": "C123",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["messages"], [])

    def test_bot_traffic_is_audited_under_the_service_account(self):
        self.context.service.answerer = StubAnswerer("先看這週回覆率 [1]")

        self.request("POST", "/api/bot/reply", {
            "message": "燙髮後怎麼整理？", "conversation_id": "C123",
        })

        audits = self.context.store.list_audits(limit=5)
        self.assertTrue(audits)
        self.assertEqual(audits[0]["user_id"], self.context.bot_user_id)
        self.assertEqual(audits[0]["conversation_id"], "C123")


if __name__ == "__main__":
    unittest.main()
