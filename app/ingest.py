import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .storage import KnowledgeStore
from .text_utils import search_tokens


REQUIRED_FIELDS = ("chunk_id", "locator", "text", "title", "source_file")


@dataclass(frozen=True)
class IngestReport:
    imported: int
    rejected: int
    errors: list[str] = field(default_factory=list)


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def validate_chunk(
    row: dict,
    expected_access_level: str = "customer_service",
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(row, dict):
        return False, ["row must be an object"]
    for field_name in REQUIRED_FIELDS:
        if not normalize_text(row.get(field_name, "")):
            errors.append(f"missing {field_name}")
    legacy_customer_approval = (
        expected_access_level == "customer_service"
        and row.get("customer_service_allowed") is True
    )
    if row.get("rag_allowed") is not True and not legacy_customer_approval:
        errors.append("rag_allowed must be true")
    if row.get("review_status") != "approved":
        errors.append("review_status must be approved")
    if row.get("access_level") != expected_access_level:
        errors.append(f"access_level must be {expected_access_level}")
    return not errors, errors


def _search_text(row: dict) -> str:
    original = normalize_text(" ".join([
        row.get("title", ""),
        row.get("section_title", ""),
        row.get("category", ""),
        row.get("text", ""),
    ]))
    return " ".join(search_tokens(original))


def ingest_jsonl(
    store: KnowledgeStore,
    path: str | Path,
    expected_access_level: str = "customer_service",
) -> IngestReport:
    source = Path(path)
    accepted: list[dict] = []
    errors: list[str] = []
    rejected = 0

    with source.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                rejected += 1
                errors.append(f"line {line_number}: invalid JSON ({exc.msg})")
                continue
            valid, row_errors = validate_chunk(row, expected_access_level=expected_access_level)
            if not valid:
                rejected += 1
                errors.append(f"line {line_number}: {', '.join(row_errors)}")
                continue
            prepared = dict(row)
            prepared["search_text"] = _search_text(prepared)
            accepted.append(prepared)

    if errors:
        raise ValueError(f"知識檔包含 {rejected} 筆未核准或無效資料")
    if not accepted:
        raise ValueError("知識檔沒有可匯入的核准資料")
    store.replace_chunks(accepted)
    store.set_metadata("knowledge_sha256", hashlib.sha256(source.read_bytes()).hexdigest())
    store.set_metadata("knowledge_access_level", expected_access_level)
    return IngestReport(imported=len(accepted), rejected=rejected, errors=errors)
