#!/usr/bin/env python3
"""Independent retrieval holdout and optional paid conversation capture."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.answer import AnswerEngine  # noqa: E402
from app.ingest import ingest_jsonl  # noqa: E402
from app.policy import PolicyEngine  # noqa: E402
from app.retrieval import Retriever  # noqa: E402
from app.service import CustomerService  # noqa: E402
from app.storage import KnowledgeStore  # noqa: E402
from app.text_utils import normalize_for_search  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "rag_holdout.json"
KNOWLEDGE = ROOT / "knowledge" / "designer_coaching_process.jsonl"
QUESTION_BANK = ROOT / "config" / "question_bank.json"


def load_fixture(path: Path = FIXTURE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def exact_seed_overlaps(payload: dict, knowledge: Path = KNOWLEDGE,
                        question_bank: Path = QUESTION_BANK) -> list[str]:
    seeds = set()
    for line in knowledge.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        aliases = row.get("aliases", []) or []
        if isinstance(aliases, str):
            aliases = aliases.splitlines()
        for seed in aliases:
            seeds.add(normalize_for_search(seed).replace(" ", ""))
    bank = json.loads(question_bank.read_text(encoding="utf-8"))
    for aliases in bank.get("sections", {}).values():
        for seed in aliases:
            seeds.add(normalize_for_search(seed).replace(" ", ""))
    return [case["id"] for case in payload["retrieval_cases"]
            if normalize_for_search(case["query"]).replace(" ", "") in seeds]


def evaluate(payload: dict, retriever: Retriever) -> dict:
    answered = [case for case in payload["retrieval_cases"] if case.get("expected_locators")]
    no_answer = [case for case in payload["retrieval_cases"] if not case.get("expected_locators")]
    recalled = 0
    reciprocal = 0.0
    false_answers = 0
    policy_false_answers = 0
    diagnostics = []
    policy = PolicyEngine(minimum_score=.72)
    for case in answered:
        hits = retriever.retrieve(case["query"], limit=3)
        locators = [hit.locator for hit in hits]
        ranks = [locators.index(locator) + 1 for locator in case["expected_locators"] if locator in locators]
        if ranks:
            recalled += 1
            reciprocal += 1.0 / min(ranks)
        diagnostics.append({"id": case["id"], "expected": case["expected_locators"], "retrieved": locators})
    for case in no_answer:
        hits = retriever.retrieve(case["query"], limit=3)
        locators = [hit.locator for hit in hits]
        if any(hit.score >= 0.72 for hit in hits):
            false_answers += 1
        if policy.precheck(case["query"]).action == "continue" and policy.evaluate(hits).action == "answer":
            policy_false_answers += 1
        diagnostics.append({"id": case["id"], "expected": [], "retrieved": locators})
    return {
        "answered_cases": len(answered),
        "no_answer_cases": len(no_answer),
        "recall_at_3": recalled / len(answered) if answered else 0.0,
        "mrr_at_3": reciprocal / len(answered) if answered else 0.0,
        "no_answer_threshold_proxy_rate": false_answers / len(no_answer) if no_answer else 0.0,
        "threshold_proxy_count": false_answers,
        "no_answer_policy_false_positive_rate": policy_false_answers / len(no_answer) if no_answer else 0.0,
        "policy_false_positive_count": policy_false_answers,
        "case_diagnostics": diagnostics,
        "exact_overlap_scope": "retrieval_cases versus compiled aliases and question_bank seeds",
    }


def paid_capture(payload: dict, service: CustomerService, output: Path) -> None:
    if not os.environ.get("LLM_API_KEY"):
        raise SystemExit("--paid requires LLM_API_KEY")
    records = []
    for case in payload["conversation_cases"]:
        history = []
        outputs = []
        for message in case["messages"]:
            if message["role"] == "user":
                result = service.chat(message["content"], history=history, tone=case["tone"])
                outputs.append(result)
                history.extend(({"role": "user", "content": message["content"]},
                                {"role": "assistant", "content": result["answer"]}))
        records.append({"id": case["id"], "outputs": outputs})
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--paid", action="store_true", help="make paid API calls for the 50 conversation cases")
    parser.add_argument("--paid-output", type=Path, default=ROOT / "paid_holdout_results.json")
    args = parser.parse_args()
    payload = load_fixture(args.fixture)
    if len(payload.get("conversation_cases", [])) != 50:
        raise SystemExit("conversation_cases must contain exactly 50 cases")
    overlaps = exact_seed_overlaps(payload)
    if overlaps:
        raise SystemExit(f"holdout queries exactly overlap alias seeds: {', '.join(overlaps)}")
    with tempfile.TemporaryDirectory() as directory:
        store = KnowledgeStore(Path(directory) / "holdout.db")
        try:
            ingest_jsonl(store, KNOWLEDGE, expected_access_level="internal_coaching")
            retriever = Retriever(store)
            metrics = evaluate(payload, retriever)
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
            if args.paid:
                service = CustomerService(store, retriever, PolicyEngine(minimum_score=.72), AnswerEngine())
                paid_capture(payload, service, args.paid_output)
                print(f"paid results: {args.paid_output}")
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
