import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.ingest import ingest_jsonl, validate_chunk
from app.storage import KnowledgeStore


def approved_chunk(**overrides):
    row = {
        "chunk_id": "chunk-1",
        "doc_id": "doc-1",
        "locator": "aftercare-1",
        "section_title": "居家照護",
        "text": "燙髮後請依設計師說明整理，避免自行承諾效果。",
        "title": "燙髮後照護",
        "source_file": "curated/燙髮後照護.md",
        "source_sha256": "abc123",
        "category": "顧客服務",
        "access_level": "customer_service",
        "customer_service_allowed": True,
        "review_status": "approved",
    }
    row.update(overrides)
    return row


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root / "knowledge.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write_jsonl(self, rows):
        path = self.root / "knowledge.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        return path

    def test_ingest_rejects_entire_batch_when_any_chunk_is_invalid(self):
        path = self.write_jsonl([
            approved_chunk(),
            approved_chunk(chunk_id="chunk-2", review_status="pending"),
            approved_chunk(chunk_id="chunk-3", access_level="restricted"),
        ])

        with self.assertRaisesRegex(ValueError, "知識檔包含 2 筆未核准或無效資料"):
            ingest_jsonl(self.store, path)

        self.assertEqual(self.store.count_chunks(), 0)

    def test_validate_chunk_requires_citation_fields(self):
        valid, errors = validate_chunk(approved_chunk(locator=""))

        self.assertFalse(valid)
        self.assertIn("locator", " ".join(errors))

    def test_internal_coaching_chunk_requires_explicit_profile(self):
        row = approved_chunk(
            access_level="internal_coaching",
            customer_service_allowed=False,
            rag_allowed=True,
        )

        default_valid, _ = validate_chunk(row)
        coaching_valid, errors = validate_chunk(row, expected_access_level="internal_coaching")

        self.assertFalse(default_valid)
        self.assertTrue(coaching_valid, errors)

    def test_ingest_accepts_only_matching_internal_coaching_chunks(self):
        path = self.write_jsonl([
            approved_chunk(
                chunk_id="coach-1",
                access_level="internal_coaching",
                customer_service_allowed=False,
                rag_allowed=True,
            ),
        ])

        report = ingest_jsonl(self.store, path, expected_access_level="internal_coaching")

        self.assertEqual(report.imported, 1)
        self.assertEqual(report.rejected, 0)
        self.assertIsNotNone(self.store.get_chunk("coach-1"))

    def test_reindex_replaces_previous_knowledge_atomically(self):
        ingest_jsonl(self.store, self.write_jsonl([approved_chunk()]))
        second = self.write_jsonl([approved_chunk(chunk_id="chunk-new", text="新的核准內容")])

        ingest_jsonl(self.store, second)

        self.assertEqual(self.store.count_chunks(), 1)
        self.assertEqual(self.store.get_chunk("chunk-new")["text"], "新的核准內容")
        self.assertIsNone(self.store.get_chunk("chunk-1"))

    def test_ingest_records_source_digest(self):
        source = self.write_jsonl([approved_chunk()])

        ingest_jsonl(self.store, source)

        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(self.store.get_metadata("knowledge_sha256"), expected)

    def test_invalid_update_preserves_last_known_good_index_and_digest(self):
        original = self.write_jsonl([approved_chunk()])
        ingest_jsonl(self.store, original)
        original_digest = self.store.get_metadata("knowledge_sha256")
        invalid = self.write_jsonl([approved_chunk(chunk_id="bad", review_status="pending")])

        with self.assertRaises(ValueError):
            ingest_jsonl(self.store, invalid)

        self.assertIsNotNone(self.store.get_chunk("chunk-1"))
        self.assertIsNone(self.store.get_chunk("bad"))
        self.assertEqual(self.store.get_metadata("knowledge_sha256"), original_digest)


if __name__ == "__main__":
    unittest.main()
