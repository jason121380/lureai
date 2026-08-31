#!/usr/bin/env python3
"""把所有人工整理的知識手冊編譯成一份 RAG 索引。

每本手冊是 Markdown，區段格式固定為 `## [locator | 分類] 標題`。
編譯時會替每個區段產生「問法索引」（aliases）：同一塊知識的各種口語問句，
只用於檢索比對，不會被當成答案輸出。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domains import COACHING, OPERATIONS


PLAYBOOKS = [
    {
        "path": "knowledge/designer_coaching_process.md",
        "prefix": "coach",
        "doc_id": "designer-coaching-process",
        "title": "設計師 1 對 1 輔導流程",
        "domain": COACHING,
        "evidence": "lurebot-conversations-20260831.json",
    },
    {
        "path": "knowledge/messaging_audit_playbook.md",
        "prefix": "chat",
        "doc_id": "messaging-audit-playbook",
        "title": "私訊對話健檢與成交手冊",
        "domain": COACHING,
        "evidence": "一對一輔導對話健檢整理",
    },
    {
        "path": "knowledge/ads_playbook.md",
        "prefix": "ads",
        "doc_id": "ads-playbook",
        "title": "設計師廣告投放手冊",
        "domain": COACHING,
        "evidence": "一對一輔導投放紀錄整理",
    },
    {
        "path": "knowledge/social_playbook.md",
        "prefix": "social",
        "doc_id": "social-playbook",
        "title": "設計師社群與版面輔導手冊",
        "domain": COACHING,
        "evidence": "一對一輔導版面健檢整理",
    },
    {
        "path": "knowledge/session_playbook.md",
        "prefix": "session",
        "doc_id": "session-playbook",
        "title": "一對一輔導流程手冊",
        "domain": COACHING,
        "evidence": "一對一輔導流程整理",
    },
    {
        "path": "knowledge/salon_operations_playbook.md",
        "prefix": "ops",
        "doc_id": "salon-operations-playbook",
        "title": "店務營運管理手冊",
        "domain": OPERATIONS,
        "evidence": "source_documents（130 份門市教材）",
    },
]

QUESTION_BANK = PROJECT_ROOT / "config" / "question_bank.json"

# 每一個問法模板都保留原本的主題詞，所以展開後仍然只對應到同一塊知識。
QUESTION_TEMPLATES = (
    "{}",
    "{}怎麼做",
    "{}要怎麼做",
    "{}怎麼辦",
    "{}該怎麼處理",
    "{}要注意什麼",
    "{}有哪些重點",
    "{}的標準是什麼",
    "{}要多久",
    "{}是什麼意思",
    "怎麼{}",
    "如何{}",
    "我想知道{}",
    "我的{}怎麼改",
    "{}要從哪裡開始",
    "{}做不好怎麼辦",
    "{}有沒有範例",
    "{}要準備什麼",
    "教我{}",
    "{}的做法",
)

CLAUSE_SPLIT = re.compile(r"[，。；：、（）()「」『』,.;:!?？！]")
LEADING_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)]|第[一二三四五六七八九十]+[、.])\s*")
STOP_PHRASES = {"這是", "不要", "可以", "如果", "因為", "所以", "例如", "而且", "但是", "然後"}


def section_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"^## \[(?P<locator>{prefix}-\d+) \| (?P<category>[^\]]+)\] (?P<title>.+)$",
        re.MULTILINE,
    )


def parse_sections(markdown: str, prefix: str) -> list[dict]:
    matches = list(section_pattern(prefix).finditer(markdown))
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
        raise ValueError(f"找不到任何 {prefix}-* 知識區段")
    return sections


def key_phrases(section: dict, limit: int = 6) -> list[str]:
    """從標題與每一點的開頭抓出這塊知識在講什麼。"""
    phrases: list[str] = []
    title = CLAUSE_SPLIT.split(section["section_title"])[0].strip()
    if title:
        phrases.append(title)
    for raw_line in section["text"].splitlines():
        line = LEADING_MARKER.sub("", raw_line.strip())
        if not line:
            continue
        clause = CLAUSE_SPLIT.split(line)[0].strip()
        clause = clause.strip("「」『』 ")
        if 3 <= len(clause) <= 14 and clause not in phrases and clause[:2] not in STOP_PHRASES:
            phrases.append(clause)
        if len(phrases) >= limit:
            break
    return phrases


def build_aliases(section: dict, seeds: list[str], cap: int = 60) -> list[str]:
    aliases: list[str] = []

    def add(value: str) -> None:
        cleaned = " ".join(str(value).split())
        if cleaned and cleaned not in aliases:
            aliases.append(cleaned)

    for seed in seeds:
        add(seed)
    for phrase in key_phrases(section):
        for template in QUESTION_TEMPLATES:
            add(template.format(phrase))
            if len(aliases) >= cap:
                return aliases[:cap]
    add(section["category"])
    return aliases[:cap]


def load_question_bank() -> dict:
    if not QUESTION_BANK.is_file():
        return {}
    payload = json.loads(QUESTION_BANK.read_text(encoding="utf-8"))
    sections = payload.get("sections") if isinstance(payload, dict) else payload
    return sections if isinstance(sections, dict) else {}


def build_rows(reviewed_at: str) -> list[dict]:
    bank = load_question_bank()
    rows: list[dict] = []
    for playbook in PLAYBOOKS:
        source = PROJECT_ROOT / playbook["path"]
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        for section in parse_sections(raw.decode("utf-8"), playbook["prefix"]):
            locator = section["locator"]
            aliases = build_aliases(section, bank.get(locator, []))
            rows.append({
                "chunk_id": f"{playbook['doc_id']}:{locator}:1",
                "doc_id": playbook["doc_id"],
                "locator": locator,
                "section_title": section["section_title"],
                "text": section["text"],
                "title": playbook["title"],
                "source_file": f"knowledge/{source.name}",
                "source_sha256": digest,
                "evidence_source": playbook["evidence"],
                "category": section["category"],
                "domain": playbook["domain"],
                "aliases": aliases,
                "access_level": "internal_coaching",
                "rag_allowed": True,
                "review_status": "approved",
                "reviewer": "AI 重點整理（依使用者授權）",
                "reviewed_at": reviewed_at,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="編譯所有知識手冊為 RAG JSONL")
    parser.add_argument("output", type=Path)
    parser.add_argument("--reviewed-at", required=True)
    args = parser.parse_args()

    rows = build_rows(args.reviewed_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "chunks": len(rows),
        "aliases": sum(len(row["aliases"]) for row in rows),
        "playbooks": len(PLAYBOOKS),
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
