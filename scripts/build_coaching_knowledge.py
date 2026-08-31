#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path


SECTION_PATTERN = re.compile(
    r"^## \[(?P<locator>coach-\d+) \| (?P<category>[^\]]+)\] (?P<title>.+)$",
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
        raise ValueError("找不到任何輔導知識區段")
    return sections


def build_rows(
    source_path: str | Path,
    evidence_name: str,
    evidence_sha256: str,
    reviewed_at: str,
) -> list[dict]:
    source = Path(source_path)
    raw = source.read_bytes()
    markdown = raw.decode("utf-8")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    source_label = f"knowledge/{source.name}"
    rows = []
    for section in parse_sections(markdown):
        locator = section["locator"]
        rows.append({
            "chunk_id": f"designer-coaching-process:{locator}:1",
            "doc_id": "designer-coaching-process",
            "locator": locator,
            "section_title": section["section_title"],
            "text": section["text"],
            "title": "設計師 1 對 1 輔導流程",
            "source_file": source_label,
            "source_sha256": source_sha256,
            "evidence_source": evidence_name,
            "evidence_sha256": evidence_sha256,
            "category": section["category"],
            "access_level": "internal_coaching",
            "rag_allowed": True,
            "review_status": "approved",
            "reviewer": "AI 專家初審（依使用者授權）",
            "reviewed_at": reviewed_at,
        })
    return rows


def write_jsonl(rows: list[dict], output_path: str | Path) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    output.write_text(body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="編譯設計師輔導 Markdown 為 RAG JSONL")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--evidence-name", required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--reviewed-at", required=True)
    args = parser.parse_args()

    rows = build_rows(
        args.source,
        evidence_name=args.evidence_name,
        evidence_sha256=args.evidence_sha256,
        reviewed_at=args.reviewed_at,
    )
    write_jsonl(rows, args.output)
    print(json.dumps({"chunks": len(rows), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
