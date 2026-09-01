"""AI 模型校調：後台看到的規則，跟實際送給模型的指令必須是同一份。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from app import tuning
from app.answer import TONE_INSTRUCTIONS, AnswerEngine
from app.policy import PolicyEngine
from app.server import AppContext, create_server

from tests.test_api import ServerTestCase

ROOT = Path(__file__).resolve().parents[1]


class ComposeTests(unittest.TestCase):
    """預設值必須跟改動前逐字相同——校調頁不能悄悄改變 AI 的行為。"""

    def test_policy_round_trips_byte_for_byte(self):
        original = (ROOT / "config" / "designer_coach_policy.md").read_text(encoding="utf-8")

        self.assertEqual(tuning.compose_policy(), original)

    def test_every_tone_round_trips_byte_for_byte(self):
        for tone in ("expert", "service", "line"):
            with self.subTest(tone=tone):
                self.assertEqual(tuning.compose_tone(tone), TONE_INSTRUCTIONS[tone])

    def test_override_replaces_only_that_rule(self):
        composed = tuning.compose_tone("service", {"service-01": "改成這句"})

        self.assertIn("改成這句", composed)
        self.assertNotIn(tuning.TONE_GROUPS[1]["rules"][0]["text"], composed)
        # 其餘規則原封不動
        self.assertIn(tuning.TONE_GROUPS[1]["rules"][1]["text"], composed)

    def test_blank_override_falls_back_to_the_default(self):
        self.assertEqual(tuning.compose_tone("service", {"service-01": "   "}),
                         tuning.compose_tone("service"))

    def test_catalogue_covers_every_rule_id(self):
        ids = tuning.known_rule_ids()

        self.assertIn("service-01", ids)
        self.assertIn("policy-00", ids)
        self.assertIn("line-01", ids)


class EngineTests(unittest.TestCase):
    def test_answer_engine_uses_the_overrides(self):
        engine = AnswerEngine(
            policy_path=ROOT / "config" / "designer_coach_policy.md",
            rules_provider=lambda: {"service-01": "後台改過的第一句"},
        )

        self.assertIn("後台改過的第一句", engine.instructions("service"))

    def test_answer_engine_ignores_a_broken_provider(self):
        def boom():
            raise RuntimeError("db down")

        engine = AnswerEngine(
            policy_path=ROOT / "config" / "designer_coach_policy.md", rules_provider=boom
        )

        self.assertIn(tuning.TONE_GROUPS[1]["rules"][0]["text"], engine.instructions("service"))

    def test_fixed_replies_can_be_overridden(self):
        policy = PolicyEngine(rules_provider=lambda: {
            "reply-fallback": "改過的查無資料說法",
            "reply-identity": "改過的身分回答",
        })

        self.assertEqual(policy.fallback_message, "改過的查無資料說法")
        self.assertEqual(policy.boundary_reply("你是ai嗎").message, "改過的身分回答")


class TuningApiTests(ServerTestCase):
    def test_catalogue_lists_every_group_for_admins(self):
        status, body = self.request("GET", "/api/admin/tuning", token="secret-token")

        self.assertEqual(status, 200)
        self.assertEqual(
            [group["id"] for group in body["groups"]],
            ["policy", "tone_expert", "tone_service", "tone_line", "fixed_replies"],
        )
        self.assertEqual(body["customized"], 0)

    def test_catalogue_requires_admin(self):
        status, _body = self.request("GET", "/api/admin/tuning")

        self.assertEqual(status, 401)

    def test_saving_a_rule_changes_what_the_model_receives(self):
        before = self.request("GET", "/api/admin/tuning/preview?tone=service", token="secret-token")[1]

        status, _body = self.request("POST", "/api/admin/tuning", {
            "rule_id": "service-01", "text": "後台改過的第一句",
        }, token="secret-token")
        after = self.request("GET", "/api/admin/tuning/preview?tone=service", token="secret-token")[1]

        self.assertEqual(status, 200)
        self.assertIn("後台改過的第一句", after["instructions"])
        self.assertNotEqual(before["instructions"], after["instructions"])
        self.assertEqual(
            self.request("GET", "/api/admin/tuning", token="secret-token")[1]["customized"], 1
        )

    def test_resetting_a_rule_restores_the_default(self):
        before = self.request("GET", "/api/admin/tuning/preview?tone=service", token="secret-token")[1]
        self.request("POST", "/api/admin/tuning", {
            "rule_id": "service-01", "text": "暫時改一下",
        }, token="secret-token")

        self.request("POST", "/api/admin/tuning/reset", {"rule_id": "service-01"}, token="secret-token")
        after = self.request("GET", "/api/admin/tuning/preview?tone=service", token="secret-token")[1]

        self.assertEqual(before["instructions"], after["instructions"])

    def test_clearing_the_text_also_restores_the_default(self):
        self.request("POST", "/api/admin/tuning", {
            "rule_id": "service-01", "text": "暫時改一下",
        }, token="secret-token")

        self.request("POST", "/api/admin/tuning", {"rule_id": "service-01", "text": "  "}, token="secret-token")

        self.assertEqual(
            self.request("GET", "/api/admin/tuning", token="secret-token")[1]["customized"], 0
        )

    def test_unknown_rule_is_rejected(self):
        status, body = self.request("POST", "/api/admin/tuning", {
            "rule_id": "not-a-rule", "text": "x",
        }, token="secret-token")

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_rule")

    def test_overlong_rule_is_rejected(self):
        status, body = self.request("POST", "/api/admin/tuning", {
            "rule_id": "service-01", "text": "字" * 4001,
        }, token="secret-token")

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "too_long")

    def test_reset_all_clears_every_override(self):
        self.request("POST", "/api/admin/tuning", {"rule_id": "service-01", "text": "改一下"}, token="secret-token")
        self.request("POST", "/api/admin/tuning", {"rule_id": "line-01", "text": "也改一下"}, token="secret-token")

        self.request("POST", "/api/admin/tuning/reset", {}, token="secret-token")

        self.assertEqual(
            self.request("GET", "/api/admin/tuning", token="secret-token")[1]["customized"], 0
        )


if __name__ == "__main__":
    unittest.main()
