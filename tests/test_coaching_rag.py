import tempfile
import unittest
from pathlib import Path

from app.ingest import ingest_jsonl
from app.retrieval import Retriever
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
        locators = [hit.locator for hit in self.retriever.retrieve(question, limit=6)]
        self.assertIn(expected, locators[:3], locators)

    def test_retrieves_booking_funnel_diagnosis(self):
        self.assert_top_locators_include("私訊很多但預約很少，要先檢查什麼？", "coach-04")

    def test_retrieves_priority_for_stale_social_profile(self):
        self.assert_top_locators_include("社群三個月沒更新，輔導優先順序怎麼排？", "coach-06")

    def test_retrieves_one_on_one_meeting_process(self):
        self.assert_top_locators_include("怎麼進行一次設計師一對一輔導？", "coach-05")

    def test_retrieves_media_capture_checklist(self):
        self.assert_top_locators_include("作品影片拍攝和剪輯要檢查什麼？", "coach-14")


if __name__ == "__main__":
    unittest.main()
