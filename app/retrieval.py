from __future__ import annotations

import json
import math
import re
from collections import Counter
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


# Remove question glue before making CJK spans, so a fragment crossing into
# an interrogative (e.g. the preceding verb plus 多 in 多少) is not evidence.
_QUERY_GLUE = re.compile("|".join(re.escape(term) for term in
    sorted(GENERIC_QUERY_TOKENS | {"多少", "多久", "幾"}, key=len, reverse=True)))


def evidence_question(text: str) -> str:
    normalized = normalize_for_search(text.replace("〔已遮罩〕", " "))
    def replace_glue(match):
        value = match.group()
        # Cut the preceding action→interrogative span (升多少 must not become
        # evidence for unrelated 一次升多少), while retaining the directional
        # interrogative→object span when the object is explicit (多少錢→少錢).
        if (value in {"多少", "多久", "幾"} and match.end() < len(normalized)
                and "\u3400" <= normalized[match.end()] <= "\u9fff"):
            return " " * (len(value) - 1) + value[-1]
        return " " * len(value)
    return _QUERY_GLUE.sub(replace_glue, normalized)


def relevance_tokens(text: str) -> set[str]:
    return {token for token in search_tokens(text) if token not in GENERIC_QUERY_TOKENS}


def relevance_bigrams(text: str) -> set[str]:
    return {token for token in cjk_bigrams(text) if token not in GENERIC_QUERY_TOKENS}


@lru_cache(maxsize=2048)
def support_terms(text: str) -> frozenset[str]:
    """One lexical unit per CJK bigram or Latin word, without alias evidence."""
    return frozenset(relevance_bigrams(text) | {
        term for term in relevance_tokens(text) if term.isascii()
    })


@lru_cache(maxsize=512)
def alias_terms(text: str) -> frozenset[str]:
    """問法索引的比對詞：中英文都要算，因為問句常混英文（emoji、roas）。"""
    return frozenset(relevance_tokens(text))


@lru_cache(maxsize=512)
def alias_phrases(text: str) -> frozenset[str]:
    """問法索引裡一整句一整句的問法（存的時候一行一個）。"""
    return frozenset(
        normalize_for_search(line).replace(" ", "")
        for line in str(text or "").split("\n")
        if line.strip()
    )


def _phrase_key(text: str) -> str:
    """比對用的形式：去空白、去標點，「私訊 多久要回？」＝「私訊多久要回」。"""
    return normalize_for_search(text).replace(" ", "")


# Evidence is squashed into [0.5, 1.0) instead of being clipped, so strongly
# matching chunks stay ordered rather than all landing on 1.0 and falling back
# to an alphabetical tie-break. 0.22 of evidence still maps to the historical
# 0.72 policy threshold.
_COMPRESSION = 2.63

