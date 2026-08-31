import hashlib
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .ingest import validate_chunk


FRONTEND_ASSETS = (
    "index.html",
    "admin.html",
    "app.css",
    "chat.js",
    "admin.js",
    "vendor/lucide.min.js",
)


def _chunk_fingerprint(payload: dict) -> str:
    normalized = {key: value for key, value in payload.items() if key != "search_text"}
    serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _timed_check(check_id: str, label: str, operation) -> dict:
    started = time.perf_counter()
    try:
        status, message, details = operation()
    except Exception as exc:  # A health report must survive one failed dependency.
        status = "error"
        message = "檢查失敗"
        details = {"error_type": type(exc).__name__}
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "message": message,
        "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
        "details": details,
    }


def _server_check(context) -> tuple[str, str, dict]:
    uptime = max(0, round(time.monotonic() - context.started_at))
    return "ok", "伺服器程序正常運作", {
        "profile": context.profile,
        "python": platform.python_version(),
        "uptime_seconds": uptime,
    }


def _api_check(context) -> tuple[str, str, dict]:
    service = context.service
    policy = getattr(service, "policy", None)
    retriever = getattr(service, "retriever", None)
    answerer = getattr(service, "answerer", None)
    store = getattr(service, "store", None)
    service_chain = {
        "chat": getattr(service, "chat", None),
        "policy_precheck": getattr(policy, "precheck", None),
        "policy_evaluate": getattr(policy, "evaluate", None),
        "retrieval": getattr(retriever, "retrieve", None),
        "answer": getattr(answerer, "answer", None),
        "audit": getattr(store, "add_audit", None),
    }
    unavailable = [name for name, operation in service_chain.items() if not callable(operation)]
    mismatched = []
    if store is not context.store:
        mismatched.append("store")
    if retriever is not context.retriever:
        mismatched.append("retriever")
    details = {
        "admin_auth": bool(context.admin_token),
        "max_request_bytes": context.max_request_bytes,
        "service_chain": len(service_chain) - len(unavailable),
    }
    if unavailable or mismatched or not context.admin_token:
        return "error", "API 服務鏈未完整就緒", {
            **details,
            "unavailable": unavailable,
            "mismatched": mismatched,
        }
    return "ok", "健康端點與聊天服務鏈已就緒", details


def _frontend_check(context) -> tuple[str, str, dict]:
    markers = {
        "index.html": ('id="prompt"', "chat.js"),
        "admin.html": ('id="admin-shell"', "admin.js"),
        "app.css": (".chat-main", ".admin-shell"),
        "chat.js": ("/api/chat",),
        "admin.js": ("/api/admin/health",),
        "vendor/lucide.min.js": ("lucide",),
    }
    missing = []
    unreadable = []
    invalid = []
    total_bytes = 0
    for name in FRONTEND_ASSETS:
        path = context.static_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(name)
            continue
        total_bytes += len(content.encode("utf-8"))
        if not content or any(marker not in content for marker in markers[name]):
            invalid.append(name)
    if missing or unreadable or invalid:
        return "error", "前端資源不完整", {
            "assets": len(FRONTEND_ASSETS),
            "missing": missing,
            "unreadable": unreadable,
            "invalid": invalid,
        }
    return "ok", "首頁與管理後台資源可讀且結構完整", {
        "assets": len(FRONTEND_ASSETS),
        "bytes": total_bytes,
    }


def _database_check(context) -> tuple[str, str, dict]:
    details = context.store.health_check()
    if details["integrity"] != "ok" or not details["writable"]:
        return "error", "SQLite 完整性或寫入檢查失敗", details
    return "ok", "SQLite 可讀且儲存目錄可寫", {
        "integrity": details["integrity"],
        "writable": details["writable"],
        "size_bytes": details["size_bytes"],
    }


