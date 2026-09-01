import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.humanize import DELAY_RANGE, postprocess, reply_delay, strip_citations
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
    """送出前的固定動作；回覆的語氣與長短規則寫在 line 語氣裡，這裡沒有可調參數。"""

    def test_citations_are_stripped_before_sending(self):
        self.assertEqual(strip_citations("先看回覆率 [1]"), "先看回覆率")

    def test_postprocess_splits_on_blank_lines_and_drops_punctuation(self):
        parts = postprocess("先看回覆率，這週抓 20 則 [1]\n\n明天再回報給我 [2]")

        self.assertEqual(parts, ["先看回覆率 這週抓 20 則", "明天再回報給我"])

    def test_one_message_can_have_two_lines(self):
        """一則裡面可以有兩行（空一行才換一則），超過兩行會自動重排。"""
        self.assertEqual(postprocess("我想要吃\n海鮮"), ["我想要吃\n海鮮"])

    def test_a_wall_of_lines_is_reflowed_into_short_messages(self):
        """模型忘了空行時，八行不能全擠進同一則。"""
        parts = postprocess("今天先找最近 5 筆預約\n逐筆確認有沒有說清楚\n日期時間\n預計時長")

        self.assertEqual(len(parts), 2)
        self.assertTrue(all(part.count("\n") <= 1 for part in parts), parts)

    def test_postprocess_caps_at_three_messages(self):
        self.assertEqual(len(postprocess("一 [1]\n\n二\n\n三\n\n四")), 3)
        self.assertEqual(len(postprocess("一 [1]\n\n二\n\n三\n\n四\n\n五\n\n六")), 3)

    def test_postprocess_keeps_every_line_when_capping(self):
        """超過 3 則要把中間併起來，不能砍掉尾巴——收尾的問句不能消失。"""
        parts = postprocess(
            "可以先用這個貼文範例\n\n最近想整理髮型的你\n\n這週有 2 個名額"
            "\n\n想換個顏色嗎\n\n要一起改嗎"
        )

        self.assertEqual(parts[0], "可以先用這個貼文範例")
        self.assertEqual(parts[-1], "要一起改嗎")
        joined = " ".join(parts)
        for line in ("最近想整理髮型的你", "這週有 2 個名額", "想換個顏色嗎"):
            self.assertIn(line, joined)

    def test_postprocess_splits_a_long_single_line(self):
        """一行太長就斷行，斷完超過 2 行才換下一則。"""
        parts = postprocess("先看這週的回覆率 抓 20 則來看 明天再回報給我")

        self.assertEqual(parts, ["先看這週的回覆率\n抓 20 則來看", "明天再回報給我"])

    def test_no_line_is_longer_than_the_twelve_character_rule(self):
        """只數行數擋不住「一行 120 字」——行數檢查看到 1 行就放行了。"""
        wall = (
            "因為你一年前染過黑色染膏 現在又是細髮 沒有漂過 我會先確認髮尾的殘留和彈性 "
            "再評估奶茶色需要漂到哪個程度 奶茶色不一定都要漂"
        )

        parts = postprocess(wall)

        for part in parts:
            for line in part.split("\n"):
                # 字數只算內容不算空白；單一詞組本身就超過時不硬切，所以放寬一點。
                self.assertLessEqual(len(line.replace(" ", "")), 16, line)
            self.assertLessEqual(len(part.split("\n")), 2, part)

    def test_message_gaps_make_the_replies_arrive_one_by_one(self):
        """三則不能同時跳出來——每一則之間要再等一小段。"""
        from app.humanize import message_gaps

        gaps = message_gaps(3)

        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(2 <= gap <= 4 for gap in gaps), gaps)

    def test_a_single_message_needs_no_gap(self):
        from app.humanize import message_gaps

        self.assertEqual(message_gaps(1), [])

    def test_delay_stays_inside_the_reply_token_window(self):
        for _ in range(20):
            delay = reply_delay()
            self.assertGreaterEqual(delay, DELAY_RANGE[0])
            self.assertLessEqual(delay, DELAY_RANGE[1])


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
        self.assertNotIn("style", body)

    def test_style_endpoints_are_gone(self):
        # 回覆行為由 line 語氣決定，沒有可調參數，也就沒有設定端點。
        self.assertEqual(self.request("GET", "/api/bot/style")[0], 404)
        self.assertEqual(
            self.request("POST", "/api/bot/style", {"style": {"tone": "humor"}})[0], 404
        )

    def test_reply_returns_line_ready_messages(self):
        stub = StubAnswerer("先看這週回覆率 [1]\n\n明天回報給我 [1]")
        self.context.service.answerer = stub

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "燙髮後怎麼整理？",
            "conversation_id": "C123",
            "context": {
                "group_name": "台中一店", "speaker": "小美", "stage": "開權限",
                "recent": ["設計師 小美: 版面弄好了", "輔導 阿明: 很棒"],
            },
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["messages"], ["先看這週回覆率", "明天回報給我"])
        # 兩則訊息＝一個間隔，lurebot 照這個秒數 sleep 再送下一則。
        self.assertEqual(len(body["message_gaps"]), 1)
        self.assertGreaterEqual(body["delay_seconds"], DELAY_RANGE[0])
        self.assertNotIn("[1]", body["answer"])
        self.assertEqual(body["citations"][0]["locator"], "aftercare-1")
        self.assertEqual(stub.tone, "line")
        self.assertFalse(stub.include_followups)
        self.assertIn("台中一店", stub.extra_instruction)
        self.assertIn("小美", stub.extra_instruction)
        self.assertIn("版面弄好了", stub.extra_instruction)

    def test_reply_survives_a_missing_citation(self):
        # 引用只給後台核對用，送出前就剝掉；少了編號不該讓 LINE 整則不回。
        self.context.service.answerer = StubAnswerer("先看這週回覆率\n\n明天回報給我")

        status, body = self.request("POST", "/api/bot/reply", {
            "message": "燙髮後怎麼整理？", "conversation_id": "C123",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["messages"], ["先看這週回覆率", "明天回報給我"])

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

    def test_smalltalk_reaches_line(self):
        # 打招呼、道謝、應聲不查知識庫，但一定要回——群組裡已讀不回最傷。
        status, body = self.request("POST", "/api/bot/reply", {
            "message": "hello", "conversation_id": "C123",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["answer_mode"], "smalltalk")
        self.assertTrue(body["messages"])

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
