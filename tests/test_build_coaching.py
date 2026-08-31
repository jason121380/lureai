import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_knowledge_index import (
    PLAYBOOKS,
    build_aliases,
    build_rows,
    key_phrases,
    parse_sections,
)


ROOT = Path(__file__).resolve().parents[1]


class ParseSectionTests(unittest.TestCase):
    def test_parse_sections_reads_tag_category_title_and_body(self):
        source = "# 文件\n\n## [coach-01 | 使用範圍] 任務邊界\n\n只能使用核准資料。\n"

        sections = parse_sections(source, "coach")

        self.assertEqual(sections, [{
            "locator": "coach-01",
            "category": "使用範圍",
            "section_title": "任務邊界",
            "text": "只能使用核准資料。",
        }])

    def test_duplicate_locators_are_rejected(self):
        source = (
            "## [chat-01 | 對話健檢] 一\n\n內容一。\n\n"
            "## [chat-01 | 對話健檢] 二\n\n內容二。\n"
        )

        with self.assertRaises(ValueError):
            parse_sections(source, "chat")


class AliasTests(unittest.TestCase):
    def setUp(self):
        self.section = {
            "locator": "chat-10",
            "category": "對話健檢",
            "section_title": "二選一提問法",
            "text": "- 二選一的意思是：不要問「你要不要來」，要問「你比較方便週三還是週五」。\n- 每次只給兩個選項。",
        }

    def test_key_phrases_come_from_the_title_and_bullet_openings(self):
        phrases = key_phrases(self.section)

        self.assertEqual(phrases[0], "二選一提問法")
        self.assertTrue(all(3 <= len(phrase) <= 14 for phrase in phrases))

    def test_seed_questions_are_kept_and_templates_expand_them(self):
        aliases = build_aliases(self.section, ["二選一怎麼問"])

        self.assertEqual(aliases[0], "二選一怎麼問")
        self.assertIn("二選一提問法怎麼做", aliases)
        self.assertLessEqual(len(aliases), 60)


class BuildIndexTests(unittest.TestCase):
    def test_every_playbook_file_exists_and_compiles(self):
        for playbook in PLAYBOOKS:
            self.assertTrue((ROOT / playbook["path"]).is_file(), playbook["path"])

        rows = build_rows("2026-08-31")

        self.assertGreaterEqual(len(rows), 200)
        self.assertTrue(all(row["access_level"] == "internal_coaching" for row in rows))
        self.assertTrue(all(row["review_status"] == "approved" for row in rows))
        self.assertTrue(all(row["rag_allowed"] for row in rows))
        self.assertEqual(len({row["chunk_id"] for row in rows}), len(rows))

    def test_index_carries_a_question_alias_bank(self):
        rows = build_rows("2026-08-31")

        aliases = sum(len(row["aliases"]) for row in rows)
        self.assertGreaterEqual(aliases, 10000)
        self.assertTrue(all(row["aliases"] for row in rows))

    def test_written_index_matches_the_playbooks(self):
        bundled = [
            json.loads(line)
            for line in (ROOT / "knowledge" / "designer_coaching_process.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rebuilt = build_rows(bundled[0]["reviewed_at"])

        self.assertEqual(
            [row["chunk_id"] for row in bundled], [row["chunk_id"] for row in rebuilt],
            "knowledge/*.md 有改動但沒重新編譯索引",
        )


if __name__ == "__main__":
    unittest.main()
