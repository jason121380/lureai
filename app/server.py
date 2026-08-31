import hmac
import hashlib
import ipaddress
import itertools
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from .answer import AnswerEngine
from .auth import AuthManager, LoginRateLimiter, RequestRateLimiter
from .curation import quality_report
from .domains import DOMAIN_LABELS, classify, is_domain
from .health import build_health_report
from .ingest import ingest_jsonl
from .policy import PolicyEngine
from .retrieval import Retriever
from .service import CustomerService
from .storage import KnowledgeStore
from .usage import UsagePricing


SESSION_COOKIE = "hairbrain_session"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
LONG_CACHE_SUFFIXES = {".png", ".svg", ".ico", ".woff", ".woff2", ".webmanifest"}

CUSTOM_SOURCE_FILE = "knowledge/admin_authored.md"
MAX_KNOWLEDGE_TEXT = 8000


def build_custom_chunk(payload: dict, access_level: str) -> dict:
    """Turn admin form input into an approved chunk the retriever can index."""
    from .text_utils import search_tokens

    title = " ".join(str(payload.get("title", "")).split())
    section_title = " ".join(str(payload.get("section_title", "")).split())
    category = " ".join(str(payload.get("category", "")).split())
    domain = " ".join(str(payload.get("domain", "")).split())
    text = str(payload.get("text", "")).strip()
    if not section_title:
        raise ValueError("標題不可為空")
    if len(section_title) > 80:
        raise ValueError("標題不可超過 80 個字")
    if domain and not is_domain(domain):
        raise ValueError("主題只能是店務營運管理或設計師一對一行銷輔導")
    if not text:
        raise ValueError("內容不可為空")
    if len(text) > MAX_KNOWLEDGE_TEXT:
        raise ValueError(f"內容不可超過 {MAX_KNOWLEDGE_TEXT} 個字")
    chunk_id = str(payload.get("chunk_id", "")).strip()
    if chunk_id and not chunk_id.startswith("admin:"):
        raise ValueError("只能編輯後台建立的知識")
    if not chunk_id:
        chunk_id = f"admin:{uuid4().hex[:12]}"
    searchable = " ".join([title or "後台新增知識", section_title, category, text])
    return {
        "chunk_id": chunk_id,
        "doc_id": "admin-authored",
        "locator": chunk_id.split(":", 1)[1],
        "section_title": section_title,
        "text": text,
        "title": title or "後台新增知識",
        "source_file": CUSTOM_SOURCE_FILE,
        "source_sha256": "",
        "category": category or "後台新增",
        "domain": domain or classify(category),
        "access_level": access_level,
        "rag_allowed": True,
        "review_status": "approved",
        "reviewer": "管理後台",
        "reviewed_at": datetime.now(timezone.utc).date().isoformat(),
        "search_text": " ".join(search_tokens(searchable)),
    }


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
    auth: AuthManager
    pricing: UsagePricing
    login_limiter: LoginRateLimiter
    chat_limiter: RequestRateLimiter = field(default_factory=RequestRateLimiter)
    profile: str = "customer_service"
    access_level: str = "customer_service"
    app_name: str = "張副總 AI 客服"
    assistant_name: str = "AI 客服"
    welcome_prompts: tuple[str, ...] = ()
    pipeline_stats: dict | None = None
    max_request_bytes: int = 65536
    started_at: float = field(default_factory=time.monotonic)

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
        knowledge_digest = hashlib.sha256(knowledge.read_bytes()).hexdigest() if knowledge.is_file() else ""
        indexed_digest = store.get_metadata("knowledge_sha256")
        indexed_access_level = store.get_metadata("knowledge_access_level")
        needs_reindex = (
            store.count_chunks() == 0
            or indexed_digest != knowledge_digest
            or indexed_access_level != access_level
        )
        if knowledge.is_file() and needs_reindex:
            try:
                ingest_jsonl(store, knowledge, expected_access_level=access_level)
            except Exception:
                store.close()
                raise
        retriever = Retriever(store)
        pricing = UsagePricing.from_env()
        auth = AuthManager(store)
        login_limiter = LoginRateLimiter()
        bootstrap_username = os.getenv("USER_USERNAME", "").strip()
        bootstrap_password = os.getenv("USER_PASSWORD", "")
        if bootstrap_username or bootstrap_password:
            if not bootstrap_username or not bootstrap_password:
                store.close()
                raise ValueError("USER_USERNAME 與 USER_PASSWORD 必須同時設定")
            auth.ensure_bootstrap_user(
                bootstrap_username, bootstrap_password,
                role=os.getenv("USER_ROLE", "").strip() or None,
            )
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
            pricing=pricing,
        )
        return cls(
            store=store,
            service=service,
            retriever=retriever,
            knowledge_path=knowledge,
            static_dir=Path(static_dir),
            admin_token=admin_token,
            auth=auth,
            pricing=pricing,
            login_limiter=login_limiter,
            chat_limiter=RequestRateLimiter(
                max_requests=int(os.getenv("CHAT_RATE_LIMIT_PER_MINUTE", "20") or 20),
                window_seconds=60,
            ),
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
        # Drop slow or stalled connections so they cannot pin worker threads.
        timeout = 30

        def log_message(self, format_string: str, *args) -> None:
            if os.getenv("APP_QUIET") != "1":
                super().log_message(format_string, *args)

        def _send_security_headers(self) -> None:
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)

        def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            for key, value in (headers or {}).items():
                self.send_header(key, value)
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
            if supplied and bool(context.admin_token) and hmac.compare_digest(supplied, context.admin_token):
                return True
            user = self._current_user()
            return bool(user and user.get("role") == "admin")

        def _require_admin(self) -> bool:
            if self._is_admin():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "message": "管理權杖無效"})
            return False

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            try:
                peer_address = ipaddress.ip_address(peer)
            except ValueError:
                return peer
            if peer_address.is_private or peer_address.is_loopback:
                forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
                if forwarded:
                    try:
                        ipaddress.ip_address(forwarded)
                        return forwarded
                    except ValueError:
                        pass
            return peer

        def _session_token(self) -> str:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return ""
            morsel = cookie.get(SESSION_COOKIE)
            return morsel.value if morsel else ""

        def _current_user(self) -> dict | None:
            return context.auth.authenticate(self._session_token())

        def _require_user(self) -> dict | None:
            user = self._current_user()
            if user:
                return user
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "authentication_required", "message": "請先登入"},
            )
            return None

        def _session_cookie(self, token: str, max_age: int) -> str:
            forwarded_https = (
                self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip() == "https"
            )
            host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
            secure = forwarded_https or host not in {"localhost", "127.0.0.1", "::1"}
            parts = [
                f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                f"Max-Age={max_age}",
            ]
            if secure:
                parts.append("Secure")
            return "; ".join(parts)

        def _usage_summary(self, user_id: int) -> dict:
            now = datetime.now(ZoneInfo("Asia/Taipei"))
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if month_start.month == 12:
                next_month = month_start.replace(year=month_start.year + 1, month=1)
            else:
                next_month = month_start.replace(month=month_start.month + 1)
            totals = context.store.usage_totals(
                user_id,
                month_start.astimezone(timezone.utc).isoformat(),
                next_month.astimezone(timezone.utc).isoformat(),
            )
            return context.pricing.summary(month=f"{now.year:04d}-{now.month:02d}", **totals)

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
            if parsed.path == "/api/auth/me":
                user = self._require_user()
                if user:
                    self._json(HTTPStatus.OK, {"user": user})
                return
            if parsed.path == "/api/usage":
                user = self._require_user()
                if user:
                    self._json(HTTPStatus.OK, self._usage_summary(user["id"]))
                return
            if parsed.path == "/api/admin/users":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": context.auth.list_users()})
                return
            if parsed.path == "/api/admin/stats":
                if self._require_admin():
                    stats = context.store.stats()
                    stats["pipeline"] = context.pipeline_stats or {}
                    stats["composition"] = context.store.knowledge_composition()
                    stats["domain_labels"] = DOMAIN_LABELS
                    self._json(HTTPStatus.OK, stats)
                return
            if parsed.path == "/api/admin/knowledge/detail":
                if not self._require_admin():
                    return
                chunk_id = parse_qs(parsed.query).get("chunk_id", [""])[0].strip()
                chunk = context.store.get_chunk(chunk_id)
                if not chunk:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                self._json(HTTPStatus.OK, {"chunk": {
                    "chunk_id": chunk["chunk_id"],
                    "section_title": chunk["section_title"],
                    "category": chunk["category"],
                    "domain": chunk.get("domain", ""),
                    "text": chunk["text"],
                    "origin": chunk.get("origin", "file"),
                }})
                return
            if parsed.path == "/api/admin/knowledge/quality":
                if self._require_admin():
                    chunks = context.store.list_chunks(limit=100000)
                    self._json(HTTPStatus.OK, quality_report(chunks))
                return
            if parsed.path == "/api/admin/knowledge/export":
                if not self._require_admin():
                    return
                payloads = context.store.all_chunk_payloads()
                body = "\n".join(
                    json.dumps(payload, ensure_ascii=False) for payload in payloads
                ).encode("utf-8") + b"\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Disposition", 'attachment; filename="knowledge-export.jsonl"'
                )
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/admin/health":
                if self._require_admin():
                    self._json(HTTPStatus.OK, build_health_report(context))
                return
            if parsed.path == "/api/admin/chunks":
                if not self._require_admin():
                    return
                params = parse_qs(parsed.query)
                query = params.get("q", [""])[0].strip()
                origin = params.get("origin", [""])[0].strip()
                domain = params.get("domain", [""])[0].strip()
                origin = origin if origin in ("file", "custom") else ""
                domain = domain if is_domain(domain) else ""
                if query:
                    hits = context.retriever.retrieve(query, limit=60)
                    found = {hit.chunk_id for hit in hits}
                    items = [
                        chunk
                        for chunk in context.store.list_chunks(
                            limit=100000, origin=origin, domain=domain
                        )
                        if chunk["chunk_id"] in found
                    ]
                else:
                    items = context.store.list_chunks(limit=200, origin=origin, domain=domain)
                # Send only what the list renders: the full rows carry
                # metadata_json and search_text, which made this a 1.5 MB reply.
                self._json(HTTPStatus.OK, {"items": [
                    {
                        "chunk_id": item["chunk_id"],
                        "title": item["title"],
                        "section_title": item["section_title"],
                        "category": item["category"],
                        "domain": item.get("domain", ""),
                        "locator": item["locator"],
                        "origin": item.get("origin", "file"),
                        "text": str(item["text"])[:400],
                        "length": len(str(item["text"])),
                    }
                    for item in items[:200]
                ]})
                return
            if parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._serve_static(parsed.path)

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin", "")
            if not origin:
                return True
            origin_host = urlparse(origin).netloc.strip().lower()
            host = self.headers.get("Host", "").strip().lower()
            return not origin_host or not host or origin_host == host

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not self._same_origin():
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden", "message": "來源網域不符"})
                return
            try:
                payload = self._read_json()
                if parsed.path == "/api/auth/login":
                    username = str(payload.get("username", "")).strip().casefold()
                    login_key = f"{self._client_ip()}|{username}"
                    if not context.login_limiter.allowed(login_key):
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "too_many_attempts", "message": "登入嘗試過多，請稍後再試"},
                            {"Retry-After": "300"},
                        )
                        return
                    try:
                        token, user = context.auth.login(
                            payload.get("username", ""), payload.get("password", "")
                        )
                    except ValueError:
                        context.login_limiter.failed(login_key)
                        self._json(
                            HTTPStatus.UNAUTHORIZED,
                            {"error": "invalid_credentials", "message": "帳號或密碼錯誤"},
                        )
                        return
                    context.login_limiter.succeeded(login_key)
                    self._json(
                        HTTPStatus.OK,
                        {"user": user},
                        {"Set-Cookie": self._session_cookie(token, context.auth.session_days * 86400)},
                    )
                    return
                if parsed.path == "/api/auth/logout":
                    context.auth.logout(self._session_token())
                    self._json(
                        HTTPStatus.OK,
                        {"status": "ok"},
                        {"Set-Cookie": self._session_cookie("", 0)},
                    )
                    return
                if parsed.path == "/api/chat":
                    user = self._require_user()
                    if not user:
                        return
                    if not context.chat_limiter.allow(f"user:{user['id']}"):
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "rate_limited", "message": "訊息傳送太頻繁，請稍候再試"},
                            {"Retry-After": "30"},
                        )
                        return
                    usage_summary = self._usage_summary(user["id"])
                    within_budget = (
                        usage_summary["budget_twd"] <= 0
                        or usage_summary["spend_twd"] < usage_summary["budget_twd"]
                    )
                    result = context.service.chat(
                        payload.get("message", ""),
                        payload.get("conversation_id"),
                        payload.get("history"),
                        user_id=user["id"],
                        allow_model=within_budget,
                    )
                    self._json(HTTPStatus.OK, result)
                    return
                if parsed.path == "/api/chat/stream":
                    user = self._require_user()
                    if not user:
                        return
                    if not context.chat_limiter.allow(f"user:{user['id']}"):
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "rate_limited", "message": "訊息傳送太頻繁，請稍候再試"},
                            {"Retry-After": "30"},
                        )
                        return
                    usage_summary = self._usage_summary(user["id"])
                    within_budget = (
                        usage_summary["budget_twd"] <= 0
                        or usage_summary["spend_twd"] < usage_summary["budget_twd"]
                    )
                    events = context.service.chat_stream(
                        payload.get("message", ""),
                        payload.get("conversation_id"),
                        payload.get("history"),
                        user_id=user["id"],
                        allow_model=within_budget,
                    )
                    # Validation errors must surface as JSON before the stream starts.
                    try:
                        first_event = next(events)
                    except StopIteration:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "服務暫時無法處理請求"})
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Accel-Buffering", "no")
                    self._send_security_headers()
                    self.end_headers()
                    try:
                        for event in itertools.chain([first_event], events):
                            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if parsed.path == "/api/chat/title":
                    user = self._require_user()
                    if not user:
                        return
                    if not context.chat_limiter.allow(f"user:{user['id']}"):
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "rate_limited", "message": "請求太頻繁，請稍候再試"},
                            {"Retry-After": "30"},
                        )
                        return
                    usage_summary = self._usage_summary(user["id"])
                    within_budget = (
                        usage_summary["budget_twd"] <= 0
                        or usage_summary["spend_twd"] < usage_summary["budget_twd"]
                    )
                    result = context.service.summarize_title(
                        payload.get("message", ""),
                        payload.get("answer", ""),
                        conversation_id=payload.get("conversation_id"),
                        user_id=user["id"],
                        allow_model=within_budget,
                    )
                    self._json(HTTPStatus.OK, result)
                    return
                if parsed.path == "/api/admin/users":
                    if not self._require_admin():
                        return
                    user = context.auth.create_or_reset_user(
                        payload.get("username", ""), payload.get("password", ""),
                        role=payload.get("role"),
                    )
                    self._json(HTTPStatus.OK, {"user": user})
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
                if parsed.path == "/api/admin/knowledge":
                    if not self._require_admin():
                        return
                    chunk = build_custom_chunk(payload, context.access_level)
                    context.store.upsert_custom_chunk(chunk)
                    self._json(HTTPStatus.OK, {"chunk": {
                        "chunk_id": chunk["chunk_id"],
                        "section_title": chunk["section_title"],
                        "category": chunk["category"],
                        "domain": chunk["domain"],
                    }})
                    return
                if parsed.path == "/api/admin/knowledge/delete":
                    if not self._require_admin():
                        return
                    chunk_id = str(payload.get("chunk_id", "")).strip()
                    if not context.store.delete_custom_chunk(chunk_id):
                        self._json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "not_found", "message": "只能刪除後台建立的知識"},
                        )
                        return
                    self._json(HTTPStatus.OK, {"status": "deleted", "chunk_id": chunk_id})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(exc)})
            except (OSError, json.JSONDecodeError):
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "服務暫時無法處理請求"})

        CLEAN_ROUTES = {"/": "index.html", "/admin": "admin.html", "/chat": "index.html"}

        def _serve_static(self, request_path: str) -> None:
            normalized = request_path.rstrip("/") or "/"
            relative = self.CLEAN_ROUTES.get(normalized) or request_path.lstrip("/")
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
            etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
            long_cache = relative.startswith("vendor/") or target.suffix.lower() in LONG_CACHE_SUFFIXES
            cache_control = "public, max-age=86400" if long_cache else "no-cache"
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache_control)
                self._send_security_headers()
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self._send_security_headers()
            if content_type == "text/html":
                self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
