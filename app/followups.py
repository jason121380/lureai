"""建議問題（追問）一定要是「問得下去」的問題。

模型自己寫的追問可能問到知識庫沒有的東西，點下去就會得到「沒有足夠資料」。
這裡做兩件事：
1. 驗證：每個候選追問都先跑一次檢索，撈不到夠格知識的直接丟掉。
2. 補位：從相鄰知識（同分類 → 同手冊 → 同主題）取出它們的問法，補到三個。

因為補位來源是知識庫本身，所以照著建議一路問下去不會斷。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


QUESTION_BANK_PATH = Path(__file__).resolve().parent.parent / "config" / "question_bank.json"

# 標題已經像問句時直接用，否則補一個口語的問法。
QUESTION_SUFFIXES = ("嗎", "呢", "什麼", "哪裡", "幾次", "多久", "怎麼辦", "怎麼做", "怎麼開", "怎麼算")


@lru_cache(maxsize=1)
def seed_questions(path: str | None = None) -> dict[str, tuple[str, ...]]:
    source = Path(path) if path else QUESTION_BANK_PATH
    if not source.is_file():
        return {}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sections = payload.get("sections") if isinstance(payload, dict) else payload
    if not isinstance(sections, dict):
        return {}
    return {
        str(locator): tuple(str(question).strip() for question in questions if str(question).strip())
        for locator, questions in sections.items()
        if isinstance(questions, list)
    }


def question_from_title(section_title: str) -> str:
    """把知識標題變成一句設計師會問的話。"""
    title = " ".join(str(section_title or "").split())
    if not title:
        return ""
    if title.endswith("？") or title.endswith("?"):
        return title
    if any(title.endswith(suffix) for suffix in QUESTION_SUFFIXES) or "怎麼" in title or "如何" in title:
        return f"{title}？"
    return f"{title}要怎麼做？"


def questions_for(locator: str, section_title: str, limit: int = 2) -> list[str]:
    picks = list(seed_questions().get(locator, ())[:limit])
    if not picks:
        candidate = question_from_title(section_title)
        if candidate:
            picks = [candidate]
    return [question if question.endswith(("？", "?")) else f"{question}？" for question in picks]


def _normalize(question: str) -> str:
    return "".join(str(question or "").split()).rstrip("？?。.")


class FollowupPlanner:
    """依目前答案用到的知識，挑出接得下去的追問。"""

    def __init__(self, store, retriever, policy):
        self.store = store
        self.retriever = retriever
        self.policy = policy

    def _answerable(self, question: str) -> bool:
        """點下去一定要有答案：政策不擋，而且撈得到夠格的知識。"""
        if self.policy.precheck(question).action == "escalate":
            return False
        hits = self.retriever.retrieve(question, limit=1)
        return bool(hits) and hits[0].score >= self.policy.minimum_score

    def _neighbours(self, hits: list, limit: int = 400) -> list[dict]:
        """同分類優先，其次同一本手冊，再其次同主題，最後才是其他知識。

        取整個語料當候選池，這樣一路追問下去永遠找得到還沒問過的題目。
        """
        if not hits:
            return []
        seen = {hit.chunk_id for hit in hits}
        primary = self.store.get_chunk(hits[0].chunk_id) or {}
        return self.store.related_chunks(
            category=str(primary.get("category", "")),
            domain=str(primary.get("domain", "")),
            source_file=str(primary.get("source_file", "")),
            exclude_ids=seen,
            limit=limit,
        )

    def plan(self, hits: list, asked: set[str] | None = None, limit: int = 3,
             candidates: list[str] | None = None) -> list[str]:
        blocked = {_normalize(question) for question in (asked or set())}
        picked: list[str] = []

        def add(question: str) -> bool:
            cleaned = " ".join(str(question or "").split())[:60]
            key = _normalize(cleaned)
            if not key or key in blocked:
                return False
            blocked.add(key)
            picked.append(cleaned)
            return len(picked) >= limit

        for question in candidates or []:
            if self._answerable(question) and add(question):
                return picked


        rows = self._neighbours(hits)
        # 每問一輪就把候選池轉一格，追問才會一直往新的主題走，而不是在同幾塊
        # 知識之間繞圈（前端只會帶最近幾則對話，光靠去重不夠）。
        if rows:
            offset = len(blocked) % len(rows)
            rows = rows[offset:] + rows[:offset]
        for row in rows:
            for question in questions_for(str(row.get("locator", "")), str(row.get("section_title", "")), limit=3):
                if _normalize(question) in blocked or not self._answerable(question):
                    continue
                if add(question):
                    return picked
        return picked
