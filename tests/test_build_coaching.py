import tempfile
import unittest
from pathlib import Path

from scripts.build_coaching_knowledge import build_rows, parse_sections


class BuildCoachingKnowledgeTests(unittest.TestCase):
    def test_parse_sections_reads_tag_category_title_and_body(self):
        source = """# 文件\n\n## [coach-01 | 使用範圍] 任務邊界\n\n只能使用核准資料。\n"""

        sections = parse_sections(source)

        self.assertEqual(sections, [{
            "locator": "coach-01",
            "category": "使用範圍",
            "section_title": "任務邊界",
            "text": "只能使用核准資料。",
        }])

    def test_build_rows_sets_internal_access_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coaching.md"
            path.write_text(
                "# 文件\n\n## [coach-01 | 私訊流程] 第一輪回覆\n\n先回答，再問需求。\n",
                encoding="utf-8",
            )

            rows = build_rows(
                path,
                evidence_name="source.json",
                evidence_sha256="abc123",
                reviewed_at="2026-08-31",
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chunk_id"], "designer-coaching-process:coach-01:1")
        self.assertEqual(rows[0]["access_level"], "internal_coaching")
        self.assertTrue(rows[0]["rag_allowed"])
        self.assertEqual(rows[0]["review_status"], "approved")
        self.assertEqual(rows[0]["evidence_source"], "source.json")
        self.assertEqual(rows[0]["evidence_sha256"], "abc123")
        self.assertEqual(len(rows[0]["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
