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

    def answer(self, _question, _hits, history=None, allow_model=True):
        self.history = history
        self.allow_model = allow_model
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
        result = self.service.chat("染髮多少錢？")

        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["reason"], "price_or_promotion")
        self.assertEqual(result["citations"], [])

    def test_empty_question_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "問題不可為空"):
            self.service.chat("   ")

    def test_chat_writes_audit_record(self):
        result = self.service.chat("染髮多少錢？", "conversation-1")

        audits = self.store.list_audits()
        self.assertEqual(audits[0]["trace_id"], result["trace_id"])
        self.assertEqual(audits[0]["status"], "escalated")

    def test_chat_stream_without_model_yields_single_result(self):
        events = list(self.service.chat_stream("燙髮後怎麼整理？", "conversation-1"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "result")
        self.assertEqual(events[0]["status"], "answered")
        self.assertEqual(events[0]["answer_mode"], "extractive")
        self.assertTrue(events[0]["citations"])

    def test_chat_stream_emits_deltas_then_authoritative_result(self):
        class StreamingAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None):
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

        self.assertEqual([event["type"] for event in events], ["delta", "delta", "result"])
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

            def stream_answer(self, _question, _hits, history=None):
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
        self.assertEqual(result["followups"], [
            "回覆速度標準是什麼？", "如何抽查私訊品質？", "預約引導怎麼寫？",
        ])

    def test_chat_stream_falls_back_when_stream_lacks_citations(self):
        class UncitedAnswerer:
            model_enabled = True
            model_name = "test-model"

            def stream_answer(self, _question, _hits, history=None):
                yield ("delta", "沒有引用的回答")

            def _extractive_answer(self, hits, model_failed=False):
                return "模型暫時無法完成生成，原文 [1]"

        self.service.answerer = UncitedAnswerer()
        events = list(self.service.chat_stream("燙髮後怎麼整理？"))

        result = events[-1]
        self.assertEqual(result["answer_mode"], "extractive")
        self.assertEqual(result["model_status"], "missing_citations")
        self.assertIn("原文", result["answer"])

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
