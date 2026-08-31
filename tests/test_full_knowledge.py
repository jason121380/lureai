import unittest

from scripts.build_full_knowledge import conversation_chunk, collect_names, sanitize_message, split_text


class FullKnowledgePipelineTests(unittest.TestCase):
    def test_collect_names_includes_conversation_and_sender_aliases(self):
        conversations = [{
            "conv_name": "[W1v1] Mia - Example Salon",
            "messages": [{"sender": "Eric Chen"}],
        }]

        names = collect_names(conversations)

        self.assertIn("Mia", names)
        self.assertIn("Example Salon", names)
        self.assertIn("Eric", names)

    def test_sanitize_message_removes_contact_details_and_names(self):
        text = "@Mia 請找 ERIC，電話 0912-345-678，信箱 user@example.com https://example.com"

        sanitized = sanitize_message(text, {"Mia", "Eric"})

        self.assertNotIn("Mia", sanitized)
        self.assertNotIn("ERIC", sanitized)
        self.assertNotIn("0912", sanitized)
        self.assertNotIn("example.com", sanitized)
        self.assertIn("[敏感資訊已移除]", sanitized)

    def test_sanitize_message_removes_concatenated_aliases(self):
        sanitized = sanitize_message("跟yulisa聊到的嗎", {"Yuli", "sa"})

        self.assertNotIn("yuli", sanitized.lower())
        self.assertNotIn("sa", sanitized.lower())

    def test_sanitize_message_removes_physical_address(self):
        sanitized = sanitize_message("地址在台北市中山區松江路297巷17號1樓", set())

        self.assertIn("[地址已移除]", sanitized)
        self.assertNotIn("松江路", sanitized)

    def test_conversation_rag_masks_historical_numbers(self):
        row = conversation_chunk("case", 1, 1, [{
            "role": "教練", "month": "2026-08", "text": "私訊 79，成本 $6,889，點擊率 4.15%",
        }])

        self.assertIn("[歷史數值]", row["text"])
        self.assertNotIn("6,889", row["text"])
        self.assertNotIn("4.15%", row["text"])
        self.assertIn("非現行標準", row["text"])

    def test_split_text_respects_chunk_limit(self):
        chunks = split_text("。".join(["這是一段可索引的測試內容" * 12] * 20), max_chars=400)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 400 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
