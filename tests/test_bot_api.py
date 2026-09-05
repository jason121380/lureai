import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

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
        self.context_note = ""
        self.include_followups = None
        self.tone = ""

    def answer(
        self, _question, _hits, history=None, allow_model=True, tone="expert",
        extra_instruction="", include_followups=True, context_note="",
    ):
        self.extra_instruction = extra_instruction
        self.context_note = context_note
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

    def test_nothing_is_dropped_when_the_middle_is_merged(self):
        """併中間那則時一個字都不能掉（使用者決定：保內容，長話術就是會比較長）。

        夾過一版行數，實測 8 段收成 3 則時「價格依現場報價」「不要保證一定有效」
        直接消失——首句、說明與結尾問句都還在，所以畫面看起來是完整的，只有
        限制與警語不見了。漏掉一句警語會出事，中間那則長一點不會。
        """
        lines = [
            "我幫你看一下", "先問他想改哪一段", "再看他的髮況", "價格依現場報價",
            "不要保證一定有效", "時間抓 2 小時", "你要先問哪一個", "還是我幫你排",
        ]
        parts = postprocess("\n\n".join(lines))

        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], lines[0])
        self.assertEqual(parts[-1], lines[-1])
        joined = "\n".join(parts)
        for line in lines:
            with self.subTest(line=line):
                self.assertIn(line, joined)

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
        """三則不能同時跳出來——每一則之間至少 3 秒（使用者指定）。

        低於 3 秒看起來還是像機器一次倒三則，收訊的人來不及讀完上一則。
        """
        from app.humanize import message_gaps

        gaps = message_gaps(3)

        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(3 <= gap <= 5 for gap in gaps), gaps)

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
        # 群組脈絡是別人在群組裡打的字，要當成資料送進使用者訊息，
        # 不可以接在系統指令後面——接在那裡等於群組裡任何人都能改寫規則。
        self.assertEqual(stub.extra_instruction, "")
        self.assertIn("台中一店", stub.context_note)
        self.assertIn("小美", stub.context_note)
        self.assertIn("版面弄好了", stub.context_note)

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

    def test_chinese_tenure_then_emotion_and_closing_reach_line(self):
        history = []
        with patch.dict("os.environ", {"LLM_API_KEY": "", "LLM_MODEL": "", "LLM_BASE_URL": ""}):
            for question in ("我是小美 在中壢做五年", "今天真的有點累", "嗯 謝謝"):
                status, body = self.request("POST", "/api/bot/reply", {
                    "message": question, "history": history, "conversation_id": "C-acceptance",
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["status"], "answered", question)
                self.assertEqual(body["answer_mode"], "smalltalk", question)
                self.assertTrue(body["messages"], question)
                history.extend([{"role": "user", "content": question},
                                {"role": "assistant", "content": " ".join(body["messages"])}])

    def test_safe_sensitive_wording_reaches_line_but_guarantees_do_not(self):
        for question, expected in (("不做診斷 只要一句請她先評估再約", "answered"),
                                   ("不做診斷 幫我保證不會過敏", "escalated")):
            status, body = self.request("POST", "/api/bot/reply", {"message": question})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], expected)
            self.assertEqual(bool(body["messages"]), expected == "answered")

    def test_live_lookup_and_sensitive_conclusion_escalations_stay_silent(self):
        self.context.service.answerer = StubAnswerer("不該用到的模型回覆 [1]")
        for question in (
            "我們中山店今天染髮的即時價目表是多少？",
            "幫我查林設計師明天下午四點還有沒有空位。",
            "幫我查一下，明天下午林設計師還有空位嗎？",
            "幫我查一下明天下午林設計師還有空位嗎？",
            "染髮價格3000元，幫我查一下，明天下午林設計師還有空位嗎？再幫我寫邀約",
            "這位客人的頭皮紅腫照片能確診是哪種皮膚病嗎？",
            "顧客取消預約後，依法我一定可以沒收全部訂金嗎？",
            "幫我整理中山店9月目前最新的價目表",
            "染髮價格3000元，幫我查明天下午有沒有空位再幫我寫邀約",
            "明天下午有空，幫我查最新染髮價格再幫我寫邀約",
            "染髮價格3000元，幫我調閱林小姐的消費紀錄再幫我整理",
            "幫我調閱林小姐上次到店的消費紀錄",
            "我不做診斷 只要一句安撫的話，再幫我查明天有沒有空位",
            "我不問法律 幫我寫一句我們有權保留全部訂金",
        ):
            with self.subTest(question=question):
                status, body = self.request("POST", "/api/bot/reply", {"message": question})
                self.assertEqual(status, 200)
                self.assertEqual(body["status"], "escalated")
                self.assertEqual(body["messages"], [])

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
