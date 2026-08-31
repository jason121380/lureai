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
KNOWLEDGE = ROOT / "knowledge" / "designer_coaching_process.jsonl"
REQUIRED_TURNS = 50


class FollowupChainTests(unittest.TestCase):
    """點著建議問題一路問下去，不可以撞到「需要人工協助」。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = KnowledgeStore(Path(cls.temp.name) / "chain.db")
        ingest_jsonl(cls.store, KNOWLEDGE, expected_access_level="internal_coaching")
        cls.service = CustomerService(
            store=cls.store,
            retriever=Retriever(cls.store),
            policy=PolicyEngine(minimum_score=0.72, fallback_message="需要人工協助"),
            answerer=AnswerEngine(policy_path=ROOT / "config" / "designer_coach_policy.md"),
        )

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.temp.cleanup()

    def walk(self, start: str, turns: int = REQUIRED_TURNS) -> list[str]:
        asked: list[str] = []
        question = start
        for turn in range(turns):
            history = [{"role": "user", "content": item} for item in asked][-60:]
            result = self.service.chat(question, history=history, allow_model=False)
            self.assertEqual(
                result["status"], "answered",
                f"第 {turn + 1} 輪就需要人工：{question}（{result.get('reason')}）",
            )
            self.assertTrue(result["citations"], f"第 {turn + 1} 輪沒有引用知識：{question}")
            asked.append(question)
            nxt = [item for item in result.get("followups", []) if item not in asked]
            self.assertTrue(nxt, f"第 {turn + 1} 輪沒有新的建議問題：{question}")
            question = nxt[0]
        return asked

    def test_messaging_chain_runs_fifty_turns(self):
        asked = self.walk("二選一怎麼問？")

        self.assertEqual(len(asked), REQUIRED_TURNS)
        self.assertEqual(len(set(asked)), REQUIRED_TURNS, "追問開始重複")

    def test_ads_chain_runs_fifty_turns(self):
        self.assertEqual(len(self.walk("廣告要投多少錢？")), REQUIRED_TURNS)

    def test_social_chain_runs_fifty_turns(self):
        self.assertEqual(len(self.walk("我的版面要怎麼排？")), REQUIRED_TURNS)


if __name__ == "__main__":
    unittest.main()
