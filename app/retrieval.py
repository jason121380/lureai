from dataclasses import dataclass

from .storage import KnowledgeStore
from .text_utils import cjk_bigrams, fts_query, search_tokens


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    title: str
    source_file: str
    locator: str
    section_title: str
    text: str
    category: str
    score: float

    def citation(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "title": self.title,
            "source_file": self.source_file,
            "locator": self.locator,
            "section_title": self.section_title,
            "score": round(self.score, 4),
            "text": self.text,
        }


class Retriever:
    def __init__(self, store: KnowledgeStore):
        self.store = store

    def retrieve(self, question: str, limit: int = 6) -> list[SearchHit]:
        query_tokens = set(search_tokens(question))
        if not query_tokens:
            return []
        rows = self.store.search_fts(fts_query(question), limit=max(limit * 8, 50))
        query_bigrams = cjk_bigrams(question)
        hits: list[SearchHit] = []
        for row in rows:
            document_tokens = set(row["search_text"].split())
            overlap = len(query_tokens & document_tokens) / max(1, len(query_tokens))
            if overlap <= 0:
                continue
            field_matches = len(query_bigrams & cjk_bigrams(f"{row['title']} {row['section_title']}"))
            content_matches = len(query_bigrams & cjk_bigrams(row["text"]))
            field_score = min(0.30, field_matches * 0.085)
            content_score = min(0.12, content_matches * 0.02)
            score = min(1.0, 0.58 + field_score + content_score)
            hits.append(SearchHit(
                chunk_id=row["chunk_id"],
                title=row["title"],
                source_file=row["source_file"],
                locator=row["locator"],
                section_title=row["section_title"],
                text=row["text"],
                category=row["category"],
                score=round(score, 4),
            ))
        hits.sort(key=lambda hit: (-hit.score, hit.title, hit.locator))
        return hits[:limit]
