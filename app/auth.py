from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .storage import KnowledgeStore


USERNAME_PATTERN = re.compile(r"^\S{2,64}$")
PASSWORD_MIN_LENGTH = 4
ROLES = ("user", "admin")
# OWASP-equivalent scrypt work factor N=2^15/r=8/p=4: identical total cost to
# the N=2^17/p=1 baseline but ~32 MB instead of ~128 MB per verification, so
# concurrent logins cannot exhaust container memory. Existing hashes still
# verify with the parameters stored inside each hash string.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 4
SCRYPT_MAXMEM = 256 * 1024 * 1024


class LoginRateLimiter:
    # 每累積這麼多次失敗就整份掃一次過期的 key。_prune 只清「再次被查到的
    # 那一把」，而登入失敗的 key 是攻擊者出的（換一個 XFF 或帳號就是一把
    # 新鑰匙），只出現一次的 key 永遠不會再被查到——不掃的話字典只進不出。
    SWEEP_EVERY = 512

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._ops = 0

    def _sweep(self, now: float) -> None:
        """caller 持鎖。整份走一遍，把整把都過期的 key 刪掉。"""
        self._ops += 1
        if self._ops % self.SWEEP_EVERY:
            return
        cutoff = now - self.window_seconds
        for key in [k for k, items in self._failures.items() if not items or items[-1] <= cutoff]:
            del self._failures[key]

    def _prune(self, key: str, now: float) -> deque[float] | None:
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return None
        return failures

    def allowed(self, key: str) -> bool:
        with self._lock:
            failures = self._prune(key, time.monotonic())
            return failures is None or len(failures) < self.max_failures

    def failed(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            self._failures.setdefault(key, deque()).append(now)
            self._sweep(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class RequestRateLimiter:
    """Sliding-window limiter for authenticated endpoints such as /api/chat."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return False
            hits.append(now)
            return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public_user(row) -> dict:
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "role": str(row["role"]) if row["role"] in ROLES else "user",
        "active": bool(row["active"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class AuthManager:
    def __init__(self, store: KnowledgeStore, session_days: int = 30):
        self.store = store
        self.session_days = session_days

    def _validate(self, username: str, password: str) -> tuple[str, str]:
        normalized = str(username or "").strip()
        secret = str(password or "")
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("帳號需為 2 至 64 個字且不可包含空白")
        if len(secret) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"密碼至少 {PASSWORD_MIN_LENGTH} 個字")
        if len(secret) > 256:
            raise ValueError("密碼不可超過 256 個字")
        return normalized, secret

    @staticmethod
    def _validate_role(role: str | None) -> str | None:
        if role is None or role == "":
            return None
        normalized = str(role).strip().lower()
        if normalized not in ROLES:
            raise ValueError("權限必須是 user（一般用戶）或 admin（管理者）")
        return normalized

    def _hash_password(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
            p=SCRYPT_P, maxmem=SCRYPT_MAXMEM,
        )
        return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"

    def _verify_password(self, password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt, expected = encoded.split("$", 5)
            if algorithm != "scrypt":
                return False
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt),
                n=int(n),
                r=int(r),
                p=int(p),
                maxmem=SCRYPT_MAXMEM,
            )
            return hmac.compare_digest(actual, bytes.fromhex(expected))
        except (ValueError, TypeError):
            return False

    def create_or_reset_user(self, username: str, password: str, role: str | None = None) -> dict:
        username, password = self._validate(username, password)
        normalized_role = self._validate_role(role)
        password_hash = self._hash_password(password)
        timestamp = _now().isoformat()
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute(
                "SELECT id, role FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if row:
                user_id = int(row["id"])
                self.store.connection.execute(
                    "UPDATE users SET username = ?, password_hash = ?, role = ?, active = 1, updated_at = ? WHERE id = ?",
                    (username, password_hash, normalized_role or str(row["role"]), timestamp, user_id),
                )
                self.store.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            else:
                cursor = self.store.connection.execute(
                    "INSERT INTO users(username, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (username, password_hash, normalized_role or "user", timestamp, timestamp),
                )
                user_id = int(cursor.lastrowid)
            current = self.store.connection.execute(
                "SELECT id, username, role, active, created_at, updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return _public_user(current)

    def ensure_bootstrap_user(self, username: str, password: str, role: str | None = None) -> dict:
        normalized, secret = self._validate(username, password)
        normalized_role = self._validate_role(role) or "user"
        password_hash = self._hash_password(secret)
        timestamp = _now().isoformat()
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                """
                INSERT INTO users(username, password_hash, role, active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(username) DO NOTHING
                """,
                (normalized, password_hash, normalized_role, timestamp, timestamp),
            )
            row = self.store.connection.execute(
                "SELECT id, username, role, active, created_at, updated_at FROM users WHERE username = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        return _public_user(row)

    def login(self, username: str, password: str) -> tuple[str, dict]:
        normalized = str(username or "").strip()
        secret = str(password or "")
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (normalized,)
            ).fetchone()
        # scrypt is deliberately expensive; verify outside the store lock so a
        # login attempt cannot stall every other database operation.
        if not row or not row["active"] or not self._verify_password(secret, row["password_hash"]):
            raise ValueError("帳號或密碼錯誤")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        created_at = _now()
        expires_at = created_at + timedelta(days=self.session_days)
        with self.store._lock, self.store.connection:
            # Re-check under the lock: a concurrent password reset must not let
            # a login verified against the old hash mint a fresh session.
            current = self.store.connection.execute(
                "SELECT * FROM users WHERE id = ?", (int(row["id"]),)
            ).fetchone()
            if (
                not current
                or not current["active"]
                or current["password_hash"] != row["password_hash"]
            ):
                raise ValueError("帳號或密碼錯誤")
            self.store.connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (created_at.isoformat(),)
            )
            self.store.connection.execute(
                "INSERT INTO sessions(user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (int(row["id"]), token_hash, created_at.isoformat(), expires_at.isoformat()),
            )
        return token, _public_user(current)

    def authenticate(self, token: str | None) -> dict | None:
        if not token:
            return None
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self.store._lock:
            row = self.store.connection.execute(
                """
                SELECT users.id, users.username, users.role, users.active, users.created_at, users.updated_at
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1
                """,
                (token_hash, _now().isoformat()),
            ).fetchone()
        return _public_user(row) if row else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self.store._lock, self.store.connection:
            self.store.connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def list_users(self) -> list[dict]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT id, username, role, active, created_at, updated_at FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [_public_user(row) for row in rows]
