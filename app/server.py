import hmac
import json
import mimetypes
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .answer import AnswerEngine
from .ingest import ingest_jsonl
from .policy import PolicyEngine
from .retrieval import Retriever
from .service import CustomerService
from .storage import KnowledgeStore


def load_pipeline_stats(knowledge_path: Path) -> dict:
    candidates = [knowledge_path.parent / "manifest.json"]
    if knowledge_path.parent.name == "rag":
        candidates.insert(0, knowledge_path.parent.parent / "manifest.json")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        statuses = payload.get("status_counts", {})
        return {
            "source_files": int(payload.get("source_files", 0)),
            "markdown_files": int(payload.get("markdown_files", 0)),
            "conversation_cases": int(payload.get("conversation_cases", 0)),
            "protected_files": int(statuses.get("protected", 0)),
        }
    return {}


@dataclass
class AppContext:
    store: KnowledgeStore
    service: CustomerService
    retriever: Retriever
    knowledge_path: Path
    static_dir: Path
    admin_token: str
    profile: str = "customer_service"
    access_level: str = "customer_service"
    app_name: str = "張副總 AI 客服"
    assistant_name: str = "AI 客服"
    welcome_prompts: tuple[str, ...] = ()
    pipeline_stats: dict | None = None
    max_request_bytes: int = 65536

    @classmethod
    def create(
        cls,
        db_path: str | Path,
        knowledge_path: str | Path,
        static_dir: str | Path,
        admin_token: str,
        policy_path: str | Path | None = None,
        minimum_score: float = 0.72,
        top_k: int = 6,
        profile: str = "customer_service",
        access_level: str = "customer_service",
        app_name: str = "張副總 AI 客服",
        assistant_name: str = "AI 客服",
        welcome_prompts: tuple[str, ...] = (),
        blocked_topics: dict | None = None,
        fallback_message: str | None = None,
    ) -> "AppContext":
        store = KnowledgeStore(db_path)
        knowledge = Path(knowledge_path)
        if store.count_chunks() == 0 and knowledge.is_file():
            ingest_jsonl(store, knowledge, expected_access_level=access_level)
        retriever = Retriever(store)
        service = CustomerService(
            store=store,
            retriever=retriever,
            policy=PolicyEngine(
                minimum_score=minimum_score,
                blocked_topics=blocked_topics,
                **({"fallback_message": fallback_message} if fallback_message else {}),
            ),
            answerer=AnswerEngine(policy_path=policy_path),
            top_k=top_k,
        )
        return cls(
            store=store,
            service=service,
            retriever=retriever,
            knowledge_path=knowledge,
            static_dir=Path(static_dir),
            admin_token=admin_token,
            profile=profile,
            access_level=access_level,
            app_name=app_name,
            assistant_name=assistant_name,
            welcome_prompts=welcome_prompts,
            pipeline_stats=load_pipeline_stats(knowledge),
        )

    def close(self) -> None:
        self.store.close()


def create_server(host: str, port: int, context: AppContext) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ZhangRAG/1.0"

        def log_message(self, format_string: str, *args) -> None:
            if os.getenv("APP_QUIET") != "1":
                super().log_message(format_string, *args)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("無效的 Content-Length") from exc
            if size <= 0 or size > context.max_request_bytes:
                raise ValueError("請求內容大小不符合限制")
            try:
                payload = json.loads(self.rfile.read(size))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("請求必須是有效 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON 必須是物件")
            return payload

        def _is_admin(self) -> bool:
            supplied = self.headers.get("X-Admin-Token", "")
            return bool(context.admin_token) and hmac.compare_digest(supplied, context.admin_token)

        def _require_admin(self) -> bool:
            if self._is_admin():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "message": "管理權杖無效"})
            return False

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._json(HTTPStatus.OK, {
                    "status": "ok", "chunks": context.store.count_chunks(),
                    "model_enabled": context.service.answerer.model_enabled,
                    "profile": context.profile,
                    "app_name": context.app_name,
                    "assistant_name": context.assistant_name,
                    "welcome_prompts": list(context.welcome_prompts),
                })
                return
            if parsed.path == "/api/admin/stats":
                if self._require_admin():
                    stats = context.store.stats()
                    stats["pipeline"] = context.pipeline_stats or {}
                    self._json(HTTPStatus.OK, stats)
                return
            if parsed.path == "/api/admin/audits":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": context.store.list_audits(100)})
                return
            if parsed.path == "/api/admin/chunks":
                if not self._require_admin():
                    return
                query = parse_qs(parsed.query).get("q", [""])[0]
                if query.strip():
                    items = [hit.citation() for hit in context.retriever.retrieve(query, limit=50)]
                else:
                    items = context.store.list_chunks(100)
                self._json(HTTPStatus.OK, {"items": items})
                return
            if parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._serve_static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/api/chat":
                    result = context.service.chat(payload.get("message", ""), payload.get("conversation_id"))
                    self._json(HTTPStatus.OK, result)
                    return
                if parsed.path == "/api/admin/reindex":
                    if not self._require_admin():
                        return
                    report = ingest_jsonl(
                        context.store,
                        context.knowledge_path,
                        expected_access_level=context.access_level,
                    )
                    self._json(HTTPStatus.OK, {
                        "imported": report.imported, "rejected": report.rejected,
                        "errors": report.errors,
                    })
                    return
                if parsed.path == "/api/admin/retrieve":
                    if not self._require_admin():
                        return
                    hits = context.retriever.retrieve(payload.get("message", ""), limit=6)
                    self._json(HTTPStatus.OK, {"items": [hit.citation() for hit in hits]})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(exc)})
            except (OSError, json.JSONDecodeError):
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "服務暫時無法處理請求"})

        def _serve_static(self, request_path: str) -> None:
            relative = "index.html" if request_path == "/" else request_path.lstrip("/")
            target = (context.static_dir / relative).resolve()
            static_root = context.static_dir.resolve()
            if static_root not in target.parents and target != static_root:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not target.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
