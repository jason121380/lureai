#!/usr/bin/env python3
"""用問法索引量測檢索覆蓋率：每個問法有沒有撈到它對應的那塊知識。"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.ingest import ingest_jsonl
from app.policy import PolicyEngine
from app.retrieval import Retriever
from app.storage import KnowledgeStore


def run(knowledge: Path, bank: Path, top_n: int = 3) -> dict:
    sections = json.loads(bank.read_text(encoding="utf-8")).get("sections", {})
    with tempfile.TemporaryDirectory() as directory:
        store = KnowledgeStore(Path(directory) / "coverage.db")
        try:
            ingest_jsonl(store, knowledge, expected_access_level="internal_coaching")
            retriever = Retriever(store)
            policy = PolicyEngine()
            total = hit = above = 0
            misses = []
            for locator, questions in sections.items():
                for question in questions:
                    total += 1
                    hits = retriever.retrieve(question, limit=top_n)
                    locators = [item.locator for item in hits]
                    if hits and hits[0].score >= policy.minimum_score:
                        above += 1
                    if locator in locators:
                        hit += 1
                    else:
                        misses.append({
                            "question": question,
                            "expected": locator,
                            "got": locators,
                            "score": round(hits[0].score, 3) if hits else 0,
                        })
        finally:
            store.close()
    return {
        "questions": total,
        "hit_rate": round(hit / total, 4) if total else 0,
        "above_threshold": round(above / total, 4) if total else 0,
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge", type=Path, default=PROJECT_ROOT / "knowledge" / "designer_coaching_process.jsonl")
    parser.add_argument("--bank", type=Path, default=PROJECT_ROOT / "config" / "question_bank.json")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--show", type=int, default=15)
    args = parser.parse_args()

    report = run(args.knowledge, args.bank, args.top)
    print(json.dumps({k: v for k, v in report.items() if k != "misses"}, ensure_ascii=False))
    for miss in report["misses"][: args.show]:
        print(f"  MISS {miss['expected']:<10} {miss['question']}  → {miss['got']} ({miss['score']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
