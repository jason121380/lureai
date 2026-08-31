#!/usr/bin/env python3
"""Verify full private extraction, deidentification, indexes, and RAG boundaries."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.retrieval import Retriever  # noqa: E402
from app.storage import KnowledgeStore  # noqa: E402
from scripts.build_full_knowledge import ADDRESS_PATTERN, DISTRICT_ADDRESS_PATTERN, collect_names  # noqa: E402


EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
URL = re.compile(r"https?://\S+", re.I)
MOBILE = re.compile(r"(?<!\d)(?:\+?886[- ]?)?0?9\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)")


def jsonl_rows(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{line_number} 不是有效 JSON") from exc
        rows.append(row)
    return rows


def name_is_present(name: str, text: str) -> bool:
    if any("\u3400" <= char <= "\u9fff" for char in name):
        return name in text
    flags = re.I if len(name) >= 3 else 0
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, flags))


def top_score(db_path: Path, question: str) -> float:
    store = KnowledgeStore(db_path)
    try:
        hits = Retriever(store).retrieve(question, limit=1)
        return hits[0].score if hits else 0.0
    finally:
        store.close()


def verify(full_root: Path, customer_db: Path, coach_db: Path) -> dict:
    manifest = json.loads((full_root / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    conversations = manifest["conversations"]
    source_markdown = [full_root / item["markdown"] for item in files]
    conversation_markdown = [full_root / item["markdown"] for item in conversations]
    conversation_text = "\n".join(path.read_text(encoding="utf-8") for path in conversation_markdown)

    source_json = Path(manifest["source_root"]) / "lurebot-conversations-20260831.json"
    original = json.loads(source_json.read_text(encoding="utf-8"))
    names = collect_names(original["conversations"])
    original_ids = {str(item.get("conv_id", "")) for item in original["conversations"]}
    leaked_names = sorted(name for name in names if name_is_present(name, conversation_text))
    leaked_ids = sorted(value for value in original_ids if value and value in conversation_text)

    customer_rows = jsonl_rows(full_root / "rag" / "customer_service_full.jsonl")
    coach_rows = jsonl_rows(full_root / "rag" / "designer_coach_full.jsonl")
    historical_rows = [row for row in coach_rows if row.get("category") == "歷史輔導案例"]
    required = {"chunk_id", "locator", "text", "title", "source_file", "access_level", "review_status"}
    customer_valid = all(required <= row.keys() and row["access_level"] == "customer_service" for row in customer_rows)
    coach_valid = all(required <= row.keys() and row["access_level"] == "internal_coaching" for row in coach_rows)

    scores = {
        "customer_aftercare": top_score(customer_db, "燙後怎麼保養？"),
        "customer_unrelated_weather": top_score(customer_db, "明天天氣怎麼樣？"),
        "coach_private_message": top_score(coach_db, "設計師私訊很多但預約很少，先查什麼？"),
        "coach_unrelated_weather": top_score(coach_db, "明天天氣怎麼樣？"),
    }
    checks = {
        "source_manifest_matches": manifest["source_files"] == len(files) == 267,
        "every_source_has_markdown": all(path.is_file() for path in source_markdown),
        "conversation_manifest_matches": manifest["conversation_cases"] == len(conversations) == 270,
        "every_conversation_has_markdown": all(path.is_file() for path in conversation_markdown),
        "markdown_total_matches": manifest["markdown_files"] == len(source_markdown) + len(conversation_markdown),
        "no_failed_extractions": not any(item["status"] == "failed" for item in files),
        "no_email_in_conversations": EMAIL.search(conversation_text) is None,
        "no_url_in_conversations": URL.search(conversation_text) is None,
        "no_mobile_in_conversations": MOBILE.search(conversation_text) is None,
        "no_address_in_conversations": ADDRESS_PATTERN.search(conversation_text) is None
        and DISTRICT_ADDRESS_PATTERN.search(conversation_text) is None,
        "no_raw_conversation_ids": not leaked_ids,
        "no_known_names": not leaked_names,
        "customer_jsonl_valid": customer_valid and len(customer_rows) == manifest["customer_service_chunks"],
        "coach_jsonl_valid": coach_valid and len(coach_rows) == manifest["designer_coach_chunks"],
        "historical_rag_numbers_masked": all(
            "非現行標準" in row["text"]
            and not re.search(r"[$＄]\s*\d", row["text"])
            and not re.search(r"\d+(?:\.\d+)?%", row["text"])
            for row in historical_rows
        ),
        "customer_relevant_above_threshold": scores["customer_aftercare"] >= 0.72,
        "coach_relevant_above_threshold": scores["coach_private_message"] >= 0.72,
        "customer_unrelated_below_threshold": scores["customer_unrelated_weather"] < 0.72,
        "coach_unrelated_below_threshold": scores["coach_unrelated_weather"] < 0.72,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": all(checks.values()),
        "checks": checks,
        "counts": {
            "source_files": len(files),
            "source_markdown": len(source_markdown),
            "conversation_markdown": len(conversation_markdown),
            "total_markdown": len(source_markdown) + len(conversation_markdown),
            "customer_chunks": len(customer_rows),
            "coach_chunks": len(coach_rows),
            "protected_files": manifest["status_counts"].get("protected", 0),
            "corrupt_or_unknown": manifest["status_counts"].get("corrupt_or_unknown", 0),
            "leaked_names": len(leaked_names),
            "leaked_ids": len(leaked_ids),
        },
        "retrieval_scores": scores,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-root", type=Path, default=PROJECT_ROOT / "private_sources" / "full")
    parser.add_argument("--customer-db", type=Path, default=PROJECT_ROOT / "data" / "knowledge.db")
    parser.add_argument("--coach-db", type=Path, default=PROJECT_ROOT / "data" / "designer_coach.db")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "qa" / "full_knowledge_verification.json")
    args = parser.parse_args()
    report = verify(args.full_root, args.customer_db, args.coach_db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
