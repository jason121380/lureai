import hmac
import hashlib
import ipaddress
import itertools
import base64
import binascii
import json
import mimetypes
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from .answer import SMALLTALK_KINDS, AnswerEngine
from .auth import AuthManager, LoginRateLimiter, RequestRateLimiter
from .curation import quality_report
from . import documents, extract
from . import tuning
from .followups import welcome_questions

# 單條規則的長度上限：夠寫一段完整的話，但擋掉整份文件貼進來。
TUNING_RULE_MAX_CHARS = 4000

# 對話紀錄存伺服器（使用者決定），這幾個上限只是防呆，不讓單一帳號無限長大。
CONVERSATION_KEEP = 100
CONVERSATION_MAX_MESSAGES = 200
CONVERSATION_MAX_CHARS = 20000
CONVERSATION_TITLE_MAX = 120

# 後台總覽的回覆品質指標算最近幾天（太長會被舊資料稀釋，看不出改動有沒有效）。
REPLY_METRIC_DAYS = 30

# 開場題庫一次全部送給前端，讓每次抽題都從整個池子隨機。
WELCOME_PROMPT_POOL = 100

# 允許同步到伺服器的個人偏好（白名單，避免前端亂塞東西）。
USER_PREF_KEYS = {"tone"}
from .domains import DOMAIN_LABELS, classify, is_domain
from .humanize import (
    DELAY_RANGE,
    MESSAGE_GAP_RANGE,
    message_gaps,
    context_instruction,
    postprocess,
    reply_delay,
    strip_citations,
)
from .health import build_health_report
from . import ingest as ingest_module
from .ingest import ingest_jsonl
from .policy import BOUNDARY_REPLIES, PolicyEngine
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

# lurebot 走機器對機器的入口，用這個服務帳號記帳與稽核（不給人登入）。
BOT_SERVICE_USERNAME = "lurebot"
MAX_BOT_HISTORY = 8

