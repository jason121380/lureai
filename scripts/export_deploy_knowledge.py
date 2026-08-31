#!/usr/bin/env python3
"""Create a public-deployable RAG JSONL from the reviewed private index."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domains import domain_of
from scripts.build_full_knowledge import (
    ADDRESS_PATTERN,
    DISTRICT_ADDRESS_PATTERN,
    PRIVATE_PATTERNS,
    sanitize_deployable_text,
    sanitize_message,
)


NAME_BEFORE_TITLE = re.compile(
    r"(?m)(?<![\u3400-\u9fff])([\u3400-\u9fff]{2,4})[ \t]+"
    r"(?:副總(?:經理)?|部長|處長|經理|董事長)(?=$|[^\u3400-\u9fff])"
)
BRACKETED_SPECIALIST = re.compile(r"《([\u3400-\u9fff]{2,4})(?:老師|設計師)》")
ROLE_BEFORE_NAME = re.compile(
    r"(?:講師|市長參選人|髮型師|老師|經理|副總|董事長|董娘)\s*[:：]?\s*([\u3400-\u9fff]{2,4})"
)
VOCATIVE_NAME = re.compile(
    r"(?<![\u3400-\u9fff])([\u3400-\u9fff]{2,4})(?=你(?:要|可以|想|再))"
)
LATIN_ALIAS = re.compile(r"(?<![A-Za-z])#?([A-Z][A-Za-z]{2,24})(?![A-Za-z])")
LEADERSHIP_TITLES = {"董事長", "副總", "副總經理", "部長", "處長", "經理"}
ROLE_LABELS = LEADERSHIP_TITLES | {
    "講師", "老師", "髮型師", "董娘", "市長參選人",
    "教練", "設計師", "顧問", "店長", "副店長", "助理", "學員",
}
# Common acronyms and tool names the Latin-alias pattern would otherwise
# mistake for personal names (e.g. "SOP" became "[人名]").
LATIN_STOPWORDS = {
    "sop", "ai", "ig", "fb", "line", "google", "youtube", "meta", "facebook",
    "instagram", "threads", "tiktok", "pos", "kol", "dm", "ga", "ga4", "qa",
    "excel", "word", "canva", "capcut", "iphone", "android", "gpt", "chatgpt",
    "messenger", "rag", "ai", "sms", "vr", "ar", "ph",
}
SAFE_SOURCE_PATH = re.compile(
    r"^(?:knowledge/[^/]+|private_sources/conversations/case-[0-9]{4}-[0-9a-f]{16}\.md|"
    r"source_documents/[0-9a-f]{16}\.md)$"
)


def collect_deploy_names(rows: list[dict]) -> set[str]:
    names = set()
    for row in rows:
        text = "\n".join(
            str(row.get(field, ""))
            for field in ("text", "title", "section_title", "locator")
        )
        for pattern in (
            NAME_BEFORE_TITLE,
            BRACKETED_SPECIALIST,
            ROLE_BEFORE_NAME,
            VOCATIVE_NAME,
        ):
            names.update(pattern.findall(text))
        names.update(LATIN_ALIAS.findall(text))
        lines = [line.strip() for line in text.splitlines()]
        for title, candidate in zip(lines, lines[1:]):
            if title in LEADERSHIP_TITLES and re.fullmatch(r"[\u3400-\u9fff]{2,4}", candidate):
                names.add(candidate)
    return {
        name for name in names - ROLE_LABELS
        if name.lower() not in LATIN_STOPWORDS
    }


def deployable_row(row: dict, names: set[str] | None = None) -> dict | None:
    searchable = f"{row.get('section_title', '')}\n{row.get('text', '')}"
    if any(marker in searchable for marker in ("部門分機", "聯絡名冊", "通訊錄", "人員名單")):
        return None
    identity_markers = sum(marker in searchable for marker in ("姓名", "生日", "電話", "地址", "身分證", "家屬"))
    if str(row.get("chunk_id", "")).startswith("source-doc:") and (
        identity_markers >= 3 or "人員姓名" in searchable or ("姓名" in searchable and "出勤" in searchable)
    ):
        return None
    output = dict(row)
    # Rows sourced from knowledge/ are human-reviewed public content; masking
    # them again only destroys role words like 教練/設計師/SOP.
    already_reviewed = str(output.get("source_file", "")).startswith("knowledge/")
    if not already_reviewed:
        for field in ("text", "title", "section_title", "locator"):
            output[field] = sanitize_deployable_text(
                sanitize_message(str(output.get(field, "")), names or set())
            )
    # A masked name at the start of a title ("[人名] 1 對 1 輔導流程") makes
    # every citation look identical; the title works without it.
    output["title"] = re.sub(r"^\[人名\]\s*", "", str(output.get("title", "")))
    if str(output.get("chunk_id", "")).startswith("source-doc:"):
        digest = str(output.get("source_sha256", ""))[:16] or "unknown"
        category = str(output.get("category", "企業知識"))
        output["title"] = f"歷史教材：{category}"
        output["source_file"] = f"source_documents/{digest}.md"
    # Every exported chunk belongs to one of the two knowledge domains.
    output["domain"] = domain_of(output)
    return output


def validate_deployable_rows(rows: list[dict], names: set[str]) -> list[str]:
    errors = []
    for row in rows:
        chunk_id = str(row.get("chunk_id", "unknown"))
        public_text = "\n".join(
            str(row.get(field, ""))
            for field in ("text", "title", "section_title", "locator")
        )
        source_file = str(row.get("source_file", ""))
        if any(pattern.search(public_text) for pattern in PRIVATE_PATTERNS):
            errors.append(f"{chunk_id}: private contact data")
        if ADDRESS_PATTERN.search(public_text) or DISTRICT_ADDRESS_PATTERN.search(public_text):
            errors.append(f"{chunk_id}: private address data")
        if any(
            name and (
                bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", public_text, re.I))
                if re.fullmatch(r"[A-Za-z0-9 ._/'-]+", name)
                else name in public_text
            )
            for name in names
        ):
            errors.append(f"{chunk_id}: detected personal name")
        if not SAFE_SOURCE_PATH.fullmatch(source_file):
            errors.append(f"{chunk_id}: unsafe source path")
        if any(marker in public_text for marker in ("部門分機", "聯絡名冊", "通訊錄", "人員名單")):
            errors.append(f"{chunk_id}: private directory data")
    return errors


def export_jsonl(source: Path, destination: Path) -> int:
    source_rows = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    names = collect_deploy_names(source_rows)
    rows = []
    for source_row in source_rows:
        row = deployable_row(source_row, names)
        if row is not None:
            rows.append(row)
    errors = validate_deployable_rows(rows, names)
    if errors:
        raise ValueError("部署知識隱私驗證失敗：" + "; ".join(errors[:10]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(export_jsonl(args.source, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
