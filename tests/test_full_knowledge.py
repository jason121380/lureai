import json
import unittest
from pathlib import Path

from scripts.build_full_knowledge import (
    conversation_chunk,
    collect_names,
    sanitize_deployable_text,
    sanitize_message,
    split_text,
)
from scripts.export_deploy_knowledge import (
    collect_deploy_names,
    deployable_row,
    validate_deployable_rows,
)


class FullKnowledgePipelineTests(unittest.TestCase):
    def test_bundled_designer_knowledge_contains_full_approved_index(self):
        path = Path(__file__).resolve().parents[1] / "knowledge" / "designer_coaching_process.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertGreaterEqual(len(rows), 100)
        self.assertTrue(all(row.get("review_status") == "approved" for row in rows))
        self.assertTrue(all(row.get("access_level") == "internal_coaching" for row in rows))
        self.assertEqual(validate_deployable_rows(rows, set()), [])

    def test_bundled_knowledge_is_readable_prose_not_raw_dumps(self):
        path = Path(__file__).resolve().parents[1] / "knowledge" / "designer_coaching_process.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        for row in rows:
            text = row["text"]
            self.assertNotRegex(text, r"(?m)^[A-Z]{1,2}\d{1,3}=", row["chunk_id"])
            self.assertNotIn("[人名]", text, row["chunk_id"])
            self.assertNotIn("[敏感資訊已移除]", text, row["chunk_id"])
            self.assertGreaterEqual(len(text), 80, row["chunk_id"])

    def test_deployable_redaction_removes_filled_identity_fields(self):
        text = "編號：7號 姓名：許博棠\n店家：晨安店 電話：02-12345678\n地址：台北市測試路1號"

        sanitized = sanitize_deployable_text(text)

        self.assertNotIn("許博棠", sanitized)
        self.assertNotIn("晨安店", sanitized)
        self.assertNotIn("02-12345678", sanitized)
        self.assertNotIn("台北市測試路1號", sanitized)
        self.assertIn("[已移除]", sanitized)

    def test_deployable_export_excludes_staff_contact_directories(self):
        row = {
            "chunk_id": "source-doc:directory",
            "section_title": "工作表：各部門分機EMAIL",
            "text": "部門 | 職稱 | 姓名 | 分機 | Email\n營業部 | 專員 | 王小明 | 811 | user@example.com",
        }

        self.assertIsNone(deployable_row(row))

    def test_deployable_export_redacts_names_found_next_to_titles(self):
        row = {
            "chunk_id": "source-doc:leadership",
            "text": "張連財 副總\n董事長\n鄭茂發\n《管偉宏老師》\n張連財,鄭茂發工作分組",
        }
        names = collect_deploy_names([row])

        deployed = deployable_row(row, names)

        self.assertEqual(names, {"張連財", "鄭茂發", "管偉宏"})
        self.assertNotIn("張連財", deployed["text"])
        self.assertNotIn("鄭茂發", deployed["text"])
        self.assertNotIn("管偉宏", deployed["text"])

    def test_deploy_name_detection_includes_role_context_and_latin_aliases(self):
        rows = [{
            "historical_example": True,
            "text": "講師：陳小明\n市長參選人 林大華\n教練：傑西你要換素材嗎\nPS23 Monica\n#MonicaHair",
        }]

        names = collect_deploy_names(rows)

        for name in ("陳小明", "林大華", "傑西", "Monica", "MonicaHair"):
            self.assertIn(name, names)

    def test_deployable_validation_fails_on_remaining_private_data(self):
        rows = [{
            "chunk_id": "source-doc:unsafe",
            "text": "聯絡 user@example.com",
            "source_file": "raw/private/person.xlsx",
        }]

        errors = validate_deployable_rows(rows, {"王小明"})

        self.assertIn("source-doc:unsafe: private contact data", errors)
        self.assertIn("source-doc:unsafe: unsafe source path", errors)

    def test_deployable_validation_scans_public_metadata_fields(self):
        row = {
            "chunk_id": "source-doc:unsafe-metadata",
            "text": "一般流程內容",
            "title": "王小明的教材",
            "section_title": "聯絡 user@example.com",
            "locator": "第 1 段",
            "source_file": "source_documents/0123456789abcdef.md",
        }

        errors = validate_deployable_rows([row], {"王小明"})

        self.assertIn("source-doc:unsafe-metadata: private contact data", errors)
        self.assertIn("source-doc:unsafe-metadata: detected personal name", errors)

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
