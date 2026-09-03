import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.auth import AuthManager, LoginRateLimiter, SCRYPT_N
from app.storage import KnowledgeStore


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.db")
        self.auth = AuthManager(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_password_is_hashed_and_login_creates_session(self):
        self.auth.create_or_reset_user("designer", "correct-horse-battery")

        row = self.store.connection.execute(
            "SELECT username, password_hash FROM users WHERE username = ?",
            ("designer",),
        ).fetchone()
        self.assertEqual(row["username"], "designer")
        self.assertNotIn("correct-horse-battery", row["password_hash"])
        self.assertTrue(row["password_hash"].startswith("scrypt$"))

        token, user = self.auth.login("designer", "correct-horse-battery")
        self.assertEqual(user["username"], "designer")
        self.assertNotIn(token, self.store.connection.execute(
            "SELECT token_hash FROM sessions"
        ).fetchone()["token_hash"])
        self.assertEqual(self.auth.authenticate(token)["username"], "designer")

    def test_reset_invalidates_existing_sessions(self):
        self.auth.create_or_reset_user("designer", "first-password")
        token, _ = self.auth.login("designer", "first-password")

        self.auth.create_or_reset_user("designer", "second-password")

        self.assertIsNone(self.auth.authenticate(token))
        with self.assertRaises(ValueError):
            self.auth.login("designer", "first-password")
        self.assertEqual(
            self.auth.login("designer", "second-password")[1]["username"],
            "designer",
        )

    def test_rejects_short_passwords(self):
        with self.assertRaisesRegex(ValueError, "至少 4"):
            self.auth.create_or_reset_user("designer", "abc")

    def test_accepts_four_character_password(self):
        user = self.auth.create_or_reset_user("designer", "1234")
        self.assertEqual(user["username"], "designer")
        self.assertEqual(user["role"], "user")

    def test_roles_are_stored_and_validated(self):
        admin = self.auth.create_or_reset_user("boss", "1234", role="admin")
        self.assertEqual(admin["role"], "admin")
        _, logged_in = self.auth.login("boss", "1234")
        self.assertEqual(logged_in["role"], "admin")
        # Resetting the password without a role keeps the existing role.
        kept = self.auth.create_or_reset_user("boss", "5678")
        self.assertEqual(kept["role"], "admin")
        with self.assertRaisesRegex(ValueError, "權限"):
            self.auth.create_or_reset_user("boss", "1234", role="owner")

    def test_password_hash_uses_strong_scrypt_work_factor(self):
        from app.auth import SCRYPT_P, SCRYPT_R

        # OWASP-acceptable scrypt cost: N * r * p must be at least 2^17 * 8
        # (equivalent to the N=2^17, r=8, p=1 baseline) while keeping the
        # per-verify memory footprint (128 * N * r) at or below ~64 MB.
        self.assertGreaterEqual(SCRYPT_N * SCRYPT_R * SCRYPT_P, 2**17 * 8)
        self.assertLessEqual(128 * SCRYPT_N * SCRYPT_R, 64 * 1024 * 1024)

    def test_login_rate_limiter_blocks_after_repeated_failures(self):
        limiter = LoginRateLimiter(max_failures=3, window_seconds=60)

        for _ in range(3):
            self.assertTrue(limiter.allowed("client"))
            limiter.failed("client")

        self.assertFalse(limiter.allowed("client"))
        limiter.succeeded("client")
        self.assertTrue(limiter.allowed("client"))

    def test_login_rate_limiter_sweeps_keys_that_are_never_queried_again(self):
        """撞庫攻擊每次換一個 XFF 或帳號就是一把新鑰匙，只出現一次的 key
        永遠不會再被 _prune 查到——不整份掃的話，字典只進不出直到記憶體吃光。"""
        limiter = LoginRateLimiter(max_failures=3, window_seconds=60)
        limiter.SWEEP_EVERY = 4
        with patch("app.auth.time.monotonic") as clock:
            clock.return_value = 0.0
            for n in range(3):
                limiter.failed(f"ip|10.0.0.{n}|user-{n}")
            # 視窗過了之後來的下一次失敗要順手把上面三把清掉。
            clock.return_value = 120.0
            limiter.failed("ip|10.9.9.9|fresh")

        self.assertEqual(set(limiter._failures), {"ip|10.9.9.9|fresh"})

    def test_password_reset_cannot_leave_old_password_session(self):
        self.auth.create_or_reset_user("designer", "first-password")
        entered_verify = threading.Event()
        continue_verify = threading.Event()
        original_verify = self.auth._verify_password
        result = {}

        def delayed_verify(password, encoded):
            entered_verify.set()
            continue_verify.wait(3)
            return original_verify(password, encoded)

        def login():
            try:
                result["login"] = self.auth.login("designer", "first-password")
            except ValueError:
                result["login"] = None

        self.auth._verify_password = delayed_verify
        login_thread = threading.Thread(target=login)
        login_thread.start()
        self.assertTrue(entered_verify.wait(3))

        reset_thread = threading.Thread(
            target=lambda: self.auth.create_or_reset_user("designer", "second-password")
        )
        reset_thread.start()
        continue_verify.set()
        login_thread.join(5)
        reset_thread.join(5)

        token = result.get("login", (None,))[0] if result.get("login") else None
        self.assertIsNone(self.auth.authenticate(token))


if __name__ == "__main__":
    unittest.main()
