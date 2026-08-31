#!/usr/bin/env python3
"""把人工整理過的店務營運手冊編譯成 RAG JSONL。

原始教材是掃描 OCR 與試算表傾印，無法直接當知識引用；
`knowledge/salon_operations_playbook.md` 是重點整理版，這支腳本負責編譯。
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domains import OPERATIONS


SECTION_PATTERN = re.compile(
    r"^## \[(?P<locator>ops-\d+) \| (?P<category>[^\]]+)\] (?P<title>.+)$",
    re.MULTILINE,
)


def parse_sections(markdown: str) -> list[dict]:
    matches = list(SECTION_PATTERN.finditer(markdown))
    sections: list[dict] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        locator = match.group("locator").strip()
        if locator in seen:
            raise ValueError(f"重複的知識定位：{locator}")
        seen.add(locator)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        text = markdown[match.end():end].strip()
        if not text:
            raise ValueError(f"知識區段沒有內容：{locator}")
        sections.append({
            "locator": locator,
            "category": match.group("category").strip(),
            "section_title": match.group("title").strip(),
            "text": text,
        })
    if not sections:
        raise ValueError("找不到任何店務營運知識區段")
    return sections


def build_rows(source_path: str | Path, reviewed_at: str) -> list[dict]:
    source = Path(source_path)
    raw = source.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    source_label = f"knowledge/{source.name}"
    rows = []
    for section in parse_sections(raw.decode("utf-8")):
        locator = section["locator"]
        rows.append({
            "chunk_id": f"salon-operations-playbook:{locator}:1",
            "doc_id": "salon-operations-playbook",
            "locator": locator,
            "section_title": section["section_title"],
            "text": section["text"],
            "title": "店務營運管理手冊",
            "source_file": source_label,
            "source_sha256": source_sha256,
            "evidence_source": "source_documents（130 份門市教材）",
            "category": section["category"],
            "domain": OPERATIONS,
            "access_level": "internal_coaching",
            "rag_allowed": True,
            "review_status": "approved",
            "reviewer": "AI 重點整理（依使用者授權）",
            "reviewed_at": reviewed_at,
        })
    return rows


def write_jsonl(rows: list[dict], output_path: str | Path) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    output.write_text(body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="編譯店務營運手冊為 RAG JSONL")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reviewed-at", required=True)
    args = parser.parse_args()

    rows = build_rows(args.source, reviewed_at=args.reviewed_at)
    write_jsonl(rows, args.output)
    print(json.dumps({"chunks": len(rows), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
