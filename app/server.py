from __future__ import annotations

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
import socket
import threading
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


def _trusted_proxies() -> tuple:
    """`TRUSTED_PROXY_IPS`：逗號分隔的 IP 或 CIDR，指定哪幾台 proxy 的
    `X-Forwarded-For` 可以採信。沒設就用「內網或本機」這個預設（見 `_client_ip`）。
    """
    networks = []
    for item in os.getenv("TRUSTED_PROXY_IPS", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


TRUSTED_PROXIES = _trusted_proxies()

# 對話紀錄存伺服器（使用者決定），這幾個上限只是防呆，不讓單一帳號無限長大。
# LINE 的 reply token 從 webhook 進來算起只有 60 秒，而 lurebot 在打我們之前已經
# 用掉一些、送出去也還要時間。45 秒是內部的保守目標，不是 LINE 的保證。
# 同時最多處理幾個請求。串流那條會一直佔著（生成可能要幾十秒），所以要留餘裕；
# 但也不能無上限，否則慢速連線可以把記憶體與執行緒吃光。
class BudgetLedger:
    """把「已經送出去、還沒記到帳」的那幾筆也算進月支出。

    舊版是「先讀餘額 → 呼叫模型 → 事後記帳」，兩個請求同時進來時讀到的是同一個
    餘額，於是一起穿過上限（實測：上限 NT$1，兩個並行請求都獲准，各記 0.65，
    最後 1.30）。這裡在呼叫之前先預留一筆估計值，記完帳再釋放。

    只在單一行程內有效，這個服務本來就只有一個 process；真的要跨行程再說。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, list[float]] = {}
        self._next = itertools.count(1)

    def reserve(self, user_id: int | None, amount: float) -> tuple | None:
        if user_id is None or amount <= 0:
            return None
        token = (user_id, next(self._next), float(amount))
        with self._lock:
            self._pending.setdefault(int(user_id), []).append(float(amount))
        return token

    def check_and_reserve(
        self, user_id: int | None, amount: float, spend_twd: float, budget_twd: float,
    ) -> tuple[bool, tuple | None]:
        """「還在預算內嗎」與「預留」要在同一次取鎖裡做完。

        分開做的話（先讀含 pending 的餘額、再 reserve），兩個同時到的請求
        會在彼此 reserve 之前都讀到同一份 pending，一起判定沒超額——預算
        照樣被穿過。spend_twd 是資料庫裡已結算的花費，由呼叫端先讀好帶進來
        （已結算的部分只增不減，不需要跟 pending 同一把鎖）。
        """
        if user_id is None:
            return True, None
        with self._lock:
            pending = float(sum(self._pending.get(int(user_id), ())))
            if budget_twd > 0 and spend_twd + pending >= budget_twd:
                return False, None
            if amount <= 0:
                return True, None
            token = (user_id, next(self._next), float(amount))
            self._pending.setdefault(int(user_id), []).append(float(amount))
        return True, token

    def release(self, token: tuple | None) -> None:
        if not token:
            return
        user_id, _serial, amount = token
        with self._lock:
            amounts = self._pending.get(int(user_id))
            if not amounts:
                return
            try:
                amounts.remove(amount)
            except ValueError:
                amounts.pop()
            if not amounts:
                self._pending.pop(int(user_id), None)

    def pending_for(self, user_id: int | None) -> float:
        if user_id is None:
            return 0.0
        with self._lock:
            return float(sum(self._pending.get(int(user_id), ())))


MAX_WORKERS = int(os.getenv("MAX_WORKERS", "48") or 48)
# 讀完整個 request body 的總時限（socket timeout 管的是「兩次收到資料之間」，
# 每 25 秒滴一個位元組就能把一條執行緒綁住任意久）。
BODY_READ_TIMEOUT = 20.0

LINE_TOTAL_BUDGET = 45.0
# 再怎麼趕也要留這麼多秒，否則連一次生成都跑不完，等於整組關掉。
MIN_LINE_BUDGET = 10.0

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
    fit_delays,
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
    # 併發時的預算把關：呼叫模型之前先預留、記完帳再釋放。
    budget: BudgetLedger = field(default_factory=BudgetLedger)
    profile: str = "designer_coach"
    access_level: str = "internal_coaching"
    app_name: str = "LUREAI 你的智慧大腦中心"
    assistant_name: str = "AI 輔導教練"
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
        profile: str = "designer_coach",
        access_level: str = "internal_coaching",
        app_name: str = "LUREAI 你的智慧大腦中心",
        assistant_name: str = "AI 輔導教練",
        welcome_prompts: tuple[str, ...] = (),
        blocked_topics: dict | None = None,
        fallback_message: str | None = None,
        defer_bootstrap: bool = False,
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
        context = cls(
            store=store,
            service=service,
            retriever=retriever,
            knowledge_path=knowledge,
            static_dir=Path(static_dir),
            admin_token=admin_token,
            bot_token=bot_token,
            bot_user_id=None,
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
        service.answerer.runtime.durable = context.persist_model_accounting
        if not defer_bootstrap:
            try:
                context.initialize_accounts()
            except Exception:
                context.close()
                raise
        return context

    def initialize_accounts(self):
        """Apply bootstrap credentials and rule migration after durable restore."""
        username = os.getenv("USER_USERNAME", "").strip()
        password = os.getenv("USER_PASSWORD", "")
        if username or password:
            if not username or not password:
                raise ValueError("USER_USERNAME 與 USER_PASSWORD 必須同時設定")
            self.auth.ensure_bootstrap_user(
                username, password, role=os.getenv("USER_ROLE", "").strip() or None,
            )
        if self.bot_token:
            self.bot_user_id = self.auth.ensure_bootstrap_user(
                BOT_SERVICE_USERNAME, secrets.token_urlsafe(32)
            )["id"]
        tuning.migrate_rule_overrides(self.store)

    def persist_model_accounting(self):
        if self.replica is not None and self.replica.configured:
            if not self.replica.enabled or not self.replica.check_writer():
                raise RuntimeError("snapshot writer unavailable")
            self.replica.backup(self.store)
            return True
        return True

    def close(self) -> None:
        runtime = getattr(self.service.answerer, "runtime", None)
        if runtime is not None and not runtime.drain(timeout=0):
            raise TimeoutError("generation accounting still active; database left open")
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

        def parse_request(self):
            if not super().parse_request():
                return False
            with self.server._drain_condition:
                if self.server._draining:
                    self.close_connection = True
                    return False
                self.server._connections[self.connection] = True
            return True

        def handle_one_request(self):
            try:
                super().handle_one_request()
            finally:
                with self.server._drain_condition:
                    self.server._connections[self.connection] = False
                    if self.server._draining:
                        self.close_connection = True

        def log_message(self, format_string: str, *args) -> None:
            if os.getenv("APP_QUIET") != "1":
                super().log_message(format_string, *args)

        def _send_security_headers(self) -> None:
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)

        def _writer_unavailable(self):
            replica = context.replica
            if not replica or not replica.configured:
                return False
            check = getattr(replica, "check_writer", None)
            return not (check() if check else replica.writable)

        def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
            if self._writer_unavailable() and status < 400:
                status = HTTPStatus.SERVICE_UNAVAILABLE
                payload = {"error": "persistence_unavailable", "message": "資料保存暫時無法使用，請稍後重試"}
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

        def _read_body(self, size: int) -> bytes:
            """把 body 讀完，但整段有一個總時限。

            `rfile.read(size)` 會一直等到收滿為止，而 socket timeout 是「兩次
            收到資料之間」的上限——每 25 秒滴一個位元組就能把一條工作執行緒
            綁住任意久。這裡改成分段讀，總時間到了就放棄。
            """
            deadline = time.monotonic() + BODY_READ_TIMEOUT
            chunks = []
            remaining = size
            while remaining > 0:
                if time.monotonic() > deadline:
                    raise ValueError("讀取請求內容逾時")
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    raise ValueError("請求內容不完整")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def _read_json(self, max_bytes: int | None = None) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("無效的 Content-Length") from exc
            if size <= 0 or size > (max_bytes or context.max_request_bytes):
                raise ValueError("請求內容大小不符合限制")
            try:
                payload = json.loads(self._read_body(size))
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

        def _save_conversations(self, user_id: int, payload: dict) -> list[dict]:
            """把前端送上來的對話寫進資料庫；一次可以送一段或整批（登入時的搬家）。"""
            raw = payload.get("conversations")
            if raw is None:
                raw = [payload.get("conversation") or payload]
            if not isinstance(raw, list):
                return []
            now = datetime.now(timezone.utc).isoformat()
            acks = []
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
                try:
                    rev = max(0, int(item.get("rev") or 0))
                except (TypeError, ValueError):
                    rev = 0
                expected_rev = item.get("expected_rev")
                if expected_rev is not None:
                    try:
                        expected_rev = int(expected_rev)
                    except (TypeError, ValueError):
                        acks.append({"id": conversation_id, "rev": rev, "status": "conflict"})
                        continue
                acks.append(context.store.save_conversation(
                    user_id,
                    conversation_id,
                    str(item.get("title", ""))[:CONVERSATION_TITLE_MAX],
                    str(item.get("tone", ""))[:20],
                    trimmed,
                    str(item.get("createdAt") or now),
                    str(item.get("updatedAt") or now),
                    rev, expected_rev,
                ))
            if any(ack["status"] == "accepted" for ack in acks):
                context.store.prune_conversations(user_id, keep=CONVERSATION_KEEP)
            return acks

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

        # 網頁只有專家與客服兩種語氣。`line` 是寫給 LINE 出口的：句子沒有標點、
        # 要拆成多則、引用在出口才剝掉。網頁照單全收的話，畫面上會出現一段沒有
        # 標點的文字，而且那條路還跳過了引用守門——同一個 tone 在兩條路上的
        # 出口行為並不一樣。
        WEB_TONES = ("expert", "service")

        @classmethod
        def _web_tone(cls, value) -> str:
            tone = str(value or "").strip().lower()
            return tone if tone in cls.WEB_TONES else "expert"

        def _client_ip(self) -> str:
            """這條連線背後真正的來源 IP，用來當限流的鑰匙。

            `X-Forwarded-For` 是客戶端可以自己寫的，所以只在「這一跳本來就是
            我們的 proxy」時才採信。設了 `TRUSTED_PROXY_IPS` 就以那份名單為準；
            沒設時退回「內網或本機來的才信」——雲端平台一律是內網 proxy 連進來，
            全部不信的話所有人會共用同一個 IP，一個人打錯密碼就把整間店鎖在外面。
            偽造 XFF 仍然換得到新的 IP 鑰匙，擋那件事的是上面「只看帳號」那把。
            """
            peer = self.client_address[0]
            try:
                peer_address = ipaddress.ip_address(peer)
            except ValueError:
                return peer
            if TRUSTED_PROXIES:
                trusted = any(
                    peer_address.version == network.version and peer_address in network
                    for network in TRUSTED_PROXIES
                )
            else:
                trusted = peer_address.is_private or peer_address.is_loopback
            if trusted:
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

        def _usage_summary(self, user_id: int, include_pending: bool = True) -> dict:
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
            summary = context.pricing.summary(
                month=f"{now.year:04d}-{now.month:02d}", **totals
            )
            # 已經送出去、還沒記到帳的那幾筆也要算進來，否則同時進來的請求
            # 讀到的是同一個餘額，會一起穿過上限。（_reserve_budget 走
            # check_and_reserve，pending 由那把鎖自己加，這裡就不要重複算。）
            if include_pending:
                pending = context.budget.pending_for(user_id)
                if pending:
                    summary = dict(summary)
                    summary["spend_twd"] = round(summary["spend_twd"] + pending, 4)
            return summary

        def _reserve_budget(self, user_id: int | None) -> tuple[bool, tuple | None]:
            # Each actual generation reserves its complete bound in the durable runtime.
            return True, None

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
                        "tombstones": context.store.list_conversation_tombstones(user["id"]),
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
                    # 清單每一列都顯示 locator（ads-10），管理員自然會拿它來搜，
                    # 但檢索索引裡沒有 locator——語意檢索永遠撈不到，看起來就像
                    # 這塊知識不存在。編號類查詢走字面比對補上，而且**字面命中
                    # 排最前面**：搜 ads-10 時它就是要找那一塊，不能埋在語意
                    # 檢索順便撈到的十幾筆裡。
                    needle = query.lower()
                    literal: list = []
                    semantic: list = []
                    for chunk in context.store.list_chunks(
                        limit=100000, origin=origin, domain=domain
                    ):
                        if (
                            needle in str(chunk["locator"]).lower()
                            or needle in str(chunk["chunk_id"]).lower()
                        ):
                            literal.append(chunk)
                        elif chunk["chunk_id"] in found:
                            semantic.append(chunk)
                    items = literal + semantic
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
            # 這一則的共同截止時間。LINE 的 reply token 從 webhook 進來算起只有
            # 60 秒，lurebot 在打我們之前已經用掉一些，所以這裡抓得更保守。
            # 生成、每一次重試與出口的停頓全部從這一份裡扣——舊版是每次呼叫各拿
            # 一份完整 timeout（25＋25）再加上 8-25 秒停頓，光名義配置就超過窗口。
            # lurebot 可以用 `budget_seconds` 告訴我們它那邊還剩多少。
            try:
                budget = float(payload.get("budget_seconds") or LINE_TOTAL_BUDGET)
            except (TypeError, ValueError):
                budget = LINE_TOTAL_BUDGET
            budget = max(MIN_LINE_BUDGET, min(budget, LINE_TOTAL_BUDGET))
            deadline = time.monotonic() + budget
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
            within_budget, slot = self._reserve_budget(context.bot_user_id)
            try:
                result = context.service.chat(
                    payload.get("message", ""),
                    conversation_id or None,
                    history,
                    user_id=context.bot_user_id,
                    allow_model=within_budget,
                    tone="line",
                    context_note=context_instruction(payload.get("context")),
                    want_followups=False,
                    deadline=deadline,
                )
            finally:
                context.budget.release(slot)
            response = {
                "trace_id": result["trace_id"],
                "conversation_id": result.get("conversation_id"),
                "status": result["status"],
                "reason": result["reason"],
                "messages": [],
                "delay_seconds": 0.0,
                "answer": "",
                # 只有真的要送進 LINE 的那則才附來源。轉真人與降級的情況
                # lurebot 一個字都不會送出，這時候還把知識原文帶回去，等於
                # bot 權杖一旦外流就多洩漏一份內容，payload 也白白變大。
                "citations": [],
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
            # 走到這裡才是真的會送出去的那則，來源這時候才附上（稽核對照用）。
            response["citations"] = result.get("citations", [])
            rules = context.store.model_rules()
            delay = reply_delay(
                delay_range=tuning.parse_delay_range(
                    rules.get("delivery-delay", ""), DELAY_RANGE
                )
            )
            # 每一則之間再等一小段，訊息才會一則一則出現而不是同時跳出來。
            gaps = message_gaps(
                len(response.get("messages") or []),
                gap_range=tuning.parse_delay_range(
                    rules.get("delivery-gap", ""), MESSAGE_GAP_RANGE
                ),
            )
            # 停頓跟生成搶的是同一份時間。生成慢的時候還照原本的秒數等下去，
            # 等到的是 reply token 過期；剩多少就等多少，回得快一點總比不回好。
            delay, gaps = fit_delays(delay, gaps, deadline - time.monotonic())
            response["delay_seconds"] = delay
            response["message_gaps"] = gaps
            self._json(HTTPStatus.OK, response)

        def do_POST(self) -> None:
            if self._writer_unavailable():
                self.close_connection = True
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "persistence_unavailable", "message": "資料保存暫時無法使用，請稍後重試"})
                return
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
                    # Account, IP and process-wide admission is one atomic reservation.
                    # It happens before scrypt so parallel requests cannot all pass a
                    # check and then consume verifier memory together.
                    login_keys = (
                        f"account|{username}",
                        f"ip|{self._client_ip()}",
                        "global",
                    )
                    reservation = context.login_limiter.reserve(login_keys)
                    if reservation is None:
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "too_many_attempts", "message": "登入嘗試過多，請稍後再試"},
                            {"Retry-After": "300"},
                        )
                        return
                    succeeded = False
                    try:
                        token, user = context.auth.login(
                            payload.get("username", ""), payload.get("password", "")
                        )
                    except ValueError:
                        self._json(
                            HTTPStatus.UNAUTHORIZED,
                            {"error": "invalid_credentials", "message": "帳號或密碼錯誤"},
                        )
                        return
                    else:
                        succeeded = True
                    finally:
                        context.login_limiter.finish(reservation, succeeded=succeeded)
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
                    within_budget, slot = self._reserve_budget(user["id"])
                    try:
                        result = context.service.chat(
                            payload.get("message", ""),
                            payload.get("conversation_id"),
                            payload.get("history"),
                            user_id=user["id"],
                            allow_model=within_budget,
                            tone=self._web_tone(payload.get("tone")),
                        )
                    finally:
                        # 記完帳就把預留的釋放掉，否則之後的請求會被自己擋住。
                        context.budget.release(slot)
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
                    within_budget, slot = self._reserve_budget(user["id"])
                    events = context.service.chat_stream(
                        payload.get("message", ""),
                        payload.get("conversation_id"),
                        payload.get("history"),
                        user_id=user["id"],
                        allow_model=within_budget,
                        tone=self._web_tone(payload.get("tone")),
                    )
                    # 驗證錯誤要在串流開始前用 JSON 回覆；service 會先 yield 一個
                    # start 事件，所以這裡幾乎立刻返回，header 不會被模型生成卡住。
                    try:
                        first_event = next(events)
                    except StopIteration:
                        context.budget.release(slot)
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": "服務暫時無法處理請求"})
                        return
                    except Exception:
                        context.budget.release(slot)
                        raise
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
                            if self._writer_unavailable():
                                break
                            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        events.close()
                        context.budget.release(slot)
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
                    if not context.store.audit_belongs_to(trace_id, user["id"]):
                        self._json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "not_found", "message": "找不到這一則回答"},
                        )
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
                    within_budget, slot = self._reserve_budget(user["id"])
                    try:
                        result = context.service.summarize_title(
                            payload.get("message", ""),
                            payload.get("answer", ""),
                            conversation_id=payload.get("conversation_id"),
                            user_id=user["id"],
                            allow_model=within_budget,
                        )
                    finally:
                        context.budget.release(slot)
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
                    within_budget, slot = self._reserve_budget(admin["id"] if admin else None)
                    try:
                        items, source, usage = extract.propose_chunks(
                            context.service.answerer, name, text, allow_model=within_budget,
                            user_id=admin["id"] if admin else None,
                        )
                    finally:
                        context.budget.release(slot)
                    # 這條路一次送兩萬多字進模型，是單次最貴的呼叫。不記帳的話
                    # 後台看到的月花費會比實際少，預算上限也擋不到它。
                    if usage.get("input_tokens") or usage.get("output_tokens"):
                        context.service.record_usage(
                            "knowledge_analyze", usage, user_id=admin["id"] if admin else None,
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
                    acks = self._save_conversations(user["id"], payload)
                    self._json(HTTPStatus.OK, {"saved": sum(a["status"] == "accepted" for a in acks), "acks": acks})
                    return
                if parsed.path == "/api/conversations/delete":
                    user = self._require_user()
                    if not user:
                        return
                    ack = context.store.delete_conversation(
                        user["id"], str(payload.get("id", "")).strip()
                    )
                    self._json(HTTPStatus.OK, {"deleted": ack["status"] == "deleted", "ack": ack})
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
        daemon_threads = True
        block_on_close = False
        drain_timeout = 20.0

        def __init__(self, *args, **kwargs):
            self._drain_condition = threading.Condition()
            self._connections = {}
            self._draining = False
            super().__init__(*args, **kwargs)

        def begin_drain(self):
            with self._drain_condition:
                self._draining = True
                # Idle includes partial headers: these cannot begin application work.
                for connection, active in list(self._connections.items()):
                    if not active:
                        try:
                            connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

        def shutdown(self):
            self.begin_drain()
            super().shutdown()

        def server_close(self):
            self.begin_drain()
            super().server_close()
            deadline = time.monotonic() + self.drain_timeout
            with self._drain_condition:
                while self._connections:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("active HTTP workers exceeded shutdown grace")
                    self._drain_condition.wait(remaining)
            runtime = getattr(context.service.answerer, "runtime", None)
            if runtime is not None and not runtime.drain(timeout=max(0, deadline - time.monotonic())):
                raise TimeoutError("generation accounting exceeded shutdown grace")
        # 埠還在 TIME_WAIT 時也要能重新綁上，重新部署才不會卡住。
        allow_reuse_address = True
        # 一條連線一條執行緒，而且沒有上限——慢速連線可以一直開下去，把記憶體
        # 與執行緒吃光。這個號誌把「同時在處理的請求」夾住；滿了就直接回 503，
        # 不要無限排隊（排隊只是把爆掉的時間往後延，而且延到沒人知道為什麼慢）。
        workers = threading.BoundedSemaphore(MAX_WORKERS)

        # 號誌一定要在「建執行緒之前」拿：process_request_thread 是在新執行緒
        # 裡面才跑的，在那裡面等號誌等於執行緒已經開出去了——一千條慢速連線
        # 就是一千條執行緒各卡五秒，記憶體照樣被吃光。這裡在 accept 迴圈裡
        # 非阻塞地拿，拿不到直接回 503（accept 迴圈不能等，等了所有連線都進不來）。
        def process_request(self, request, client_address):
            with self._drain_condition:
                if self._draining:
                    self.shutdown_request(request)
                    return
                self._connections[request] = False
            if not self.workers.acquire(blocking=False):
                try:
                    # 回絕訊息也不能被塞住的 socket 綁住 accept 迴圈。
                    request.settimeout(2.0)
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n"
                        b"Retry-After: 5\r\n\r\n"
                    )
                except OSError:
                    pass
                self.shutdown_request(request)
                with self._drain_condition:
                    self._connections.pop(request, None)
                    self._drain_condition.notify_all()
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                # 執行緒沒開起來就要自己還，否則名額永遠少一個。
                self.workers.release()
                with self._drain_condition:
                    self._connections.pop(request, None)
                    self._drain_condition.notify_all()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.workers.release()
                with self._drain_condition:
                    self._connections.pop(request, None)
                    self._drain_condition.notify_all()

    return Server((host, port), Handler)
