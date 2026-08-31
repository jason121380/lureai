import json
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

    def test_ingest_accepts_only_approved_customer_chunks(self):
        path = self.write_jsonl([
            approved_chunk(),
            approved_chunk(chunk_id="chunk-2", review_status="pending"),
            approved_chunk(chunk_id="chunk-3", access_level="restricted"),
        ])

        report = ingest_jsonl(self.store, path)

        self.assertEqual(report.imported, 1)
        self.assertEqual(report.rejected, 2)
        self.assertEqual(self.store.count_chunks(), 1)

    def test_validate_chunk_requires_citation_fields(self):
        valid, errors = validate_chunk(approved_chunk(locator=""))

        self.assertFalse(valid)
        self.assertIn("locator", " ".join(errors))

    def test_reindex_replaces_previous_knowledge_atomically(self):
        ingest_jsonl(self.store, self.write_jsonl([approved_chunk()]))
        second = self.write_jsonl([approved_chunk(chunk_id="chunk-new", text="新的核准內容")])

        ingest_jsonl(self.store, second)

        self.assertEqual(self.store.count_chunks(), 1)
        self.assertEqual(self.store.get_chunk("chunk-new")["text"], "新的核准內容")
        self.assertIsNone(self.store.get_chunk("chunk-1"))


if __name__ == "__main__":
    unittest.main()
