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


def validate_chunk(row: dict) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(row, dict):
        return False, ["row must be an object"]
    for field_name in REQUIRED_FIELDS:
        if not normalize_text(row.get(field_name, "")):
            errors.append(f"missing {field_name}")
    if row.get("customer_service_allowed") is not True:
        errors.append("customer_service_allowed must be true")
    if row.get("review_status") != "approved":
        errors.append("review_status must be approved")
    if row.get("access_level") != "customer_service":
        errors.append("access_level must be customer_service")
    return not errors, errors


def _search_text(row: dict) -> str:
    original = normalize_text(" ".join([
        row.get("title", ""),
        row.get("section_title", ""),
        row.get("category", ""),
        row.get("text", ""),
    ]))
    return " ".join(search_tokens(original))


def ingest_jsonl(store: KnowledgeStore, path: str | Path) -> IngestReport:
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
            valid, row_errors = validate_chunk(row)
            if not valid:
                rejected += 1
                errors.append(f"line {line_number}: {', '.join(row_errors)}")
                continue
            prepared = dict(row)
            prepared["search_text"] = _search_text(prepared)
            accepted.append(prepared)

    store.replace_chunks(accepted)
    return IngestReport(imported=len(accepted), rejected=rejected, errors=errors)
