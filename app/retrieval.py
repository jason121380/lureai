from dataclasses import dataclass

from .storage import KnowledgeStore
from .text_utils import cjk_bigrams, fts_query, search_tokens


GENERIC_QUERY_TOKENS = {
    "如何", "何處", "什麼", "怎麼", "麼樣", "可以", "是否", "請問", "幫我",
    "一下", "現在", "今天", "明天", "知道", "告訴", "問題", "需要", "應該",
}


def relevance_tokens(text: str) -> set[str]:
    return {token for token in search_tokens(text) if token not in GENERIC_QUERY_TOKENS}


def relevance_bigrams(text: str) -> set[str]:
    return {token for token in cjk_bigrams(text) if token not in GENERIC_QUERY_TOKENS}


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
        query_tokens = relevance_tokens(question)
        if not query_tokens:
            return []
        rows = self.store.search_fts(fts_query(question), limit=max(limit * 8, 50))
        query_bigrams = relevance_bigrams(question)
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
            overlap_score = min(0.20, overlap * 0.65)
            curated_score = 0.04 if str(row["source_file"]).startswith("knowledge/") else 0.0
            score = min(1.0, 0.50 + overlap_score + field_score + content_score + curated_score)
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
        hits.sort(key=lambda hit: (
            -hit.score,
            not hit.source_file.startswith("knowledge/"),
            hit.title,
            hit.locator,
        ))
        return hits[:limit]
