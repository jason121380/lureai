import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .storage import KnowledgeStore
from .text_utils import cjk_bigrams, fts_query, normalize_for_search, search_tokens


DEFAULT_SYNONYM_PATH = Path(__file__).resolve().parent.parent / "config" / "synonyms.json"


def load_synonym_groups(path: str | Path | None = None) -> list[list[str]]:
    """Equivalent-term groups used to expand a question before matching."""
    source = Path(path or DEFAULT_SYNONYM_PATH)
    if not source.is_file():
        return []
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    groups = payload.get("groups") if isinstance(payload, dict) else payload
    if not isinstance(groups, list):
        return []
    return [
        [normalize_for_search(term).strip() for term in group if str(term).strip()]
        for group in groups
        if isinstance(group, list) and len(group) > 1
    ]


GENERIC_QUERY_TOKENS = {
    "如何", "何處", "什麼", "怎麼", "麼樣", "可以", "是否", "請問", "幫我",
    "一下", "現在", "今天", "明天", "知道", "告訴", "問題", "需要", "應該",
    # 「怎麼算」「怎麼辦」這類問句在斷詞後會留下跨字的 bigram，任何題目都會
    # 對上，導致不相關的問題也拿到高分。
    "麼算", "麼辦", "麼做", "麼寫", "麼看", "麼用", "麼回", "麼講", "麼查",
    "麼開", "麼分", "麼排", "麼選", "麼談", "要怎", "該怎", "怎樣", "我想",
    "想知", "有沒", "沒有", "是不", "不是", "可不", "不可", "要不", "不要",
}


def relevance_tokens(text: str) -> set[str]:
    return {token for token in search_tokens(text) if token not in GENERIC_QUERY_TOKENS}


def relevance_bigrams(text: str) -> set[str]:
    return {token for token in cjk_bigrams(text) if token not in GENERIC_QUERY_TOKENS}


@lru_cache(maxsize=512)
def alias_terms(text: str) -> frozenset[str]:
    """問法索引的比對詞：中英文都要算，因為問句常混英文（emoji、roas）。"""
    return frozenset(relevance_tokens(text))


# Evidence is squashed into [0.5, 1.0) instead of being clipped, so strongly
# matching chunks stay ordered rather than all landing on 1.0 and falling back
# to an alphabetical tie-break. 0.22 of evidence still maps to the historical
# 0.72 policy threshold.
_COMPRESSION = 2.63


def _compress(evidence: float) -> float:
    return round(0.50 + 0.50 * (1.0 - math.exp(-_COMPRESSION * max(0.0, evidence))), 4)


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
    def __init__(self, store: KnowledgeStore, synonym_path: str | Path | None = None):
        self.store = store
        self.synonym_groups = load_synonym_groups(synonym_path)

    def expand_question(self, question: str) -> str:
        """Append equivalent phrasings so '一週幾則' can reach '每週發布頻率'."""
        if not self.synonym_groups:
            return question
        normalized = normalize_for_search(question)
        extra: list[str] = []
        for group in self.synonym_groups:
            if any(term and term in normalized for term in group):
                extra.extend(group)
        if not extra:
            return question
        return f"{question} {' '.join(dict.fromkeys(extra))}"

    def retrieve(self, question: str, limit: int = 6) -> list[SearchHit]:
        core_tokens = relevance_tokens(question)
        if not core_tokens:
            return []
        expanded = self.expand_question(question)
        # Synonyms only ever add matches: they widen what counts as a hit while
        # the original question still sets the denominator.
        query_tokens = relevance_tokens(expanded)
        rows = self.store.search_fts(fts_query(expanded), limit=max(limit * 8, 60))
        query_bigrams = relevance_bigrams(expanded)
        hits: list[SearchHit] = []
        for row in rows:
            document_tokens = set(row["search_text"].split())
            matched = query_tokens & document_tokens
            if not matched:
                continue
            overlap = min(1.0, len(matched) / max(1, len(core_tokens)))
            section_bigrams = cjk_bigrams(f"{row['title']} {row['section_title']}")
            field_matches = len(query_bigrams & section_bigrams)
            content_matches = len(query_bigrams & cjk_bigrams(row["text"]))
            # 問法索引：設計師實際會怎麼問這塊知識，對得上就加分。
            alias_text = row["aliases"] if "aliases" in row.keys() else ""
            alias_matches = len((query_tokens | query_bigrams) & alias_terms(str(alias_text or "")))
            # 只靠問法模板對上（例如任何題目都有的「的做法」）不算數，必須同時
            # 命中這塊知識的標題或內文，否則不相關的問題會被拉高分數。
            grounded_in_content = bool(field_matches or content_matches)
            alias_score = min(0.20, alias_matches * 0.025) if grounded_in_content else 0.0
            field_score = min(0.30, field_matches * 0.085)
            content_score = min(0.12, content_matches * 0.02)
            overlap_score = min(0.20, overlap * 0.65)
            # 索引裡若還混有未策展的原始資料（例如私人完整索引），策展內容要
            # 夠力才不會被長逐字稿蓋過；全部都是策展內容時這個加分不影響排序。
            curated = str(row["source_file"]).startswith("knowledge/")
            curated_score = 0.06 if curated else 0.0
            section_focus = min(0.10, field_matches * 0.05) if curated else 0.0
            evidence = (
                overlap_score + field_score + content_score + curated_score
                + section_focus + alias_score
            )
            score = _compress(evidence)
            hits.append(SearchHit(
                chunk_id=row["chunk_id"],
                title=row["title"],
                source_file=row["source_file"],
                locator=row["locator"],
                section_title=row["section_title"],
                text=row["text"],
                category=row["category"],
                score=score,
            ))
        hits.sort(key=lambda hit: (
            -hit.score,
            not hit.source_file.startswith("knowledge/"),
            hit.title,
            hit.locator,
        ))
        return hits[:limit]
