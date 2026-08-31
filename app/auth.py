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
PASSWORD_MIN_LENGTH = 8
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        failures = self._failures.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    def allowed(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) < self.max_failures

    def failed(self, key: str) -> None:
        with self._lock:
            self._recent(key, time.monotonic()).append(time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public_user(row) -> dict:
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
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
            raise ValueError("密碼至少 8 個字")
        if len(secret) > 256:
            raise ValueError("密碼不可超過 256 個字")
        return normalized, secret

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

    def create_or_reset_user(self, username: str, password: str) -> dict:
        username, password = self._validate(username, password)
        password_hash = self._hash_password(password)
        timestamp = _now().isoformat()
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if row:
                user_id = int(row["id"])
                self.store.connection.execute(
                    "UPDATE users SET username = ?, password_hash = ?, active = 1, updated_at = ? WHERE id = ?",
                    (username, password_hash, timestamp, user_id),
                )
                self.store.connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            else:
                cursor = self.store.connection.execute(
                    "INSERT INTO users(username, password_hash, active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (username, password_hash, timestamp, timestamp),
                )
                user_id = int(cursor.lastrowid)
            current = self.store.connection.execute(
                "SELECT id, username, active, created_at, updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return _public_user(current)

    def ensure_bootstrap_user(self, username: str, password: str) -> dict:
        normalized, secret = self._validate(username, password)
        password_hash = self._hash_password(secret)
        timestamp = _now().isoformat()
        with self.store._lock, self.store.connection:
            self.store.connection.execute(
                """
                INSERT INTO users(username, password_hash, active, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?) ON CONFLICT(username) DO NOTHING
                """,
                (normalized, password_hash, timestamp, timestamp),
            )
            row = self.store.connection.execute(
                "SELECT id, username, active, created_at, updated_at FROM users WHERE username = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        return _public_user(row)

    def login(self, username: str, password: str) -> tuple[str, dict]:
        normalized = str(username or "").strip()
        secret = str(password or "")
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (normalized,)
            ).fetchone()
            if not row or not row["active"] or not self._verify_password(secret, row["password_hash"]):
                raise ValueError("帳號或密碼錯誤")
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            created_at = _now()
            expires_at = created_at + timedelta(days=self.session_days)
            self.store.connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (created_at.isoformat(),)
            )
            self.store.connection.execute(
                "INSERT INTO sessions(user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (row["id"], token_hash, created_at.isoformat(), expires_at.isoformat()),
            )
        return token, _public_user(row)

    def authenticate(self, token: str | None) -> dict | None:
        if not token:
            return None
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self.store._lock:
            row = self.store.connection.execute(
                """
                SELECT users.id, users.username, users.active, users.created_at, users.updated_at
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
                "SELECT id, username, active, created_at, updated_at FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [_public_user(row) for row in rows]
