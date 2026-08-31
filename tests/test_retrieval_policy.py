import json
import tempfile
import unittest
from pathlib import Path

from app.ingest import ingest_jsonl
from app.policy import PolicyEngine
from app.retrieval import Retriever, SearchHit
from app.storage import KnowledgeStore

from tests.test_ingest import approved_chunk


class RetrievalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = KnowledgeStore(root / "knowledge.db")
        source = root / "knowledge.jsonl"
        rows = [
            approved_chunk(
                chunk_id="aftercare",
                locator="aftercare-1",
                section_title="燙後整理",
                title="燙髮居家照護",
                text="燙髮後整理時，依照設計師示範的方向吹整，避免拉扯頭髮。",
            ),
            approved_chunk(
                chunk_id="communication",
                locator="policy-1",
                section_title="溝通原則",
                title="客服溝通",
                text="先確認顧客真正想解決的問題，再以中性問題釐清需求。",
            ),
            approved_chunk(
                chunk_id="intake",
                locator="intake-1",
                section_title="必要資訊",
                title="預約與接待問詢原則",
                text="客服可先詢問想做的服務項目、希望前往的門市、日期與大致時段，以及是否指定設計師。",
            ),
        ]
        source.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        ingest_jsonl(self.store, source)
        self.retriever = Retriever(self.store)
        self.policy = PolicyEngine(minimum_score=0.72)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_chinese_query_retrieves_aftercare_chunk(self):
        hits = self.retriever.retrieve("燙髮後怎麼整理")

        self.assertTrue(hits)
        self.assertEqual(hits[0].locator, "aftercare-1")
        self.assertGreaterEqual(hits[0].score, 0.72)

    def test_unrelated_query_has_no_high_confidence_hit(self):
        hits = self.retriever.retrieve("明天天氣如何")

        self.assertTrue(not hits or hits[0].score < 0.72)

    def test_generic_question_words_do_not_create_false_confidence(self):
        hits = self.retriever.retrieve("明天天氣怎麼樣？")

        self.assertTrue(not hits or hits[0].score < 0.72)

    def test_field_concepts_make_general_booking_question_answerable(self):
        hits = self.retriever.retrieve("預約需要提供什麼資訊")

        self.assertEqual(hits[0].locator, "intake-1")
        self.assertGreaterEqual(hits[0].score, 0.72)

    def test_price_question_escalates_before_retrieval(self):
        decision = self.policy.precheck("染髮多少錢？")

        self.assertEqual(decision.action, "escalate")
        self.assertEqual(decision.reason, "price_or_promotion")

    def test_general_booking_information_is_not_treated_as_live_booking(self):
        decision = self.policy.precheck("預約需要提供什麼資訊？")

        self.assertEqual(decision.action, "continue")

    def test_live_booking_request_still_escalates(self):
        decision = self.policy.precheck("可以幫我預約明天下午嗎？")

        self.assertEqual(decision.action, "escalate")
        self.assertEqual(decision.reason, "live_schedule")

    def test_labor_question_escalates_before_historical_material_is_used(self):
        decision = self.policy.precheck("設計師薪資抽成怎麼算？")

        self.assertEqual(decision.action, "escalate")
        self.assertEqual(decision.reason, "labor_hr")

    def test_low_confidence_results_escalate(self):
        hit = SearchHit(
            chunk_id="x", title="資料", source_file="x.md", locator="p1",
            section_title="", text="內容", category="", score=0.4,
        )

        decision = self.policy.evaluate([hit])

        self.assertEqual(decision.action, "escalate")
        self.assertEqual(decision.reason, "low_confidence")

    def test_complete_high_confidence_result_is_answerable(self):
        hit = SearchHit(
            chunk_id="x", title="資料", source_file="x.md", locator="p1",
            section_title="", text="內容", category="", score=0.88,
        )

        self.assertEqual(self.policy.evaluate([hit]).action, "answer")

    def test_profile_can_supply_its_own_fallback_message(self):
        policy = PolicyEngine(minimum_score=0.72, fallback_message="請補充輔導數據。")

        decision = policy.evaluate([])

        self.assertEqual(decision.message, "請補充輔導數據。")


if __name__ == "__main__":
    unittest.main()
