import random
import tempfile
import unittest
from pathlib import Path

from app.followups import welcome_questions
from app.ingest import ingest_jsonl
from app.policy import PolicyEngine
from app.retrieval import Retriever
from app.storage import KnowledgeStore
from run import PROFILES


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge" / "designer_coaching_process.jsonl"
PROMPTS = PROFILES["designer_coach"]["welcome_prompts"]


class WelcomePromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = KnowledgeStore(Path(cls.temp.name) / "welcome.db")
        ingest_jsonl(cls.store, KNOWLEDGE, expected_access_level="internal_coaching")
        cls.retriever = Retriever(cls.store)
        cls.policy = PolicyEngine()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.temp.cleanup()

    def test_every_opening_prompt_can_be_answered(self):
        self.assertGreaterEqual(len(PROMPTS), 100)
        self.assertEqual(len(PROMPTS), len(set(PROMPTS)), "開場題目不可重複")
        for prompt in PROMPTS:
            self.assertEqual(self.policy.precheck(prompt).action, "continue", prompt)
            hits = self.retriever.retrieve(prompt, limit=1)
            self.assertTrue(hits, prompt)
            self.assertGreaterEqual(hits[0].score, self.policy.minimum_score, prompt)

    def test_opening_prompts_are_shuffled_每次不同(self):
        first = welcome_questions(limit=6, rng=random.Random(1), fallback=PROMPTS)
        second = welcome_questions(limit=6, rng=random.Random(2), fallback=PROMPTS)

        self.assertEqual(len(first), 6)
        self.assertTrue(set(first) <= set(PROMPTS))
        self.assertNotEqual(first, second, "每次開場題目應該不一樣")

    def test_falls_back_to_the_question_bank_when_a_profile_has_none(self):
        picked = welcome_questions(limit=6, rng=random.Random(3), fallback=())

        self.assertEqual(len(picked), 6)
        self.assertTrue(all(question.endswith(("？", "?")) for question in picked))


if __name__ == "__main__":
    unittest.main()