# 問法索引的加分上限與每命中一筆的分數。seed 是人工寫的「這句話問的是哪一塊
# 知識」，是這個檢索器手上最強的訊號，所以給得比字面 bigram 重。
# 掃描過 0.20/0.025 ~ 0.36/0.05：0.32/0.04 同時把 570 題的命中率從 86.5% 拉到
# 90.4%，也把 100 題開場題庫的最低分從 0.867 抬到 0.890（兩個方向都變好）。
# 再往上（0.36/0.05）命中率反而掉回 89.5%，代表別名開始蓋過內文。
ALIAS_SCORE_CAP = 0.32
ALIAS_SCORE_PER_MATCH = 0.04
# 整句命中手寫問法時額外加的分數。排序已經保證它排在最前面，這個加分只是
# 讓它穩穩站在 0.72 門檻之上；不需要開太大（開大只會讓覆蓋率自己給自己打分，
# 真正要顧的是沒寫進索引的自然問法）。
SEED_MATCH_BONUS = 0.10


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
        self._source_snapshot = (None, 0, Counter(), Counter())

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

    def _source_frequencies(self, candidate_rows):
        version, count, frequencies, template_frequencies = self._source_snapshot
        if hasattr(self.store, "retrieval_snapshot"):
            new_version, rows = self.store.retrieval_snapshot(version)
            if rows is None:
                return count, frequencies, template_frequencies
        else:
            # Lightweight store adapters can expose their complete candidate set.
            new_version, rows = None, candidate_rows
        frequencies = Counter()
        template_frequencies = Counter()
        for row in rows:
            source_terms = support_terms(f"{row['title']} {row['section_title']} {row['text']}")
            frequencies.update(source_terms)
            # One vote per document. Alias templates can only discount positive
            # specificity; they must never shrink unmatched query mass.
            template_frequencies.update(source_terms | support_terms(row.get('aliases', '') or ''))
        snapshot = (new_version, len(rows), frequencies, template_frequencies)
        self._source_snapshot = snapshot
        return snapshot[1:]

    def retrieve(self, question: str, limit: int = 6) -> list[SearchHit]:
        core_tokens = relevance_tokens(question)
        if not core_tokens:
            return []
        expanded = self.expand_question(question)
        # Synonyms only ever add matches: they widen what counts as a hit while
        # the original question still sets the denominator.
        query_tokens = relevance_tokens(expanded)
        rows = self.store.search_fts(fts_query(expanded), limit=max(limit * 8, 60))
        count, frequencies, template_frequencies = self._source_frequencies(rows)
        def weight(term, document_frequencies=frequencies):
            return (0.05 + 0.95 * math.log((count + 1) / (max(1, document_frequencies.get(term, 0)) + 1))
                    / math.log(count + 1)) if count else 1.0

        # Count original characters once. Unseen cross-word bigrams must not
        # penalize two otherwise recognized words; genuinely unseen subjects
        # still retain their full unmatched mass.
        normalized = evidence_question(question)
        units = [(match.group(), match.start(), match.end())
                 for match in re.finditer(r"[a-z0-9]+|(?=([\u3400-\u9fff]{2}))", normalized)]
        # Lookahead permits overlapping CJK bigrams.
        units = [(term or normalized[start:start + 2], start, end if term else start + 2)
                 for term, start, end in units]
        weights = [None if char.isalnum() else 0.0 for char in normalized]
        equivalent_units = []
        for group in self.synonym_groups:
            equivalents = set().union(*(support_terms(term) for term in group))
            known = [weight(term) for term in equivalents if term in frequencies]
            if not known:
                continue
            for term in group:
                for match in re.finditer(re.escape(term), normalized):
                    equivalent_units.append((group, match.start(), match.end(), max(known)))
        for term, start, end in units:
            if term in frequencies:
                for index in range(start, end):
                    weights[index] = max(weights[index] or 0.0, weight(term))
        for _group, start, end, term_weight in equivalent_units:
            for index in range(start, end):
                weights[index] = max(weights[index] or 0.0, term_weight)
        weights = [weight("") if value is None else value for value in weights]
        denominator = sum(weights) or 1.0

        def coverage_in(text):
            normalized_source = normalize_for_search(text)
            terms = support_terms(text)
            supported = set()
            for term, start, end in units:
                if term in terms:
                    supported.update(range(start, end))
            for group, start, end, _weight in equivalent_units:
                if any(term in normalized_source for term in group):
                    supported.update(range(start, end))
            return sum(weights[index] for index in supported) / denominator

        query_bigrams = relevance_bigrams(expanded)
        question_key = _phrase_key(question)
        expanded_support = support_terms(evidence_question(expanded))
        hits: list[SearchHit] = []
        seed_hits: list[bool] = []
        for row in rows:
            document_tokens = set(row["search_text"].split())
            matched = query_tokens & document_tokens
            if not matched:
                continue
            section_bigrams = cjk_bigrams(f"{row['title']} {row['section_title']}")
            field_matches = len(query_bigrams & section_bigrams)
            content_matches = len(query_bigrams & cjk_bigrams(row["text"]))
            # 問法索引：設計師實際會怎麼問這塊知識，對得上就加分。
            alias_text = row["aliases"] if "aliases" in row.keys() else ""
            source_terms = support_terms(f"{row['title']} {row['section_title']} {row['text']}")
            # Unsupported alias subjects cannot increase even the coverage-scaled bonus.
            alias_matches = len((query_tokens | query_bigrams) & source_terms
                                & alias_terms(str(alias_text or "")))
            # 只靠問法模板對上（例如任何題目都有的「的做法」）不算數，必須同時
            # 命中這塊知識的標題或內文，否則不相關的問題會被拉高分數。
            grounded_in_content = bool(field_matches or content_matches)
            alias_score = min(ALIAS_SCORE_CAP, alias_matches * ALIAS_SCORE_PER_MATCH) if grounded_in_content else 0.0
            # 整句正好是問法索引裡的一句＝人工標註「這句話問的就是這塊知識」。
            # 那比任何字面比對都可信，所以除了加分還要**排在前面**：光加分擋不住
            # 一塊字面上很像的知識（「要不要加 emoji」被 career-12 以 0.841 壓過
            # chat-07 的 0.825，而 chat-07 才是這句話的正主）。
            seed_match = bool(question_key) and question_key in alias_phrases(str(alias_text or ""))
            if seed_match:
                alias_score += SEED_MATCH_BONUS
            coverage = coverage_in(f"{row['title']} {row['section_title']} {row['text']}")
            field_coverage = coverage_in(f"{row['title']} {row['section_title']}")
            specificity = max((weight(term, template_frequencies)
                               for term in expanded_support & source_terms), default=0.0) / weight("")
            # Exact curated annotations and verbatim source subjects remain strong
            # evidence, including a short subject in a one-document corpus.
            exact_source = question_key and any(
                question_key == _phrase_key(str(row[field] or ""))
                for field in ("title", "section_title", "text")
            )
            if seed_match or exact_source:
                coverage = specificity = 1.0
            # Aliases and source provenance cannot independently authorize an
            # answer. Scores retain the existing shared policy scale, not a
            # probability interpretation.
            curated = str(row["source_file"]).startswith("knowledge/")
            evidence = (0.80 * coverage + 0.25 * field_coverage
                        + coverage * (alias_score + (0.06 if curated else 0.0)))
            score = _compress(evidence * specificity)
            seed_hits.append(seed_match)
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
        ordered = sorted(
            zip(hits, seed_hits),
            key=lambda pair: (
                not pair[1],
                -pair[0].score,
                not pair[0].source_file.startswith("knowledge/"),
                pair[0].title,
                pair[0].locator,
            ),
        )
        return [hit for hit, _seed in ordered[:limit]]
