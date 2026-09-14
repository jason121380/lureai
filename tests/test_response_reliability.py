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


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "response_reliability_40.json"
KNOWLEDGE = ROOT / "knowledge" / "designer_coaching_process.jsonl"


class ResponseReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = KnowledgeStore(Path(cls.temp.name) / "reliability.db")
        ingest_jsonl(cls.store, KNOWLEDGE, expected_access_level="internal_coaching")
        cls.service = CustomerService(
            store=cls.store,
            retriever=Retriever(cls.store),
            policy=PolicyEngine(minimum_score=0.72),
            answerer=AnswerEngine(),
        )

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.temp.cleanup()

    def test_fixture_is_the_disclosed_forty_question_evaluation(self):
        cases = self.payload["cases"]

        self.assertEqual(len(cases), 40)
        self.assertEqual([item["id"] for item in cases], list(range(1, 41)))
        self.assertIn("不是盲測", self.payload["source"])

    def test_all_forty_questions_take_the_expected_offline_route(self):
        scenario = None
        history = []
        for item in self.payload["cases"]:
            if item["scenario"] != scenario:
                scenario = item["scenario"]
                history = []
            with self.subTest(case=item["id"], question=item["question"]):
                _hits, grounded, decision = self.service._route(item["question"], history)
                if item["expected_route"] == "urgent_health":
                    self.assertIsNotNone(decision)
                    self.assertEqual(decision.reason, "urgent_health")
                    for phrase in item.get("required_phrases", []):
                        self.assertIn(phrase, decision.message)
                else:
                    self.assertIsNone(decision)
                    self.assertTrue(grounded)
                    accepted = set(item.get("accepted_locators", []))
                    if accepted:
                        actual = {hit.locator for hit in grounded[:3]}
                        self.assertTrue(accepted & actual, (accepted, actual))
            history.append({"role": "user", "content": item["question"]})

    def test_urgent_health_paraphrases_and_negative_controls(self):
        urgent = (
            "她突然吸不到氣",
            "染完後客人喘不過氣",
            "客人嘴唇發紫而且快昏倒",
            "她喉嚨腫起來 意識不清",
            "客人呼吸困難 我要先做什麼",
            "客人昏厥了",
        )
        controls = (
            "客人沒有呼吸不順 只有頭皮癢",
            "貼文不要寫成讓人喘不過氣的長句",
            "資料表欄位寫呼吸困難作為警示選項",
            "染後頭皮微紅但呼吸正常",
            "廣告文案讀起來很卡",
            "最近工作忙到快昏倒是比喻",
        )
        for question in urgent:
            with self.subTest(kind="urgent", question=question):
                self.assertEqual(self.service.policy.precheck(question).reason, "urgent_health")
        for question in controls:
            with self.subTest(kind="control", question=question):
                self.assertNotEqual(self.service.policy.precheck(question).reason, "urgent_health")


if __name__ == "__main__":
    unittest.main()
