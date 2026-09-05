import json
import tempfile
import unittest
from pathlib import Path

from app.ingest import ingest_jsonl
from app.retrieval import Retriever
from app.storage import KnowledgeStore
from scripts.evaluate_holdout import FIXTURE, KNOWLEDGE, evaluate, exact_seed_overlaps, load_fixture


class IndependentHoldoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load_fixture()

    def test_fixture_has_independent_retrieval_and_exactly_fifty_multiturn_cases(self):
        self.assertEqual(exact_seed_overlaps(self.payload), [])
        self.assertEqual(len(self.payload["conversation_cases"]), 50)
        self.assertTrue(all(sum(m["role"] == "user" for m in case["messages"]) >= 2
                            for case in self.payload["conversation_cases"]))
        self.assertIn("not raw transcripts", self.payload["provenance"])

    def test_offline_metrics_have_all_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / "holdout.db")
            try:
                ingest_jsonl(store, KNOWLEDGE, expected_access_level="internal_coaching")
                result = evaluate(self.payload, Retriever(store))
            finally:
                store.close()
        self.assertEqual(result["answered_cases"], 15)
        self.assertEqual(result["no_answer_cases"], 5)
        self.assertIn("recall_at_3", result)
        self.assertIn("mrr_at_3", result)
        self.assertIn("no_answer_threshold_proxy_rate", result)
        self.assertIn("no_answer_policy_false_positive_rate", result)
        self.assertEqual(len(result["case_diagnostics"]), 20)

    def test_fixture_is_json_and_does_not_modify_retrieval_configuration(self):
        parsed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(parsed["version"], 1)
        self.assertFalse(any("alias" in case for case in parsed["retrieval_cases"]))

    def test_expected_locators_exist_and_have_source_meaning_notes(self):
        locators = {json.loads(line)["locator"] for line in KNOWLEDGE.read_text(encoding="utf-8").splitlines()}
        for case in self.payload["retrieval_cases"]:
            self.assertTrue(set(case["expected_locators"]) <= locators)
            if case["expected_locators"]:
                self.assertTrue(case.get("expected_reason"), case["id"])

    def test_overlap_guard_reads_list_aliases_as_individual_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge = root / "knowledge.jsonl"
            bank = root / "question_bank.json"
            knowledge.write_text(json.dumps({"aliases": ["真正的問法", "另一題"]}, ensure_ascii=False) + "\n", encoding="utf-8")
            bank.write_text(json.dumps({"sections": {}}), encoding="utf-8")
            payload = {"retrieval_cases": [{"id": "duplicate", "query": "真正的問法"}]}

            self.assertEqual(exact_seed_overlaps(payload, knowledge, bank), ["duplicate"])

    def test_r12_marks_that_the_source_has_no_timing_answer(self):
        case = next(case for case in self.payload["retrieval_cases"] if case["id"] == "r12")
        self.assertTrue(case["requires_unknown_timing_answer"])


if __name__ == "__main__":
    unittest.main()