def _rag_check(context) -> tuple[str, str, dict]:
    index = context.store.index_health()
    chunks = index["chunks"]
    fts_chunks = index["fts_chunks"]
    if chunks <= 0:
        return "error", "RAG 索引沒有可用知識", {**index, "probe_hits": 0}
    first = context.store.list_chunks(1)[0]
    probe = f"{first.get('title', '')} {first.get('text', '')[:80]}".strip()
    probe_hits = len(context.retriever.retrieve(probe, limit=1))
    details = {**index, "probe_hits": probe_hits}
    if chunks != fts_chunks or probe_hits == 0:
        return "error", "RAG 索引與檢索探針不一致", details
    return "ok", "RAG 索引可檢索", details


def _knowledge_check(context) -> tuple[str, str, dict]:
    path = Path(context.knowledge_path)
    if not path.is_file():
        return "error", "知識來源檔不存在", {"records": 0, "invalid_records": 0}
    records = 0
    invalid = 0
    accepted = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                valid, _ = validate_chunk(payload, expected_access_level=context.access_level)
                if not valid:
                    invalid += 1
                else:
                    accepted.append(payload)
            except json.JSONDecodeError:
                invalid += 1
            records += 1
    source_chunks = {str(payload["chunk_id"]): _chunk_fingerprint(payload) for payload in accepted}
    indexed_payloads = context.store.indexed_chunks_for_health()
    indexed_chunks = {
        str(payload["chunk_id"]): _chunk_fingerprint(payload)
        for payload in indexed_payloads
    }
    source_ids = set(source_chunks)
    indexed_ids = set(indexed_chunks)
    duplicates = len(accepted) - len(source_ids)
    changed = sum(
        source_chunks[chunk_id] != indexed_chunks[chunk_id]
        for chunk_id in source_ids & indexed_ids
    )
    in_sync = source_chunks == indexed_chunks and invalid == 0 and duplicates == 0
    details = {
        "records": records,
        "approved_records": len(accepted),
        "invalid_records": invalid,
        "duplicate_chunk_ids": duplicates,
        "indexed_records": len(indexed_ids),
        "missing_from_index": len(source_ids - indexed_ids),
        "extra_in_index": len(indexed_ids - source_ids),
        "changed_records": changed,
        "in_sync": in_sync,
        "size_bytes": path.stat().st_size,
    }
    if records == 0 or not in_sync:
        return "error", "知識來源與目前 RAG 索引不同步", details
    return "ok", "知識來源已核准且與索引同步", details


def _llm_check(context) -> tuple[str, str, dict]:
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    configured = {"base_url": bool(base_url), "api_key": bool(api_key), "model": bool(model)}
    details = {
        "mode": "llm" if all(configured.values()) else "extractive",
        "configured": configured,
    }
    if model:
        details["model"] = model
    if base_url:
        details["provider_host"] = urlparse(base_url).hostname or "invalid"
    if all(configured.values()) and details["provider_host"] != "invalid":
        return "ok", "LLM 設定完整，連線會在回答時驗證", details
    if any(configured.values()):
        return "warning", "LLM 設定不完整，目前使用抽取式回答", details
    return "warning", "未設定 LLM，目前使用抽取式回答", details


def build_health_report(context) -> dict:
    checks = [
        _timed_check("server", "Server", lambda: _server_check(context)),
        _timed_check("api", "API", lambda: _api_check(context)),
        _timed_check("frontend", "Frontend", lambda: _frontend_check(context)),
        _timed_check("database", "Database", lambda: _database_check(context)),
        _timed_check("rag", "RAG", lambda: _rag_check(context)),
        _timed_check("knowledge", "Knowledge", lambda: _knowledge_check(context)),
        _timed_check("llm", "LLM", lambda: _llm_check(context)),
    ]
    summary = {
        status: sum(check["status"] == status for check in checks)
        for status in ("ok", "warning", "error")
    }
    summary["total"] = len(checks)
    overall = "error" if summary["error"] else "warning" if summary["warning"] else "ok"
    return {
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "checks": checks,
    }
