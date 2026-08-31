import json
import tempfile
import unittest
from pathlib import Path

from app.ingest import ingest_jsonl
from app.retrieval import Retriever
from app.storage import KnowledgeStore
from scripts.coverage_report import run as coverage_run


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge" / "designer_coaching_process.jsonl"
BANK = ROOT / "config" / "question_bank.json"


class QuestionBankTests(unittest.TestCase):
    def test_seed_questions_point_at_locators_that_exist(self):
        sections = json.loads(BANK.read_text(encoding="utf-8"))["sections"]
        locators = {
            json.loads(line)["locator"]
            for line in KNOWLEDGE.read_text(encoding="utf-8").splitlines() if line.strip()
        }

        unknown = sorted(set(sections) - locators)
        self.assertEqual(unknown, [], f"問法索引指向不存在的知識：{unknown}")

    def test_coverage_stays_high_enough_to_answer(self):
        report = coverage_run(KNOWLEDGE, BANK, top_n=3)

        self.assertGreaterEqual(report["questions"], 300)
        self.assertGreaterEqual(report["hit_rate"], 0.85)
        # Every seeded phrasing has to clear the confidence gate, otherwise the
        # designer gets the "no data" reply for a question we do cover.
        self.assertEqual(report["above_threshold"], 1.0)


class AliasIsolationTests(unittest.TestCase):
    def test_aliases_are_searchable_but_never_part_of_the_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "aliases.db")
            self.addCleanup(store.close)
            ingest_jsonl(store, KNOWLEDGE, expected_access_level="internal_coaching")
            retriever = Retriever(store)

            hits = retriever.retrieve("二選一怎麼問", limit=3)

            self.assertTrue(hits)
            self.assertIn(hits[0].locator, {"chat-10", "chat-11"})
            # The citation shows the knowledge text, not the alias bank.
            self.assertNotIn("二選一怎麼問", hits[0].citation()["text"])


if __name__ == "__main__":
    unittest.main()
