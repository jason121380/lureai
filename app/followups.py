"""建議問題（追問）一定要是「問得下去」的問題。

模型自己寫的追問可能問到知識庫沒有的東西，點下去就會得到「沒有足夠資料」。
這裡做兩件事：
1. 驗證：每個候選追問都先跑一次檢索，撈不到夠格知識的直接丟掉。
2. 補位：從相鄰知識（同分類 → 同手冊 → 同主題）取出它們的問法，補到三個。

因為補位來源是知識庫本身，所以照著建議一路問下去不會斷。
"""
from __future__ import annotations

import json
import re

from .response_facts import stage_counts, user_texts
from functools import lru_cache
from pathlib import Path


QUESTION_BANK_PATH = Path(__file__).resolve().parent.parent / "config" / "question_bank.json"

# 候選池最前面這幾筆是「同分類」的知識，一定要留在原位——它們才是真的
# 接得上這一題的追問。再往後的長尾才輪流轉，避免長對話一直繞同幾塊。
# 判斷「還在同一個主題裡」時，這一題本身撈得到的一圈要放多寬。
# 太窄會把正當的追問也擋掉，太寬就跟沒擋一樣。
NEIGHBOURHOOD_LIMIT = 24

RELATED_HEAD = 6
# 頭部要照顧到答案用到的**每一塊**知識（`top_k` 是 4），不是只有第一塊。
MAX_SEED_CHUNKS = 4
# 「差一點也撈到」是相對於第一名說的：低於第一名九成的就只是字面沾到邊。
NEARBY_SCORE_RATIO = 0.90
# 分數再低也至少留兩塊來源當種子，否則常常只湊得出一個建議。
MIN_SEED_CHUNKS = 2

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


