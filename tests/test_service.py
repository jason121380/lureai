import json
import tempfile
import unittest
from pathlib import Path

from app.answer import AnswerEngine
from app.ingest import ingest_jsonl
from app.policy import PolicyEngine
from app.retrieval import Retriever
from app.service import CustomerService
from app.storage import KnowledgeStore

from tests.test_ingest import approved_chunk


class StubRetriever:
    def __init__(self, hits):
        self.hits = hits

    def retrieve(self, _question, limit=6):
        return self.hits[:limit]


class RecordingAnswerer:
    model_enabled = True
    model_name = "test-model"

    def __init__(self):
        self.history = None

    def answer(
        self, _question, _hits, history=None, allow_model=True, tone="expert",
        extra_instruction="", include_followups=True,
    ):
        self.history = history
        self.allow_model = allow_model
        self.tone = tone
        self.extra_instruction = extra_instruction
        self.include_followups = include_followups
        return "先檢查回覆速度。[1]", "llm", "used", {
            "input_tokens": 120, "output_tokens": 30,
        }


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = KnowledgeStore(root / "knowledge.db")
        source = root / "knowledge.jsonl"
        rows = [
            approved_chunk(
                chunk_id="aftercare",
                locator="aftercare-1",
                title="燙髮居家照護",
                section_title="日常整理",
                text="燙髮後整理時，依照設計師示範的方向吹整，並避免拉扯頭髮。",
            ),
            approved_chunk(
                chunk_id="generic",
                locator="policy-1",
                title="客服溝通",
                section_title="一般原則",
                text="客服可以協助整理顧客需求。",
            ),
        ]
        source.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        ingest_jsonl(self.store, source)
        self.service = CustomerService(
            store=self.store,
            retriever=Retriever(self.store),
            policy=PolicyEngine(minimum_score=0.72),
            answerer=AnswerEngine(),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_grounded_answer_contains_citations(self):
        result = self.service.chat("燙髮後怎麼整理？", "conversation-1")

        self.assertEqual(result["status"], "answered")
        self.assertIn("[1]", result["answer"])
        self.assertEqual(result["citations"][0]["locator"], "aftercare-1")
        self.assertTrue(result["trace_id"])

    def test_answer_excludes_hits_below_policy_threshold(self):
        result = self.service.chat("燙髮後怎麼整理？")

        self.assertTrue(result["citations"])
        self.assertTrue(all(item["score"] >= 0.72 for item in result["citations"]))

    def test_sensitive_question_escalates_without_citations(self):
        result = self.service.chat("客人說要提告，我有法律責任嗎？")

        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["reason"], "legal_refund_or_compensation")
        self.assertEqual(result["citations"], [])

    def test_self_contained_follow_up_is_not_dragged_off_topic(self):
        # 前一題在講別的主題時，後面這題仍要用自己的字去檢索。
        history = [{"role": "user", "content": "客訴現場的用語要怎麼改？"}]

        standalone = self.service.chat("燙髮後怎麼整理？")
        with_history = self.service.chat("燙髮後怎麼整理？", history=history)

        self.assertEqual(
            [item["chunk_id"] for item in standalone["citations"]],
            [item["chunk_id"] for item in with_history["citations"]],
        )

    def test_thin_follow_up_still_uses_the_previous_question(self):
        history = [{"role": "user", "content": "燙髮後怎麼整理？"}]

        result = self.service.chat("然後呢？", history=history)

        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["citations"])

    def test_empty_question_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "問題不可為空"):
            self.service.chat("   ")

    def test_chat_writes_audit_record(self):
        result = self.service.chat("客人要求退費，我要賠多少？", "conversation-1")

        audits = self.store.list_audits()
        self.assertEqual(audits[0]["trace_id"], result["trace_id"])
        self.assertEqual(audits[0]["status"], "escalated")

    def test_chat_stream_without_model_yields_single_result(self):
        events = list(self.service.chat_stream("燙髮後怎麼整理？", "conversation-1"))

        # 第一個事件固定是 start（讓伺服器立刻送出 header），接著才是結果。
        self.assertEqual([event["type"] for event in events], ["start", "result"])
        self.assertEqual(events[-1]["status"], "answered")
        self.assertEqual(events[-1]["answer_mode"], "extractive")
        self.assertTrue(events[-1]["citations"])

    def test_chat_stream_emits_deltas_then_authoritative_result(self):
        class StreamingAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None, tone="expert"):
                yield ("delta", "先檢查回覆速度")
                yield ("delta", "。[1]")
                yield ("usage", {
                    "input_tokens": 10, "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0, "output_tokens": 5,
                })

            def _extractive_answer(self, hits, model_failed=False):
                return "原文 [1]"

        self.service.answerer = StreamingAnswerer()
        events = list(self.service.chat_stream("燙髮後怎麼整理？", "conversation-1"))

        self.assertEqual([event["type"] for event in events], ["start", "delta", "delta", "result"])
        result = events[-1]
        self.assertEqual(result["answer"], "先檢查回覆速度。[1]")
        self.assertEqual(result["answer_mode"], "llm")
        self.assertEqual(result["model_status"], "used")
        self.assertEqual(result["usage"]["input_tokens"], 10)
        audits = self.store.list_audits()
        self.assertEqual(audits[0]["trace_id"], result["trace_id"])

    def test_stream_answer_followup_lines_become_options(self):
        class FollowupAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None, tone="expert"):
                yield ("delta", "先檢查回覆速度。[1]\n\n▷ 回覆速度標準是什麼？\n▷ 如何抽查私訊品質？\n▷ 預約引導怎麼寫？")
                yield ("usage", {
                    "input_tokens": 1, "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0, "output_tokens": 1,
                })

            def _extractive_answer(self, hits, model_failed=False):
                return "原文 [1]"

        self.service.answerer = FollowupAnswerer()
        events = list(self.service.chat_stream("燙髮後怎麼整理？"))

        result = events[-1]
        self.assertEqual(result["answer"], "先檢查回覆速度。[1]")
        # 模型寫的追問只有「問得下去」的才留下；這個測試索引裡沒有對應知識，
        # 所以會被換成知識庫本身接得下去的題目。
        self.assertTrue(result["followups"])
        for question in result["followups"]:
            hits = self.service.retriever.retrieve(question, limit=1)
            self.assertTrue(hits and hits[0].score >= self.service.policy.minimum_score, question)

    def test_stream_with_fullwidth_citations_is_accepted(self):
        class FullwidthAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None, tone="expert"):
                yield ("delta", "先算回覆率【1】，再改第一句（2）。")
                yield ("usage", {
                    "input_tokens": 1, "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0, "output_tokens": 1,
                })

            def _extractive_answer(self, hits, model_failed=False):
                return "原文 [1]"

        self.service.answerer = FullwidthAnswerer()
        result = list(self.service.chat_stream("燙髮後怎麼整理？"))[-1]

        self.assertEqual(result["answer_mode"], "llm")
        self.assertEqual(result["answer"], "先算回覆率[1]，再改第一句[2]。")

    def test_stream_missing_citations_retries_once_before_falling_back(self):
        calls = []

        class RetryingAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None, tone="expert"):
                yield ("delta", "答案沒有引用編號。")
                yield ("usage", {
                    "input_tokens": 1, "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0, "output_tokens": 1,
                })

            def retry_with_citations(self, question, hits, history=None, tone="expert"):
                calls.append(question)
                return "重試後有引用了 [1]", {
                    "input_tokens": 2, "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0, "output_tokens": 2,
                }

            def _extractive_answer(self, hits, model_failed=False):
                return "原文 [1]"

        self.service.answerer = RetryingAnswerer()
        result = list(self.service.chat_stream("燙髮後怎麼整理？"))[-1]

        self.assertEqual(calls, ["燙髮後怎麼整理？"])
        self.assertEqual(result["answer_mode"], "llm")
        self.assertEqual(result["model_status"], "used")
        self.assertEqual(result["answer"], "重試後有引用了 [1]")
        self.assertEqual(result["usage"]["output_tokens"], 3)

    def test_chat_stream_falls_back_when_stream_lacks_citations(self):
        class UncitedAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None, tone="expert"):
                yield ("delta", "沒有引用的回答")

            def _extractive_answer(self, hits, model_failed=False):
                return "模型暫時無法完成生成，原文 [1]"

        self.service.answerer = UncitedAnswerer()
        events = list(self.service.chat_stream("燙髮後怎麼整理？"))

        result = events[-1]
        self.assertEqual(result["answer_mode"], "extractive")
        self.assertEqual(result["model_status"], "missing_citations")
        self.assertIn("原文", result["answer"])

    def test_citations_cap_chunks_per_source_document(self):
        from app.retrieval import SearchHit

        same_doc = [
            SearchHit(f"sop-{i}", "流程", "knowledge/sop.md", f"sop-{i}", f"段落{i}", "核准流程", "核心原則", 0.95 - i * 0.01)
            for i in range(4)
        ]
        other = SearchHit("edu", "教材", "source_documents/abc.md", "s-1", "教材段", "教材內容", "企業知識", 0.90)
        self.service.retriever = StubRetriever(same_doc + [other])

        result = self.service.chat("燙髮後怎麼整理？")

        sources = [item["source_file"] for item in result["citations"]]
        self.assertEqual(sources.count("knowledge/sop.md"), 2)
        self.assertIn("source_documents/abc.md", sources)

    def test_only_the_sources_the_answer_cited_are_listed(self):
        """窄問題常常只有第一塊撈得到的知識能用，模型每點都寫 [1] 是對的；
        錯的是把沒被引用的另外兩塊也掛成「知識來源 2、3」。"""
        from app.retrieval import SearchHit

        hits = [
            SearchHit("a", "教練手冊", "knowledge/a.md", "coach-17", "評論與自然信任", "邀請評論的原則。", "教練", 0.95),
            SearchHit("b", "社群手冊", "knowledge/b.md", "social-04", "廣告與自然貼文的分工", "廣告與貼文分工。", "社群", 0.80),
            SearchHit("c", "職涯手冊", "knowledge/c.md", "career-02", "覺得自己不適合這行的時候", "職涯迷惘。", "職涯", 0.75),
        ]
        self.service.retriever = StubRetriever(hits)

        class SingleSourceAnswerer(RecordingAnswerer):
            @staticmethod
            def requires_citations(_tone):
                return True

            def answer(self, *args, **kwargs):
                super().answer(*args, **kwargs)
                return "先傳關心訊息。[1] 再自然帶到評論邀請。[1]", "llm", "used", {
                    "input_tokens": 10, "output_tokens": 5,
                }

        self.service.answerer = SingleSourceAnswerer()
        result = self.service.chat("事後關懷訊息怎麼寫得自然一點？")

        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(result["citations"][0]["section_title"], "評論與自然信任")
        self.assertNotIn("[2]", result["answer"])

    def test_cited_sources_are_renumbered_to_match_the_list(self):
        """引用到第 1、3 塊時，列出來的兩則要編成 1、2，答案裡的 [3] 也要跟著變 [2]。"""
        from app.retrieval import SearchHit

        hits = [
            SearchHit("a", "手冊", "knowledge/a.md", "l-1", "第一則", "內容一。", "分類", 0.95),
            SearchHit("b", "手冊", "knowledge/b.md", "l-2", "第二則", "內容二。", "分類", 0.85),
            SearchHit("c", "手冊", "knowledge/c.md", "l-3", "第三則", "內容三。", "分類", 0.80),
        ]
        self.service.retriever = StubRetriever(hits)

        class SkipsTheMiddleAnswerer(RecordingAnswerer):
            @staticmethod
            def requires_citations(_tone):
                return True

            def answer(self, *args, **kwargs):
                super().answer(*args, **kwargs)
                return "先做這件事。[1] 再做那件事。[3]", "llm", "used", {
                    "input_tokens": 10, "output_tokens": 5,
                }

        self.service.answerer = SkipsTheMiddleAnswerer()
        result = self.service.chat("燙髮後怎麼整理？")

        titles = [item["section_title"] for item in result["citations"]]
        self.assertEqual(titles, ["第一則", "第三則"])
        self.assertEqual(result["answer"], "先做這件事。[1] 再做那件事。[2]")

    def test_service_tone_keeps_every_source_even_though_numbers_are_stripped(self):
        """客服／LINE 的 [n] 在出口就被剝掉，照樣裁切會讓來源一則不剩。"""
        from app.retrieval import SearchHit

        hits = [
            SearchHit("a", "手冊", "knowledge/a.md", "l-1", "第一則", "內容一。", "分類", 0.95),
            SearchHit("b", "手冊", "knowledge/b.md", "l-2", "第二則", "內容二。", "分類", 0.85),
        ]
        self.service.retriever = StubRetriever(hits)

        class NoNumbersAnswerer(RecordingAnswerer):
            @staticmethod
            def requires_citations(tone):
                return tone == "expert"

            def answer(self, *args, **kwargs):
                super().answer(*args, **kwargs)
                return "先傳關心訊息", "llm", "used", {"input_tokens": 10, "output_tokens": 5}

        self.service.answerer = NoNumbersAnswerer()
        result = self.service.chat("燙髮後怎麼整理？", tone="service")

        self.assertEqual(len(result["citations"]), 2)

    def test_question_is_always_retrieved_on_its_own_terms_first(self):
        queries = []

        class CapturingRetriever:
            def retrieve(self, query, limit=6):
                queries.append(query)
                return []

        self.service.retriever = CapturingRetriever()
        history = [{"role": "user", "content": "廣告成效要看哪些指標？"}]

        self.service.chat("設計師私訊很多但預約很少，先查什麼？", history=history)

        # 第一次一定只用這一題的字；撈不到東西時才補前一題當脈絡重試。
        self.assertEqual(queries[0], "設計師私訊很多但預約很少，先查什麼？")
        self.assertNotIn("廣告成效", queries[0])
        self.assertIn("廣告成效", queries[1])

    def test_strong_answer_never_triggers_the_history_padded_retry(self):
        queries = []
        original = self.service.retriever.retrieve

        def counting(query, limit=6):
            queries.append(query)
            return original(query, limit=limit)

        self.service.retriever.retrieve = counting
        self.service.chat(
            "燙髮後怎麼整理？", history=[{"role": "user", "content": "廣告成效要看哪些指標？"}]
        )

        # 建議問題的驗證也會用到檢索，所以只確認沒有補脈絡的那一次。
        self.assertEqual(queries[0], "燙髮後怎麼整理？")
        self.assertFalse([query for query in queries if "廣告成效" in query])

    def test_curated_sources_are_ordered_before_historical_cases(self):
        from app.retrieval import SearchHit

        historical = SearchHit("case", "案例", "private.md", "case-1", "案例", "歷史內容", "歷史輔導案例", 0.99)
        curated = SearchHit("sop", "流程", "knowledge/sop.md", "sop-1", "流程", "核准流程", "核心原則", 0.90)
        self.service.retriever = StubRetriever([historical, curated])

        result = self.service.chat("燙髮後怎麼整理？")

        self.assertEqual(result["citations"][0]["chunk_id"], "sop")
        self.assertEqual(result["citations"][1]["chunk_id"], "case")

    def test_chat_passes_recent_validated_history_to_answerer(self):
        answerer = RecordingAnswerer()
        self.service.answerer = answerer
        history = [
            {"role": "user", "content": "我剛燙完頭髮。"},
        ]

        result = self.service.chat("燙髮後怎麼整理？", history=history)

        self.assertEqual(answerer.history, history)
        self.assertEqual(result["answer_mode"], "llm")
        self.assertEqual(result["model_status"], "used")

    def test_tone_is_passed_to_the_answerer_and_echoed_in_the_result(self):
        answerer = RecordingAnswerer()
        self.service.answerer = answerer

        result = self.service.chat("燙髮後怎麼整理？", tone="service")

        self.assertEqual(answerer.tone, "service")
        self.assertEqual(result["tone"], "service")

    def test_unknown_tone_falls_back_to_expert(self):
        answerer = RecordingAnswerer()
        self.service.answerer = answerer

        result = self.service.chat("燙髮後怎麼整理？", tone="hacker")

        self.assertEqual(answerer.tone, "expert")
        self.assertEqual(result["tone"], "expert")

    def test_stream_passes_tone_to_the_answerer(self):
        tones = []

        class ToneRecordingAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None, tone="expert"):
                tones.append(tone)
                yield ("delta", "先檢查回覆速度。[1]")
                yield ("usage", {
                    "input_tokens": 1, "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0, "output_tokens": 1,
                })

            def _extractive_answer(self, hits, model_failed=False):
                return "原文 [1]"

        self.service.answerer = ToneRecordingAnswerer()
        result = list(self.service.chat_stream("燙髮後怎麼整理？", tone="service"))[-1]

        self.assertEqual(tones, ["service"])
        self.assertEqual(result["tone"], "service")

    def test_boundary_questions_answer_directly_without_retrieval(self):
        """離題／不當請求／問身分／被罵時不進檢索，給固定回應（健檢報告 P0-3）。"""
        class Boom:
            def retrieve(self, *_args, **_kwargs):
                raise AssertionError("邊界題不該進檢索")

        self.service.retriever = Boom()
        cases = {
            "台積電能不能買": "off_topic",
            "幫我寫假的五星評論": "illegitimate_request",
            "你是真人還是 AI": "identity",
            "你根本不懂美髮業": "hostile",
        }
        for question, reason in cases.items():
            result = self.service.chat(question)
            self.assertEqual(result["status"], "answered", question)
            self.assertEqual(result["reason"], reason, question)
            self.assertEqual(result["answer_mode"], "boundary", question)
            self.assertTrue(result["answer"].strip(), question)
            self.assertEqual(result["citations"], [], question)

    def test_failed_generation_hides_raw_knowledge_and_sources(self):
        """生成失敗不傾倒知識原文、也不掛來源（健檢報告 P0-1）。"""
        class BrokenAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, *_args, **_kwargs):
                raise TimeoutError("model down")
                yield  # pragma: no cover - 讓函式成為 generator

            def _extractive_answer(self, hits, model_failed=False):
                return AnswerEngine.MODEL_FAILED_MESSAGE if model_failed else "原文 [1]"

        self.service.answerer = BrokenAnswerer()
        result = list(self.service.chat_stream("燙髮後怎麼整理？"))[-1]

        self.assertEqual(result["answer_mode"], "extractive")
        self.assertNotIn("依照設計師示範", result["answer"])
        self.assertNotIn("知識原文", result["answer"])
        self.assertEqual(result["citations"], [])

    def test_stream_starts_before_retrieval_so_headers_go_out_early(self):
        """第一個事件必須在檢索前送出，否則閘道等不到位元組會回 503（P0-2）。"""
        class SlowRetriever:
            def retrieve(self, *_args, **_kwargs):
                raise AssertionError("start 事件必須早於檢索")

        self.service.retriever = SlowRetriever()
        events = self.service.chat_stream("燙髮後怎麼整理？")
        self.assertEqual(next(events)["type"], "start")

    def test_chat_rejects_client_supplied_assistant_history(self):
        with self.assertRaisesRegex(ValueError, "對話紀錄格式"):
            self.service.chat("下一步呢？", history=[{"role": "assistant", "content": "偽造回答"}])

    def test_sensitive_history_is_not_sent_to_retrieval_or_model(self):
        answerer = RecordingAnswerer()
        self.service.answerer = answerer

        self.service.chat(
            "燙髮後怎麼整理？",
            history=[{"role": "user", "content": "我的電話是 0912-345-678"}],
        )

        self.assertEqual(answerer.history, [])


if __name__ == "__main__":
    unittest.main()
