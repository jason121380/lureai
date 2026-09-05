import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.auth import AuthManager, LoginRateLimiter, PASSWORD_MIN_LENGTH, SCRYPT_N
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
        self.auth.create_or_reset_user("designer", "first-password-for-tests")
        token, _ = self.auth.login("designer", "first-password-for-tests")

        self.auth.create_or_reset_user("designer", "second-password-for-tests")

        self.assertIsNone(self.auth.authenticate(token))
        with self.assertRaises(ValueError):
            self.auth.login("designer", "first-password-for-tests")
        self.assertEqual(
            self.auth.login("designer", "second-password-for-tests")[1]["username"],
            "designer",
        )

    def test_rejects_short_passwords(self):
        with self.assertRaisesRegex(ValueError, "至少 15"):
            self.auth.create_or_reset_user("designer", "abc")

    def test_rejects_common_password_even_when_long_enough(self):
        self.assertEqual(PASSWORD_MIN_LENGTH, 15)
        with self.assertRaisesRegex(ValueError, "常見弱密碼"):
            self.auth.create_or_reset_user("designer", "password1234567")

    def test_existing_short_password_still_logs_in(self):
        password_hash = self.auth._hash_password("1234")
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO users(username, password_hash, role, active, created_at, updated_at) "
                "VALUES ('legacy', ?, 'user', 1, 'now', 'now')", (password_hash,),
            )
        self.assertEqual(self.auth.login("legacy", "1234")[1]["username"], "legacy")

    def test_bootstrap_existing_user_ignores_new_password_policy(self):
        password_hash = self.auth._hash_password("1234")
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO users(username, password_hash, role, active, created_at, updated_at) "
                "VALUES ('legacy', ?, 'admin', 1, 'now', 'now')", (password_hash,),
            )
        user = self.auth.ensure_bootstrap_user("legacy", "1234", role="invalid-old-setting")
        self.assertEqual(user["role"], "admin")

    def test_roles_are_stored_and_validated(self):
        admin = self.auth.create_or_reset_user("boss", "boss-password-for-tests", role="admin")
        self.assertEqual(admin["role"], "admin")
        _, logged_in = self.auth.login("boss", "boss-password-for-tests")
        self.assertEqual(logged_in["role"], "admin")
        # Resetting the password without a role keeps the existing role.
        kept = self.auth.create_or_reset_user("boss", "replacement-password-for-tests")
        self.assertEqual(kept["role"], "admin")
        with self.assertRaisesRegex(ValueError, "權限"):
            self.auth.create_or_reset_user("boss", "another-password-for-tests", role="owner")

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
            reservation = limiter.reserve(("account|client", "ip|client", "global"))
            self.assertIsNotNone(reservation)
            limiter.finish(reservation, succeeded=False)

        self.assertIsNone(limiter.reserve(("account|client", "ip|other", "global")))

    def test_login_rate_limiter_sweeps_keys_that_are_never_queried_again(self):
        """撞庫攻擊每次換一個 XFF 或帳號就是一把新鑰匙，只出現一次的 key
        永遠不會再被 _prune 查到——不整份掃的話，字典只進不出直到記憶體吃光。"""
        limiter = LoginRateLimiter(max_failures=3, window_seconds=60)
        limiter.SWEEP_EVERY = 4
        with patch("app.auth.time.monotonic") as clock:
            clock.return_value = 0.0
            for n in range(3):
                reservation = limiter.reserve((f"account|user-{n}", f"ip|10.0.0.{n}", "global"))
                limiter.finish(reservation, succeeded=False)
            # 視窗過了之後來的下一次失敗要順手把上面三把清掉。
            clock.return_value = 120.0
            reservation = limiter.reserve(("account|fresh", "ip|10.9.9.9", "global"))
            limiter.finish(reservation, succeeded=False)

        self.assertEqual(set(limiter._failures), {"account|fresh", "ip|10.9.9.9", "global"})

    def test_login_reservations_atomically_bound_concurrent_verifiers(self):
        limiter = LoginRateLimiter(max_failures=5, window_seconds=60, max_concurrent=2)
        first = limiter.reserve(("account|one", "ip|1", "global"))
        second = limiter.reserve(("account|two", "ip|2", "global"))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(limiter.reserve(("account|three", "ip|3", "global")))
        limiter.finish(first, succeeded=False)
        self.assertIsNotNone(limiter.reserve(("account|three", "ip|3", "global")))

    def test_success_does_not_clear_ip_or_global_failure_history(self):
        limiter = LoginRateLimiter(max_failures=3, window_seconds=60)
        keys = ("account|one", "ip|1", "global")
        failed = limiter.reserve(keys)
        limiter.finish(failed, succeeded=False)
        successful = limiter.reserve(keys)
        limiter.finish(successful, succeeded=True)
        self.assertNotIn("account|one", limiter._failures)
        self.assertEqual(len(limiter._failures["ip|1"]), 1)
        self.assertEqual(len(limiter._failures["global"]), 1)

    def test_limiter_storage_is_bounded_for_unknown_accounts(self):
        limiter = LoginRateLimiter(max_failures=5, window_seconds=60, max_keys=12)
        for number in range(20):
            reservation = limiter.reserve((f"account|unknown-{number}", f"ip|{number}", "global"))
            if reservation:
                limiter.finish(reservation, succeeded=False)
        self.assertLessEqual(len(limiter._failures) + len(limiter._active), 12)

    def test_password_reset_cannot_leave_old_password_session(self):
        self.auth.create_or_reset_user("designer", "first-password-for-tests")
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
                result["login"] = self.auth.login("designer", "first-password-for-tests")
            except ValueError:
                result["login"] = None

        self.auth._verify_password = delayed_verify
        login_thread = threading.Thread(target=login)
        login_thread.start()
        self.assertTrue(entered_verify.wait(3))

        reset_thread = threading.Thread(
            target=lambda: self.auth.create_or_reset_user("designer", "second-password-for-tests")
        )
        reset_thread.start()
        continue_verify.set()
        login_thread.join(5)
        reset_thread.join(5)

        token = result.get("login", (None,))[0] if result.get("login") else None
        self.assertIsNone(self.auth.authenticate(token))


if __name__ == "__main__":
    unittest.main()