def welcome_questions(limit: int = 12, rng=None, fallback: tuple[str, ...] = ()) -> list[str]:
    """開場建議問題：每次都不一樣，而且橫跨不同主題。

    來源是問法索引裡人工寫的問句，所以隨機挑到哪一題都答得出來。
    """
    import random

    picker = rng or random
    curated = [question for question in fallback if str(question).strip()]
    if len(curated) >= limit:
        picker.shuffle(curated)
        return curated[:limit]
    grouped: dict[str, list[str]] = {}
    for locator, questions in seed_questions().items():
        prefix = str(locator).split("-", 1)[0]
        grouped.setdefault(prefix, []).extend(questions)
    if not grouped:
        return list(fallback)[:limit]

    for questions in grouped.values():
        picker.shuffle(questions)
    # 依主題輪流取，三個建議才不會全部落在同一本手冊。
    prefixes = sorted(grouped)
    picker.shuffle(prefixes)
    picked: list[str] = []
    index = 0
    while len(picked) < limit and any(len(grouped[prefix]) > index for prefix in prefixes):
        for prefix in prefixes:
            if len(grouped[prefix]) > index:
                question = grouped[prefix][index]
                picked.append(question if question.endswith(("？", "?")) else f"{question}？")
                if len(picked) >= limit:
                    break
        index += 1
    return picked


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

    def _neighbourhood(self, question: str, hits: list) -> dict[str, int]:
        """「這一輪在講的事」大概涵蓋哪些知識，以及各排第幾名。

        答案用到的那幾塊，加上這一題本身撈得到的一圈。用來判斷一個建議問題是不是
        還在同一個主題裡——**只問「答得出來嗎」是不夠的**：知識庫裡每一題都答得
        出來，所以那個條件形同虛設，問賣產品照樣會被建議「我想自己開店」。

        名次要留著：湊不到頭部、只能從長尾挑的時候，得先給最接近的那幾個。
        """
        near = {str(hit.chunk_id): index for index, hit in enumerate(hits or [])}
        if question:
            for index, hit in enumerate(
                self.retriever.retrieve(question, limit=NEIGHBOURHOOD_LIMIT)
            ):
                near.setdefault(str(hit.chunk_id), index)
        return near

    def _on_topic(self, candidate: str, near: dict[str, int]) -> bool:
        """這個建議問題撈到的知識，還在這一輪的主題裡嗎。"""
        if not near:
            return True
        found = self.retriever.retrieve(candidate, limit=2)
        return any(str(hit.chunk_id) in near for hit in found)

    def _related_head(self, hits: list) -> list[dict]:
        """答案用到的每一塊知識，各給幾筆**同分類**的鄰居，輪流排。

        只看第一塊會失準：「我不會賣產品要怎麼開口」同時用到產品銷售、話術與
        店販三塊，只看第一塊時同分類只剩一筆，第二個建議就掉到別的主題。
        同分類用完就少給一個建議，不要拿別的主題硬湊。
        """
        if not hits:
            return []
        seen = {hit.chunk_id for hit in hits}
        # 只讓「分數夠接近第一名」的來源去衍生建議。第 4 名常常只是勉強被撈進來
        # 陪襯（賣產品那題的第 4 名是 0.858 對 0.964），拿它的同分類鄰居當建議
        # 會整個換一個主題——實際踩到的是 ops-63「店販開發」被歸在「美髮技術」，
        # 於是建議變成「毛髮構造與三種鏈鍵」。至少留兩個，免得只剩一個建議。
        floor = hits[0].score * NEARBY_SCORE_RATIO
        seeds = [hit for hit in hits[:MAX_SEED_CHUNKS] if hit.score >= floor]
        if len(seeds) < MIN_SEED_CHUNKS:
            seeds = hits[:MIN_SEED_CHUNKS]
        primaries = [
            chunk for chunk in (self.store.get_chunk(hit.chunk_id) for hit in seeds) if chunk
        ]
        heads = []
        for chunk in primaries:
            category = str(chunk.get("category", ""))
            rows = self.store.related_chunks(
                category=category,
                domain=str(chunk.get("domain", "")),
                source_file=str(chunk.get("source_file", "")),
                exclude_ids=seen,
                limit=RELATED_HEAD,
            )
            # **只留同分類的**。`related_chunks` 是排序不是過濾，同分類用完之後
            # 會接上同一本手冊照 locator 排的其他知識——問賣產品就會冒出
            # 「毛髮構造與三種鏈鍵」。
            heads.append([row for row in rows if str(row.get("category", "")) == category])
        ordered: list[dict] = []
        taken: set[str] = set()
        for index in range(RELATED_HEAD):
            for rows in heads:
                if index >= len(rows):
                    continue
                chunk_id = str(rows[index].get("chunk_id", ""))
                if chunk_id and chunk_id not in taken:
                    taken.add(chunk_id)
                    ordered.append(rows[index])
        return ordered

    def _related_tail(self, hits: list, limit: int = 400) -> list[dict]:
        """整個語料當備胎，讓一路追問下去永遠找得到還沒問過的題目。"""
        if not hits:
            return []
        primary = self.store.get_chunk(hits[0].chunk_id) or {}
        return self.store.related_chunks(
            category=str(primary.get("category", "")),
            domain=str(primary.get("domain", "")),
            source_file=str(primary.get("source_file", "")),
            exclude_ids={hit.chunk_id for hit in hits},
            limit=limit,
        )

    def _nearby_by_question(self, question: str, hits: list, limit: int = 14) -> list[dict]:
        """這一題「差一點也撈到」的知識，就是他接下來最可能問的東西。

        門檻要看**跟第一名差多少**，不能用固定的 0.72：分數被壓縮在
        [0.5, 1.0) 裡，一題隨便都有十幾塊過 0.72，照收就變成
        「用生成式 AI 幫忙」「清潔督導與衛生檢查」這種完全不相干的建議。
        跟第一名差超過一成的就是只沾到字面，不要。
        """
        if not question:
            return []
        found = self.retriever.retrieve(question, limit=limit)
        if not found:
            return []
        floor = max(self.policy.minimum_score, found[0].score * NEARBY_SCORE_RATIO)
        used = {hit.chunk_id for hit in hits}
        return [
            {"chunk_id": hit.chunk_id, "locator": hit.locator, "section_title": hit.section_title}
            for hit in found
            if hit.chunk_id not in used and hit.score >= floor
        ]

    def plan(self, hits: list, asked: set[str] | None = None, limit: int = 3,
             candidates: list[str] | None = None, question: str = "", history=None,
             reason: str = "grounded") -> list[str]:
        if (reason in ("smalltalk", "emotion", "closing", "boundary", "hesitation", "self_intro")
                or self.policy.emotion_only(question) or self.policy.smalltalk(question)
                or self.policy.boundary_reply(question)):
            return []
        counts = stage_counts(question, history)
        context = " ".join(user_texts(question, history)[-3:])
        def compatible(candidate):
            if counts.get("messages") == 0 and re.search(r"有私訊|私訊.*(?:回|預約)|回.*私訊|客人.*過敏", candidate):
                return False
            if "開店" in candidate and not re.search(r"開店|創業|工作室", context):
                return False
            if re.search(r"廣告|曝光|點擊|私訊", question) and re.search(r"過敏|產品|毛髮|輔導要問|排班", candidate):
                return False
            return True
        blocked = {_normalize(item) for item in (asked or set())}
        picked: list[str] = []
        # 「答得出來」跟「跟這一輪有關」是兩件事，要分開檢查。知識庫裡幾乎每一題
        # 都答得出來，所以只驗前者等於沒驗——模型寫的三個追問可以全部離題而照樣
        # 通過（實測問「我不會賣產品要怎麼開口」，「自己開店」「毛髮構造」
        # 「廣告投多少」三題全被接受）。
        near = self._neighbourhood(question, hits)
        for candidate in candidates or []:
            cleaned = " ".join(str(candidate or "").split())[:60]
            key = _normalize(cleaned)
            if not key or key in blocked or not compatible(cleaned) or not self._answerable(cleaned):
                continue
            if not self._on_topic(cleaned, near):
                continue
            blocked.add(key)
            picked.append(cleaned)
            if len(picked) >= limit:
                return picked

        # The answer sources are the strongest topic evidence. Offer an unasked
        # curated question from each source before walking to category neighbours;
        # otherwise excluding used rows can replace a product answer with unrelated
        # rows that merely share a broad category.
        source_rows = [
            row for row in (self.store.get_chunk(hit.chunk_id) for hit in hits) if row
        ]
        picked.extend(self._collect(source_rows, blocked, limit - len(picked), compatible))
        if len(picked) >= limit:
            return picked[:limit]

        # 頭部是真的接得上這一題的知識：先看「這一題附近還有什麼」，
        # 再補上答案用到的每一塊知識的同分類鄰居。
        # 順序是量出來的：先給「這一題附近還有什麼」（檢索器說的，最貼題），
        # 不夠再補「答案用到的那幾塊知識的同分類鄰居」（分類說的）。
        # 兩者交錯排過，實測反而變差——同分類名單是照 locator 排的，
        # 每次都讓手冊的第一塊（例如 script-01）搶到第一個位置。
        nearby = self._nearby_by_question(question, hits)
        taken = {str(item["chunk_id"]) for item in nearby}
        head = nearby + [
            row for row in self._related_head(hits)
            if str(row.get("chunk_id", "")) not in taken
        ]
        picked.extend(self._collect(head, blocked, limit - len(picked), compatible))
        if picked:
            # 頭部湊得到就不要再往下拿。後面那條長尾只是「還沒問過的題目」，
            # 拿它湊第三個建議會冒出「毛髮構造」「我想自己開店」這種完全不
            # 相干的東西——寧可只給兩個對的，也不要三個裡有一個離題。
            return picked[:limit]

        # 頭部一個都湊不到才動用長尾，讓一路追問下去永遠有題目可問
        # （50 輪連續追問不能斷，見 `tests/test_followup_chain.py`）。
        # 每問一輪轉一格，長對話才不會在同幾塊知識之間繞圈。
        used = {str(item.get("chunk_id", "")) for item in head}
        tail = [
            row for row in self._related_tail(hits)
            if str(row.get("chunk_id", "")) not in used
        ]
        if tail:
            offset = len(blocked) % len(tail)
            tail = tail[offset:] + tail[:offset]
        # 長尾是「整份語料裡還沒問過的」，本來就可能離題。先把還在這一輪主題裡的
        # 排到前面——這個分類只有一兩塊知識、而且都被當成來源用掉時（訂金與爽約
        # 就只有 2 塊），只有這條路救得了它。旋轉留給剩下那些，鏈條才不會斷。
        if near:
            inside = sorted(
                (row for row in tail if str(row.get("chunk_id", "")) in near),
                key=lambda row: near[str(row["chunk_id"])],
            )
            taken_ids = {str(row["chunk_id"]) for row in inside}
            tail = inside + [
                row for row in tail if str(row.get("chunk_id", "")) not in taken_ids
            ]
        picked.extend(self._collect(tail, blocked, limit - len(picked), compatible))
        return picked[:limit]

    def _collect(self, rows: list[dict], blocked: set[str], limit: int, compatible=lambda _q: True) -> list[str]:
        """從候選知識挑出接得下去、而且還沒問過的問法。"""
        picked: list[str] = []
        if limit <= 0:
            return picked
        for row in rows:
            for question in questions_for(
                str(row.get("locator", "")), str(row.get("section_title", "")), limit=3
            ):
                key = _normalize(question)
                if not key or key in blocked or not compatible(question) or not self._answerable(question):
                    continue
                blocked.add(key)
                picked.append(" ".join(question.split())[:60])
                # 同一塊知識只取一個問法：一塊知識的問法都是同一件事的不同說法
                # （「不喜歡推銷」「推產品會不會很硬」「推銷讓我覺得很像騙錢」），
                # 三個建議全出自同一塊等於只給了一個選擇。
                break
            if len(picked) >= limit:
                break
        return picked
