import json
import tempfile
import unittest
from pathlib import Path

from app.domains import COACHING, DOMAIN_LABELS, OPERATIONS, classify, domain_of
from app.server import build_custom_chunk
from app.storage import KnowledgeStore


KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge" / "designer_coaching_process.jsonl"


def chunk(**overrides) -> dict:
    row = {
        "chunk_id": "test:1",
        "locator": "t-1",
        "section_title": "測試",
        "text": "內容",
        "title": "測試知識",
        "source_file": "source_documents/a.md",
        "category": "店務營運",
        "access_level": "internal_coaching",
        "review_status": "approved",
        "search_text": "測試 內容",
    }
    row.update(overrides)
    return row


class DomainClassificationTests(unittest.TestCase):
    def test_two_domains_are_named_as_the_product_defines_them(self):
        self.assertEqual(DOMAIN_LABELS[OPERATIONS], "店務營運管理")
        self.assertEqual(DOMAIN_LABELS[COACHING], "設計師一對一行銷輔導")

    def test_teaching_categories_fall_under_operations(self):
        for category in ("店務營運", "企業知識", "顧客服務", "美髮技術"):
            self.assertEqual(classify(category), OPERATIONS)

    def test_coaching_categories_and_curated_source_fall_under_coaching(self):
        self.assertEqual(classify("私訊流程"), COACHING)
        self.assertEqual(classify("數位行銷"), COACHING)
        # coach-40 is filed under 店務營運 but is curated coaching knowledge.
        self.assertEqual(
            classify("店務營運", "knowledge/designer_coaching_process.md"), COACHING
        )

    def test_explicit_domain_on_a_row_wins_over_inference(self):
        self.assertEqual(domain_of(chunk(domain=COACHING)), COACHING)
        self.assertEqual(domain_of(chunk(domain="nonsense")), OPERATIONS)


class BundledKnowledgeDomainTests(unittest.TestCase):
    def test_every_bundled_chunk_declares_one_of_the_two_domains(self):
        rows = [json.loads(line) for line in KNOWLEDGE.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertTrue(all(row.get("domain") in DOMAIN_LABELS for row in rows))
        counts = {key: sum(row["domain"] == key for row in rows) for key in DOMAIN_LABELS}
        self.assertEqual(sum(counts.values()), len(rows))
        for key, count in counts.items():
            self.assertGreater(count, 0, f"{key} 沒有任何知識")

    def test_curated_coaching_chunks_are_all_in_the_coaching_domain(self):
        rows = [json.loads(line) for line in KNOWLEDGE.read_text(encoding="utf-8").splitlines() if line.strip()]
        curated = [row for row in rows if row["source_file"].startswith("knowledge/")]

        self.assertTrue(curated)
        self.assertTrue(all(row["domain"] == COACHING for row in curated))


class StoreDomainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = KnowledgeStore(Path(self.directory.name) / "knowledge.db")
        self.addCleanup(self.store.close)
        self.store.replace_chunks([
            chunk(chunk_id="ops:1", locator="o-1", category="店務營運"),
            chunk(chunk_id="ops:2", locator="o-2", category="顧客服務"),
            chunk(
                chunk_id="coach:1", locator="c-1", category="私訊流程",
                source_file="knowledge/designer_coaching_process.md",
            ),
        ])

    def test_chunks_are_stored_under_their_domain(self):
        stored = {row["chunk_id"]: row["domain"] for row in self.store.list_chunks(limit=10)}

        self.assertEqual(stored["ops:1"], OPERATIONS)
        self.assertEqual(stored["coach:1"], COACHING)

    def test_list_chunks_filters_by_domain(self):
        coaching = self.store.list_chunks(limit=10, domain=COACHING)
        operations = self.store.list_chunks(limit=10, domain=OPERATIONS)

        self.assertEqual([row["chunk_id"] for row in coaching], ["coach:1"])
        self.assertEqual(len(operations), 2)

    def test_composition_groups_categories_under_both_domains(self):
        composition = self.store.knowledge_composition()

        domains = {item["key"]: item for item in composition["domains"]}
        self.assertEqual([item["key"] for item in composition["domains"]], [OPERATIONS, COACHING])
        self.assertEqual(domains[OPERATIONS]["count"], 2)
        self.assertEqual(domains[COACHING]["count"], 1)
        self.assertEqual(domains[COACHING]["label"], "設計師一對一行銷輔導")
        self.assertEqual(
            sorted(item["name"] for item in domains[OPERATIONS]["categories"]),
            ["店務營運", "顧客服務"],
        )

    def test_migrating_an_old_database_backfills_the_domain_column(self):
        path = Path(self.directory.name) / "legacy.db"
        legacy = KnowledgeStore(path)
        legacy.connection.execute("ALTER TABLE chunks DROP COLUMN domain")
        legacy.connection.commit()
        legacy.close()

        migrated = KnowledgeStore(path)
        self.addCleanup(migrated.close)
        migrated.replace_chunks([chunk(category="私訊流程")])

        self.assertEqual(migrated.list_chunks(limit=1)[0]["domain"], COACHING)


class CustomChunkDomainTests(unittest.TestCase):
    def test_admin_knowledge_takes_the_domain_from_the_form(self):
        built = build_custom_chunk(
            {"section_title": "標題", "text": "內容", "category": "店務營運", "domain": COACHING},
            "internal_coaching",
        )

        self.assertEqual(built["domain"], COACHING)

    def test_admin_knowledge_without_a_domain_is_inferred_from_the_category(self):
        built = build_custom_chunk(
            {"section_title": "標題", "text": "內容", "category": "店務營運"},
            "internal_coaching",
        )

        self.assertEqual(built["domain"], OPERATIONS)

    def test_an_unknown_domain_is_rejected(self):
        with self.assertRaises(ValueError):
            build_custom_chunk(
                {"section_title": "標題", "text": "內容", "domain": "marketing"},
                "internal_coaching",
            )


if __name__ == "__main__":
    unittest.main()
