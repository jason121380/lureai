import tempfile
import unittest
from pathlib import Path

from app.ingest import ingest_jsonl
from app.policy import PolicyEngine
from app.retrieval import Retriever
from app.service import CustomerService
from app.storage import KnowledgeStore


ROOT = Path(__file__).resolve().parents[1]
COACHING_KNOWLEDGE = ROOT / "knowledge" / "designer_coaching_process.jsonl"


class CoachingRagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / "coaching.db")
        report = ingest_jsonl(
            self.store,
            COACHING_KNOWLEDGE,
            expected_access_level="internal_coaching",
        )
        self.assertEqual(report.rejected, 0)
        self.retriever = Retriever(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assert_top_locators_include(self, question, expected):
        """Any of `expected` answers the question; several playbooks overlap."""
        accepted = {expected} if isinstance(expected, str) else set(expected)
        locators = [hit.locator for hit in self.retriever.retrieve(question, limit=6)]
        self.assertTrue(accepted & set(locators[:3]), locators)

    def test_thin_follow_up_falls_back_to_the_conversation_topic(self):
        """「我想寫得自然一點」自己只靠「自然」兩個字勉強過門檻，撈到的三塊全錯。

        使用者實際遇到的：對話在談染燙後的關懷訊息，這句追問卻撈到評論信任、
        廣告貼文分工、職涯迷惘。分數 0.767 剛好過 0.72，所以舊的補脈絡條件
        （低於門檻才補）不會啟動。
        """
        from app.answer import AnswerEngine

        service = CustomerService(
            store=self.store,
            retriever=self.retriever,
            policy=PolicyEngine(minimum_score=0.72),
            answerer=AnswerEngine(),
        )
        bare = [hit.locator for hit in self.retriever.retrieve("我想寫得自然一點", limit=6)]
        self.assertNotIn("coach-37", bare[:3])  # 沒有脈絡時真的撈不到

        _hits, grounded, escalation = service._route(
            "我想寫得自然一點",
            [{"role": "user", "content": "染燙護髮事後關懷訊息怎麼寫？"}],
        )

        self.assertIsNone(escalation)
        self.assertIn("coach-37", [hit.locator for hit in grounded[:3]])

    def test_self_contained_question_is_not_dragged_to_the_previous_topic(self):
        """完整的問題自己就撈得準（實測 100 題最低 0.867），不可以被前一題帶走。"""
        from app.answer import AnswerEngine

        service = CustomerService(
            store=self.store,
            retriever=self.retriever,
            policy=PolicyEngine(minimum_score=0.72),
            answerer=AnswerEngine(),
        )
        _hits, grounded, _escalation = service._route(
            "廣告一天要花多少錢？",
            [{"role": "user", "content": "客訴現場的用語要怎麼改？"}],
        )

        locators = [hit.locator for hit in grounded[:3]]
        self.assertIn("ads-04", locators)
        self.assertNotIn("ops-16", locators)

    def test_retrieves_booking_funnel_diagnosis(self):
        self.assert_top_locators_include(
            "私訊很多但預約很少，要先檢查什麼？", {"coach-04", "ads-10", "chat-01"}
        )

    def test_retrieves_priority_for_stale_social_profile(self):
        self.assert_top_locators_include(
            "社群三個月沒更新，輔導優先順序怎麼排？", {"coach-06", "social-07", "social-06"}
        )

    def test_retrieves_one_on_one_meeting_process(self):
        self.assert_top_locators_include(
            "怎麼進行一次設計師一對一輔導？", {"coach-05", "session-01"}
        )

    def test_retrieves_media_capture_checklist(self):
        self.assert_top_locators_include(
            "作品影片拍攝和剪輯要檢查什麼？", {"coach-14", "social-12", "social-10"}
        )

    def test_answers_the_follow_up_that_used_to_dead_end(self):
        self.assert_top_locators_include("記滿20則後看什麼？", {"chat-03", "chat-02", "chat-21"})

    def test_retrieves_two_choice_technique(self):
        self.assert_top_locators_include("二選一怎麼問？", {"chat-10", "chat-11"})


if __name__ == "__main__":
    unittest.main()
