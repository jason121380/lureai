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
PASSWORD_MIN_LENGTH = 15
PASSWORD_MAX_LENGTH = 256
# Exact matches after case-folding. Length remains a separate rule so this list
# can reject widely reused secrets that happen to meet the minimum.
COMMON_WEAK_PASSWORDS = frozenset({
    "123456789012345", "adminadminadmin", "letmeinletmeinletmein",
    "password1234567", "passwordpassword", "qwerty123456789",
    "qwertyuiopasdfgh", "welcome123456789",
})
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

    def __init__(
        self, max_failures: int = 5, window_seconds: int = 300,
        max_concurrent: int = 4, max_keys: int = 4096,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()
        self._ops = 0
        self.max_concurrent = max_concurrent
        self.max_keys = max_keys
        self._active_verifiers = 0

    def _sweep(self, now: float, *, force: bool = False) -> None:
        """caller 持鎖。整份走一遍，把整把都過期的 key 刪掉。"""
        self._ops += 1
        if not force and self._ops % self.SWEEP_EVERY:
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

    def _limit(self, key: str) -> int:
        if key == "global":
            return self.max_failures * 20
        if key.startswith("ip|"):
            return self.max_failures * 4
        return self.max_failures

    def reserve(self, keys: tuple[str, ...]) -> tuple[str, ...] | None:
        """Atomically admit and occupy all scopes before password verification."""
        keys = tuple(dict.fromkeys(keys))
        now = time.monotonic()
        with self._lock:
            failures = {key: self._prune(key, now) for key in keys}
            if self._active_verifiers >= self.max_concurrent:
                return None
            if any(
                len(failures[key] or ()) + self._active.get(key, 0) >= self._limit(key)
                for key in keys
            ):
                return None
            tracked = set(self._failures) | set(self._active)
            if len(tracked | set(keys)) > self.max_keys:
                self._sweep(now, force=True)
                tracked = set(self._failures) | set(self._active)
                if len(tracked | set(keys)) > self.max_keys:
                    return None
            for key in keys:
                self._active[key] = self._active.get(key, 0) + 1
            self._active_verifiers += 1
            return keys

    def finish(self, reservation: tuple[str, ...] | None, *, succeeded: bool) -> None:
        if not reservation:
            return
        now = time.monotonic()
        with self._lock:
            for key in reservation:
                remaining = self._active.get(key, 0) - 1
                if remaining > 0:
                    self._active[key] = remaining
                else:
                    self._active.pop(key, None)
            self._active_verifiers = max(0, self._active_verifiers - 1)
            if succeeded:
                # A valid account may recover from its own typos. IP and global
                # history describe surrounding attack traffic and must remain.
                for key in reservation:
                    if key.startswith("account|"):
                        self._failures.pop(key, None)
            else:
                for key in reservation:
                    self._prune(key, now)
                    self._failures.setdefault(key, deque()).append(now)
                self._sweep(now)


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

    def _validate_username(self, username: str) -> str:
        normalized = str(username or "").strip()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("帳號需為 2 至 64 個字且不可包含空白")
        return normalized

    def _validate(self, username: str, password: str) -> tuple[str, str]:
        normalized = self._validate_username(username)
        secret = str(password or "")
        if len(secret) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"密碼至少 {PASSWORD_MIN_LENGTH} 個字")
        if len(secret) > PASSWORD_MAX_LENGTH:
            raise ValueError(f"密碼不可超過 {PASSWORD_MAX_LENGTH} 個字")
        if secret.casefold() in COMMON_WEAK_PASSWORDS:
            raise ValueError("不可使用常見弱密碼")
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
        normalized = self._validate_username(username)
        # Existing deployments may intentionally retain an older short password.
        # Check existence before validating or hashing the environment value.
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT id, username, role, active, created_at, updated_at FROM users "
                "WHERE username = ? COLLATE NOCASE", (normalized,),
            ).fetchone()
        if row:
            return _public_user(row)
        normalized, secret = self._validate(normalized, password)
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