CUSTOM_SOURCE_FILE = "knowledge/admin_authored.md"
MAX_KNOWLEDGE_TEXT = 8000
# 拖進來的單一檔案上限。再大就該先拆檔，不然一次分析要等太久。
MAX_UPLOAD_CHARS = 60000
# 中文一個字 3 個位元組，6 萬字約 180KB；Word／PDF 走 base64 會再放大 4/3，
# 加上 JSON 外殼抓 8MB（只有這條端點，其他路徑照舊）。
UPLOAD_REQUEST_BYTES = 8 * 1024 * 1024


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
    bot_token: str = ""
    bot_user_id: int | None = None
    chat_limiter: RequestRateLimiter = field(default_factory=RequestRateLimiter)
    profile: str = "customer_service"
    access_level: str = "customer_service"
    app_name: str = "張副總 AI 客服"
    assistant_name: str = "AI 客服"
    welcome_prompts: tuple[str, ...] = ()
    pipeline_stats: dict | None = None
    max_request_bytes: int = 65536
    started_at: float = field(default_factory=time.monotonic)
    # Postgres 持久化（run.py 接上；沒設定時維持 None，健康檢查會顯示未設定）。
    replica: object | None = None
    restored_from_replica: bool = False

    @classmethod
    def create(
        cls,
        db_path: str | Path,
        knowledge_path: str | Path,
        static_dir: str | Path,
        admin_token: str,
        bot_token: str = "",
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
            # 索引欄位的格式改了也要重建：知識檔的雜湊沒變，只靠它偵測不到。
            or store.get_metadata("index_format") != ingest_module.INDEX_FORMAT
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
        bot_user_id = None
        if bot_token:
            # 服務帳號的密碼沒人需要知道；有了 user_id，用量、月預算與稽核才記得到帳。
            bot_user_id = auth.ensure_bootstrap_user(
                BOT_SERVICE_USERNAME, secrets.token_urlsafe(32)
            )["id"]
        # 後台「AI 模型校調」改過的規則：每次組指令時重讀，存檔後下一則就生效。
        rules_provider = store.model_rules
        service = CustomerService(
            store=store,
            retriever=retriever,
            policy=PolicyEngine(
                minimum_score=minimum_score,
                blocked_topics=blocked_topics,
                rules_provider=rules_provider,
                **({"fallback_message": fallback_message} if fallback_message else {}),
            ),
            answerer=AnswerEngine(policy_path=policy_path, rules_provider=rules_provider),
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
            bot_token=bot_token,
            bot_user_id=bot_user_id,
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
        # HTTP/1.1 才有 keep-alive。用預設的 1.0 時，每一個靜態檔（CSS、JS、
        # logo、icon）都要重開一條 TCP 連線，在雲端還要多一次 TLS 交握；一頁
        # 六七個檔就是六七次交握，加上 backlog 只有 5，同時湧進來就有人被丟掉
        # ——症狀正是「HTML 出來了但 CSS 沒有、分頁一直轉」。
        # 改 1.1 的前提：每個回應都要讓瀏覽器知道 body 到哪裡結束。目前只有
        # 串流那條沒有 Content-Length，它自己送 Connection: close（見下面）。
        protocol_version = "HTTP/1.1"
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

        def _read_json(self, max_bytes: int | None = None) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("無效的 Content-Length") from exc
            if size <= 0 or size > (max_bytes or context.max_request_bytes):
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

        def _save_conversations(self, user_id: int, payload: dict) -> int:
            """把前端送上來的對話寫進資料庫；一次可以送一段或整批（登入時的搬家）。"""
            raw = payload.get("conversations")
            if raw is None:
                raw = [payload.get("conversation") or payload]
            if not isinstance(raw, list):
                return 0
            now = datetime.now(timezone.utc).isoformat()
            saved = 0
            for item in raw[:CONVERSATION_KEEP]:
                if not isinstance(item, dict):
                    continue
                conversation_id = str(item.get("id", "")).strip()
                if not conversation_id:
                    continue
                messages = item.get("messages")
                messages = messages if isinstance(messages, list) else []
                trimmed = []
                for message in messages[-CONVERSATION_MAX_MESSAGES:]:
                    if not isinstance(message, dict):
                        continue
                    clean = dict(message)
                    clean.pop("loading", None)
                    clean.pop("pendingReveal", None)
                    clean["content"] = str(clean.get("content", ""))[:CONVERSATION_MAX_CHARS]
                    trimmed.append(clean)
                context.store.save_conversation(
                    user_id,
                    conversation_id,
                    str(item.get("title", ""))[:CONVERSATION_TITLE_MAX],
                    str(item.get("tone", ""))[:20],
                    trimmed,
                    str(item.get("createdAt") or now),
                    str(item.get("updatedAt") or now),
                )
                saved += 1
            if saved:
                context.store.prune_conversations(user_id, keep=CONVERSATION_KEEP)
            return saved

        def _fixed_replies(self) -> dict:
            """固定回覆句的預設值（供校調頁顯示與還原）。"""
            policy = context.service.policy
            return {
                "reply-fallback": policy._fallback_message,
                "reply-sensitive": policy._sensitive_message,
                "reply-model_failed": context.service.answerer.MODEL_FAILED_MESSAGE,
                **{f"reply-{reason}": message for reason, _terms, message in BOUNDARY_REPLIES},
            }

        def _smalltalk_rules(self) -> dict:
            return {
                rule_id: default
                for _kind, (rule_id, fallback_id, instruction, fallback) in SMALLTALK_KINDS.items()
                for rule_id, default in ((rule_id, instruction), (fallback_id, fallback))
            }

        def _line_delivery(self) -> dict:
            low, high = DELAY_RANGE
            gap_low, gap_high = MESSAGE_GAP_RANGE
            return {
                "delivery-delay": f"{low:g}-{high:g}",
                "delivery-gap": f"{gap_low:g}-{gap_high:g}",
            }

        def _catalogue_defaults(self) -> dict:
            return {
                "fixed_replies": self._fixed_replies(),
                "smalltalk_rules": self._smalltalk_rules(),
                "line_delivery": self._line_delivery(),
            }

        def _require_admin(self) -> bool:
            if self._is_admin():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "message": "管理權杖無效"})
            return False

        def _is_bot(self) -> bool:
            supplied = self.headers.get("X-Bot-Token", "")
            return bool(
                supplied
                and context.bot_token
                and hmac.compare_digest(supplied, context.bot_token)
            )

        def _require_bot(self) -> bool:
            if self._is_bot():
                return True
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthorized", "message": "機器人權杖無效"},
            )
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
                    # 整份題庫都送出去（順序已洗過），前端每次開空白對話再抽五題。
                    "welcome_prompts": welcome_questions(
                        limit=WELCOME_PROMPT_POOL, fallback=context.welcome_prompts
                    ),
                })
                return
            if parsed.path.startswith("/api/bot/"):
                if not self._require_bot():
                    return
                if parsed.path == "/api/bot/health":
                    usage_summary = self._usage_summary(context.bot_user_id) if context.bot_user_id else {}
                    self._json(HTTPStatus.OK, {
                        "status": "ok",
                        "chunks": context.store.count_chunks(),
                        "model_enabled": context.service.answerer.model_enabled,
                        "model": context.service.answerer.model_name,
                        "profile": context.profile,
                        "app_name": context.app_name,
                        "indexed_at": context.store.get_metadata("knowledge_indexed_at") or "",
                        "usage": usage_summary,
                    })
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if parsed.path == "/api/auth/me":
                user = self._require_user()
                if user:
                    self._json(HTTPStatus.OK, {"user": user})
                return
            if parsed.path == "/api/conversations":
                user = self._require_user()
                if user:
                    self._json(HTTPStatus.OK, {
                        "conversations": context.store.list_conversations(
                            user["id"], limit=CONVERSATION_KEEP
                        ),
                        "prefs": context.store.user_prefs(user["id"]),
                    })
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
            if parsed.path == "/api/admin/feedback":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": context.store.list_feedback(limit=200)})
                return
            if parsed.path == "/api/admin/stats":
                if self._require_admin():
                    stats = context.store.stats()
                    stats["pipeline"] = context.pipeline_stats or {}
                    stats["composition"] = context.store.knowledge_composition()
                    stats["domain_labels"] = DOMAIN_LABELS
                    stats["replies"] = context.store.reply_metrics(
                        (datetime.now(timezone.utc) - timedelta(days=REPLY_METRIC_DAYS)).isoformat()
                    )
                    stats["replies"]["window_days"] = REPLY_METRIC_DAYS
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
            if parsed.path == "/api/admin/tuning":
                if not self._require_admin():
                    return
                overrides = context.store.model_rules()
                groups = []
                for group in tuning.catalogue(**self._catalogue_defaults()):
                    rules = []
                    for rule in group["rules"]:
                        override = overrides.get(rule["id"], "")
                        rules.append({
                            "id": rule["id"],
                            "label": rule["label"],
                            "hint": rule.get("hint", ""),
                            "text": override or rule["text"],
                            "default_text": rule["text"],
                            "customized": bool(override),
                        })
                    groups.append({
                        "id": group["id"], "label": group["label"],
                        "hint": group.get("hint", ""), "rules": rules,
                    })
                self._json(HTTPStatus.OK, {
                    "groups": groups,
                    "customized": sum(1 for group in groups for rule in group["rules"] if rule["customized"]),
                })
                return
            if parsed.path == "/api/admin/tuning/preview":
                # 「改完後變成 AI 看得懂的」——這裡回傳實際會送給模型的整段指令。
                if not self._require_admin():
                    return
                tone = (parse_qs(parsed.query).get("tone", [""])[0] or "expert").strip()
                self._json(HTTPStatus.OK, {
                    "tone": tone,
                    "instructions": context.service.answerer.instructions(tone),
                })
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
                    # 沒搜尋時列出全部：知識庫超過 200 塊後，硬上限會讓清單「滑到底就沒了」。
                    items = context.store.list_chunks(limit=100000, origin=origin, domain=domain)
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
                    for item in items
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

        def _bot_reply(self, payload: dict) -> None:
            """lurebot 的唯一入口：檢索、政策、生成、引用守門、稽核全部照舊，
            只有輸出換成「可以直接送進 LINE 的幾則短訊息」。"""
            conversation_id = str(payload.get("conversation_id", "")).strip()[:120]
            # 沒帶 conversation_id 時退回群組名稱，再退回發話者；全部共用一個
            # 「unknown」桶的話，一個群組講太快會讓其他群組一起被擋。
            bot_context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
            limiter_key = conversation_id or " ".join(
                str(bot_context.get("group_name") or bot_context.get("speaker") or "unknown").split()
            )[:120]
            if not context.chat_limiter.allow(f"bot:{limiter_key}"):
                self._json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "rate_limited", "message": "訊息傳送太頻繁，請稍候再試"},
                    {"Retry-After": "30"},
                )
                return
            history = payload.get("history")
            if isinstance(history, list):
                history = history[-MAX_BOT_HISTORY:]
            usage_summary = self._usage_summary(context.bot_user_id) if context.bot_user_id else {}
            within_budget = (
                not usage_summary
                or usage_summary["budget_twd"] <= 0
                or usage_summary["spend_twd"] < usage_summary["budget_twd"]
            )
            result = context.service.chat(
                payload.get("message", ""),
                conversation_id or None,
                history,
                user_id=context.bot_user_id,
                allow_model=within_budget,
                tone="line",
                context_note=context_instruction(payload.get("context")),
                want_followups=False,
            )
            response = {
                "trace_id": result["trace_id"],
                "conversation_id": result.get("conversation_id"),
                "status": result["status"],
                "reason": result["reason"],
                "messages": [],
                "delay_seconds": 0.0,
                "answer": "",
                "citations": result.get("citations", []),
                "answer_mode": result.get("answer_mode", ""),
                "model_status": result.get("model_status", ""),
            }
            if result["status"] != "answered":
                # 敏感題與低信心一律不自動回，交還真人；lurebot 收到就安靜。
                self._json(HTTPStatus.OK, response)
                return
            if result.get("answer_mode") not in ("llm", "boundary", "smalltalk"):
                # 模型沒生成成功時的降級回覆不是真人講得出來的話，不送進群組；
                # 邊界題（問身分、離題、不當請求）與閒聊／情緒的回答本來就是寫給
                # 通訊軟體的短句，照送——群組裡有人打招呼卻已讀不回最傷。
                response["status"] = "unavailable"
                response["reason"] = result.get("model_status") or "model_unavailable"
                self._json(HTTPStatus.OK, response)
                return
            messages = postprocess(result["answer"])
            if not messages:
                response["status"] = "unavailable"
                response["reason"] = "empty_answer"
                self._json(HTTPStatus.OK, response)
                return
            response["messages"] = messages
            response["answer"] = strip_citations(result["answer"]).strip()
            rules = context.store.model_rules()
            response["delay_seconds"] = reply_delay(
                delay_range=tuning.parse_delay_range(
                    rules.get("delivery-delay", ""), DELAY_RANGE
                )
            )
            # 每一則之間再等一小段，訊息才會一則一則出現而不是同時跳出來。
            response["message_gaps"] = message_gaps(
                len(response.get("messages") or []),
                gap_range=tuning.parse_delay_range(
                    rules.get("delivery-gap", ""), MESSAGE_GAP_RANGE
                ),
            )
            self._json(HTTPStatus.OK, response)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not self._same_origin():
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden", "message": "來源網域不符"})
                return
            try:
                # 上傳分析要送整份文件進來，一般的 64KB 上限（約兩萬個中文字）
                # 不夠用。這條只有 admin 打得到，所以單獨放寬，其他路徑照舊。
                payload = self._read_json(
                    UPLOAD_REQUEST_BYTES if parsed.path == "/api/admin/knowledge/analyze" else None
                )
                if parsed.path.startswith("/api/bot/"):
                    if not self._require_bot():
                        return
                    if parsed.path == "/api/bot/reply":
                        self._bot_reply(payload)
                        return
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
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
                        tone=payload.get("tone"),
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
                        tone=payload.get("tone"),
                    )
                    # 驗證錯誤要在串流開始前用 JSON 回覆；service 會先 yield 一個
                    # start 事件，所以這裡幾乎立刻返回，header 不會被模型生成卡住。
                    try:
                        first_event = next(events)
                    except StopIteration:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "服務暫時無法處理請求"})
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Accel-Buffering", "no")
                    # 邊生成邊寫，長度事先不知道，所以這條走「寫完就關連線」。
                    # HTTP/1.1 底下沒有這兩行，瀏覽器會一直等下一則而不收尾。
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self._send_security_headers()
                    self.end_headers()
                    try:
                        for event in itertools.chain([first_event], events):
                            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if parsed.path == "/api/feedback":
                    user = self._require_user()
                    if not user:
                        return
                    trace_id = str(payload.get("trace_id", "")).strip()
                    rating = str(payload.get("rating", "")).strip()
                    if not trace_id or len(trace_id) > 64 or rating not in ("up", "down"):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": "回饋格式無效"})
                        return
                    context.store.add_feedback(
                        trace_id, user["id"], rating,
                        datetime.now(timezone.utc).isoformat(),
                    )
                    self._json(HTTPStatus.OK, {"status": "ok"})
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
                if parsed.path == "/api/admin/knowledge/analyze":
                    if not self._require_admin():
                        return
                    # 拖進來的檔案由前端讀成文字再送上來（避免 multipart，也不必存檔）。
                    # 一次只分析一份，前端才能一份一份顯示進度。
                    name = " ".join(str(payload.get("name", "")).split())[:120]
                    # 純文字檔前端直接讀成字串；Word／Excel／PDF 這種二進位檔
                    # 用 base64 送上來，在這裡才拆成文字（拆檔要用標準庫的 zipfile）。
                    if payload.get("data_base64"):
                        try:
                            raw = base64.b64decode(str(payload["data_base64"]), validate=True)
                        except (ValueError, binascii.Error):
                            self._json(HTTPStatus.BAD_REQUEST, {
                                "error": "invalid_request", "message": "檔案內容送不上來，請再試一次"})
                            return
                        try:
                            text = documents.extract_text(name, raw)
                        except documents.UnreadableDocument as exc:
                            self._json(HTTPStatus.BAD_REQUEST, {
                                "error": "invalid_request", "message": str(exc)})
                            return
                    else:
                        text = str(payload.get("text", ""))
                    if not text.strip():
                        self._json(HTTPStatus.BAD_REQUEST, {
                            "error": "invalid_request", "message": "這個檔案讀不到文字內容"})
                        return
                    if len(text) > MAX_UPLOAD_CHARS:
                        self._json(HTTPStatus.BAD_REQUEST, {
                            "error": "invalid_request",
                            "message": f"單一檔案不可超過 {MAX_UPLOAD_CHARS} 個字，請先拆開"})
                        return
                    # 用權杖打進來的沒有 session 使用者，那就不做個人預算檢查。
                    admin = self._current_user()
                    usage_summary = self._usage_summary(admin["id"]) if admin else {}
                    within_budget = (
                        not usage_summary
                        or usage_summary["budget_twd"] <= 0
                        or usage_summary["spend_twd"] < usage_summary["budget_twd"]
                    )
                    items, source = extract.propose_chunks(
                        context.service.answerer, name, text, allow_model=within_budget,
                    )
                    self._json(HTTPStatus.OK, {"items": items, "source": source, "name": name})
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
                if parsed.path == "/api/conversations":
                    user = self._require_user()
                    if not user:
                        return
                    saved = self._save_conversations(user["id"], payload)
                    self._json(HTTPStatus.OK, {"saved": saved})
                    return
                if parsed.path == "/api/conversations/delete":
                    user = self._require_user()
                    if not user:
                        return
                    context.store.delete_conversation(
                        user["id"], str(payload.get("id", "")).strip()
                    )
                    self._json(HTTPStatus.OK, {"deleted": True})
                    return
                if parsed.path == "/api/prefs":
                    user = self._require_user()
                    if not user:
                        return
                    now = datetime.now(timezone.utc).isoformat()
                    for key, value in (payload.get("prefs") or {}).items():
                        if str(key) in USER_PREF_KEYS:
                            context.store.set_user_pref(
                                user["id"], str(key), str(value)[:120], now
                            )
                    self._json(HTTPStatus.OK, {"prefs": context.store.user_prefs(user["id"])})
                    return
                if parsed.path == "/api/admin/tuning":
                    if not self._require_admin():
                        return
                    rule_id = str(payload.get("rule_id", "")).strip()
                    text = str(payload.get("text", ""))
                    if rule_id not in tuning.known_rule_ids(**self._catalogue_defaults()):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "unknown_rule", "message": "找不到這條規則"})
                        return
                    if len(text) > TUNING_RULE_MAX_CHARS:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "too_long", "message": "這條規則太長了"})
                        return
                    if text.strip():
                        context.store.save_model_rule(
                            rule_id, text.strip(), datetime.now(timezone.utc).isoformat()
                        )
                    else:
                        # 清空＝還原預設，讓 app/tuning.py 的預設值重新生效。
                        context.store.delete_model_rule(rule_id)
                    self._json(HTTPStatus.OK, {"rule_id": rule_id, "customized": bool(text.strip())})
                    return
                if parsed.path == "/api/admin/tuning/reset":
                    if not self._require_admin():
                        return
                    rule_id = str(payload.get("rule_id", "")).strip()
                    if rule_id:
                        context.store.delete_model_rule(rule_id)
                    else:
                        context.store.clear_model_rules()
                    self._json(HTTPStatus.OK, {"reset": rule_id or "all"})
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

    class Server(ThreadingHTTPServer):
        # 預設 backlog 只有 5。一頁同時要幾個靜態檔，再加上其他分頁，很容易
        # 滿出來，滿出來的連線會被作業系統直接丟掉（畫面就是圖破掉、CSS 沒套）。
        request_queue_size = 128
        # 埠還在 TIME_WAIT 時也要能重新綁上，重新部署才不會卡住。
        allow_reuse_address = True

    return Server((host, port), Handler)
