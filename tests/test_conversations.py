"""對話紀錄存伺服器：換裝置、重新部署都要看得到（使用者決定要存資料庫）。"""
import unittest

from tests.test_api import ServerTestCase


class ConversationApiTests(ServerTestCase):
    def login(self):
        self.request("POST", "/api/auth/login", {
            "username": "designer", "password": "designer-password",
        })

    def test_listing_requires_a_login(self):
        status, _body = self.request("GET", "/api/conversations")

        self.assertEqual(status, 401)

    def test_saved_conversation_comes_back(self):
        self.login()

        self.request("POST", "/api/conversations", {"conversations": [{
            "id": "c-1", "title": "客人嫌貴", "tone": "service",
            "messages": [{"role": "user", "content": "客人說太貴了"}],
            "createdAt": "2026-09-01T00:00:00Z", "updatedAt": "2026-09-01T00:00:00Z",
        }]})
        status, body = self.request("GET", "/api/conversations")

        self.assertEqual(status, 200)
        self.assertEqual(len(body["conversations"]), 1)
        conversation = body["conversations"][0]
        self.assertEqual(conversation["title"], "客人嫌貴")
        self.assertEqual(conversation["messages"][0]["content"], "客人說太貴了")

    def test_a_new_device_sees_the_same_history(self):
        """同一個帳號從另一個瀏覽器登入，要拿得到同一份紀錄。"""
        self.login()
        self.request("POST", "/api/conversations", {"conversations": [{
            "id": "c-1", "title": "客人嫌貴", "messages": [{"role": "user", "content": "客人說太貴了"}],
        }]})

        # 換一組 cookie＝換一台裝置
        self.client = self.fresh_client()
        self.login()
        status, body = self.request("GET", "/api/conversations")

        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in body["conversations"]], ["c-1"])

    def test_updating_a_conversation_replaces_its_messages(self):
        self.login()
        self.request("POST", "/api/conversations", {"conversations": [{
            "id": "c-1", "title": "第一版", "messages": [{"role": "user", "content": "一"}],
        }]})

        self.request("POST", "/api/conversations", {"conversations": [{
            "id": "c-1", "title": "第二版", "rev": 1, "expected_rev": 0,
            "messages": [{"role": "user", "content": "一"}, {"role": "assistant", "content": "二"}],
        }]})
        body = self.request("GET", "/api/conversations")[1]

        self.assertEqual(len(body["conversations"]), 1)
        self.assertEqual(body["conversations"][0]["title"], "第二版")
        self.assertEqual(len(body["conversations"][0]["messages"]), 2)

    def test_deleting_removes_it_everywhere(self):
        self.login()
        self.request("POST", "/api/conversations", {"conversations": [{
            "id": "c-1", "title": "要刪掉的", "messages": [{"role": "user", "content": "一"}],
        }]})

        self.request("POST", "/api/conversations/delete", {"id": "c-1"})

        self.assertEqual(self.request("GET", "/api/conversations")[1]["conversations"], [])

    def test_loading_placeholders_are_not_persisted(self):
        """存到一半的「產生中」訊息重新載入會一直轉圈，存之前先清掉。"""
        self.login()

        self.request("POST", "/api/conversations", {"conversations": [{
            "id": "c-1", "title": "t",
            "messages": [{"role": "assistant", "content": "", "loading": True, "pendingReveal": True}],
        }]})
        message = self.request("GET", "/api/conversations")[1]["conversations"][0]["messages"][0]

        self.assertNotIn("loading", message)
        self.assertNotIn("pendingReveal", message)

    def test_same_revision_different_content_is_rejected_with_ack(self):
        self.login()
        item = {"id": "race", "title": "first", "rev": 1, "expected_rev": 0,
                "messages": [{"role": "user", "content": "first"}]}
        first = self.request("POST", "/api/conversations", {"conversations": [item]})[1]
        item["messages"][0]["content"] = "other device"
        result = self.request("POST", "/api/conversations", {"conversations": [item]})[1]
        self.assertEqual(first["acks"][0]["status"], "accepted")
        self.assertEqual(result["acks"][0]["status"], "conflict")
        self.assertEqual(self.request("GET", "/api/conversations")[1]["conversations"][0]["messages"][0]["content"], "first")

    def test_deleted_conversation_cannot_be_resurrected(self):
        self.login()
        item = {"id": "gone", "rev": 1, "messages": [{"content": "old"}]}
        self.request("POST", "/api/conversations", {"conversations": [item]})
        self.request("POST", "/api/conversations/delete", {"id": "gone"})
        item["rev"] = 999
        result = self.request("POST", "/api/conversations", {"conversations": [item]})[1]
        self.assertEqual(result["acks"][0]["status"], "deleted")
        remote = self.request("GET", "/api/conversations")[1]
        self.assertEqual(remote["conversations"], [])
        self.assertEqual(remote["tombstones"][0]["id"], "gone")

    def test_tone_preference_follows_the_account(self):
        self.login()

        self.request("POST", "/api/prefs", {"prefs": {"tone": "service"}})
        self.client = self.fresh_client()
        self.login()

        self.assertEqual(self.request("GET", "/api/conversations")[1]["prefs"]["tone"], "service")

    def test_unknown_preference_keys_are_ignored(self):
        self.login()

        self.request("POST", "/api/prefs", {"prefs": {"tone": "service", "role": "admin"}})

        self.assertEqual(
            self.request("GET", "/api/conversations")[1]["prefs"], {"tone": "service"}
        )


if __name__ == "__main__":
    unittest.main()
