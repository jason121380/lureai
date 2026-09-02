import inspect
import re
from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

from .answer import (
    AnswerEngine, CONTACT_PATTERN, log_model_failure, mask_contacts,
    normalize_citation_marks, normalize_tone,
)
from . import quality
from .followups import FollowupPlanner, welcome_questions
from .policy import COACHING_TERMS, PolicyEngine, speaker_name
from .retrieval import Retriever
from .storage import KnowledgeStore
from .usage import UsagePricing


FOLLOWUP_PATTERN = re.compile(r"^[▷›>]\s*(.+)$")


def _accepts_speaker(answerer) -> bool:
    """測試用的假 answerer 可能沒有 speaker 參數，不要因此炸掉。"""
    try:
        return "speaker" in inspect.signature(answerer.smalltalk).parameters
    except (TypeError, ValueError):
        return False


def split_followups(text: str) -> tuple[str, list[str]]:
    """Pull trailing '▷ question' lines (written per FOLLOWUP_INSTRUCTION) off an answer."""
    lines = str(text or "").rstrip().splitlines()
    followups: list[str] = []
    while lines:
        line = lines[-1].strip()
        matched = FOLLOWUP_PATTERN.match(line)
        if matched and matched.group(1).strip():
            followups.insert(0, matched.group(1).strip()[:60])
            lines.pop()
        elif not line:
            lines.pop()
        else:
            break
    return "\n".join(lines).rstrip(), followups[:3]


# 追問要不要帶上一題當脈絡，看的是「這一題自己撈得夠不夠準」，不是有沒有過門檻。
# 實測：100 題完整問題最低 0.867；而「我想寫得自然一點」「然後呢？」這種接話
# 只有 0.748~0.773——它們是靠一個字（「自然」）勉強過 0.72，主題完全不對。
# 0.80 這條線把兩群分得很開，兩邊都還留著近 0.07 的餘裕。
WEAK_MATCH_SCORE = 0.80

# AI 自己說過的話最多帶這麼長：夠模型知道上一則講了什麼，又不會讓
# 前端塞一大段東西進脈絡。
MAX_ASSISTANT_TURNS = 4
MAX_ASSISTANT_CONTEXT_CHARS = 600

# 「接話」不是完整的問題，它一定要靠前一題才知道在講什麼。只看分數擋不住：
# 「為什麼」0.845、「多少錢」0.898、「太長了」0.851 都高於 0.80，於是完全
# 不補脈絡，撈到的是「客人為什麼會在活動期消費」這種毫不相干的知識（體檢 B9）。
# 判斷方式是「這句話裡有沒有店裡的名詞」——沒有名詞就無法自己成立。
FOLLOW_UP_MAX_CHARS = 8
FOLLOW_UP_OPENERS = re.compile(
    r"^(?:那|然後|接下來|再|還有|除了|不是|為什麼|怎麼會|可以再|幫我改|換|給我另|第[二三四五六]|舉個|用我)"
)

CITATION_REF_PATTERN = re.compile(r"\[(\d{1,2})\]")

# 電話與 Email 一律不送進模型。歷史訊息與稽核早就遮罩了，只有「現在這一則」
# 是原文送進檢索與模型的——設計師貼一句「客人 0912-345-678 一直沒回」，那組
# 號碼就離開了這台機器。這裡只挑不會誤傷的兩種：下面
# `SENSITIVE_HISTORY_PATTERN` 那條含關鍵字的規則對稽核夠好，但拿來改寫問題會
# 把「客人一直看手機」也一起遮掉。
# 正本在 `app/answer.py`（模型邊界那一層），這裡 re-export 是為了讓檢索與
# 稽核拿到的也是同一份乾淨的問題——遮在模型那層可以擋住外送，但擋不住
# 「原始號碼被拿去查知識庫、或寫進長期保存的稽核」。
_CONTACT_SOURCE = CONTACT_PATTERN.pattern.removeprefix("(?:").removesuffix(")")

# 稽核那條多認一組關鍵字寫法（「電話：0912…」）。它只用在會長期留著的紀錄上，
# 寧可多遮一點；**不要拿它去改寫問題**，`電話\s*\S+` 會把「客人一直看手機」
# 這種正常句子也遮掉。
SENSITIVE_HISTORY_PATTERN = re.compile(
    f"(?:{_CONTACT_SOURCE}|"
    r"(?:身分證|信用卡|卡號|電話|手機|住址|地址)\s*[:：]?\s*\S+)",
    re.I,
)


def _accepts_kwarg(function, name: str) -> bool:
    """測試用的假 answerer 常常少幾個參數，多帶會直接 TypeError。"""
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def is_follow_up(question: str) -> bool:
    """這句話是不是「接話」——沒有店裡的名詞，就一定要靠前一題才看得懂。

    「換一個」「為什麼」「再短一點」「用我的口氣」都算；
    「燙髮後怎麼整理」「客人說太貴」有名詞，自己就撈得準，不算。
    """
    text = "".join(str(question or "").split()).rstrip("？?。.！!~～")
    if not text:
        return False
    if any(term in text for term in COACHING_TERMS):
        return False
    return len(text) <= FOLLOW_UP_MAX_CHARS or bool(FOLLOW_UP_OPENERS.match(text))


class CustomerService:
    def __init__(
        self,
        store: KnowledgeStore,
        retriever: Retriever,
        policy: PolicyEngine,
        answerer: AnswerEngine,
        top_k: int = 6,
        max_question_chars: int = 1200,
        pricing: UsagePricing | None = None,
    ):
        self.store = store
        self.retriever = retriever
        self.policy = policy
        self.answerer = answerer
        self.top_k = top_k
        self.max_question_chars = max_question_chars
        self.pricing = pricing or UsagePricing.from_env()
        self.followups = FollowupPlanner(store, retriever, policy)

    def _validated_question(self, message: str) -> str:
        question = str(message or "").strip()
        if not question:
            raise ValueError("問題不可為空")
        if len(question) > self.max_question_chars:
            raise ValueError(f"問題不可超過 {self.max_question_chars} 個字")
        # 遮罩要在這裡做，檢索、模型與稽核拿到的才是同一份乾淨的問題。
        return mask_contacts(question)

    def _safe_history(self, history: list[dict] | None) -> list[dict]:
        """送進模型的脈絡：使用者的問題與 AI 自己上一則回覆。

        敏感題與 PII 一律不帶；敏感詞的判斷只套在使用者那幾則——AI 的回答
        本來就會提到醫療、退費這些字，拿同一套去篩會把正常回覆整段刪掉。
        """
        safe: list[dict] = []
        for item in self._normalize_history(history):
            if SENSITIVE_HISTORY_PATTERN.search(item["content"]):
                continue
            if item["role"] == "user" and self.policy.precheck(item["content"]).action == "escalate":
                continue
            safe.append(item)
        return safe

    @staticmethod
    def _diversify(hits: list, per_source: int = 2) -> list:
        """Cap chunks per source document so citations aren't one file repeated."""
        counts: dict[str, int] = {}
        diverse = []
        for hit in hits:
            source = str(hit.source_file)
            if counts.get(source, 0) >= per_source:
                continue
            counts[source] = counts.get(source, 0) + 1
            diverse.append(hit)
        return diverse

    def _route(self, question: str, recent_history: list[dict]) -> tuple[list, list, object | None]:
        """Return (all_hits, grounded_hits, escalation_decision_or_None)."""
        # 邊界題（離題、不當請求、問身分、情緒挑釁）不進檢索：硬走 RAG 只會撈到
        # 不相干知識再崩潰。直接給固定回應。
        boundary = self.policy.boundary_reply(question)
        if boundary is not None:
            return [], [], boundary
        # 打招呼、道謝、應聲這種話沒有東西可查，硬走 RAG 只會回「我手邊的資料不夠」，
        # 一句「哈囉」被當成問題，講話就很硬。交給模型自然接一句，等他問到真正的
        # 問題再從知識庫拿。
        smalltalk = self.policy.smalltalk(question) or self.policy.emotion_only(question)
        if smalltalk is not None:
            return [], [], smalltalk
        precheck = self.policy.precheck(question)
        if precheck.action == "escalate":
            return [], [], precheck
        # 先用問題本身檢索。夠好就用它，避免前一題把主題帶偏（「客人沒回要追嗎」
        # 被前面的客訴問題拉去撈送客流程）；只是「勉強及格」時才補上前兩題當脈絡，
        # 讓「然後呢？」「我想寫得自然一點」這種接話仍然查得到正確主題。
        hits = self.retriever.retrieve(question, limit=self.top_k * 2)
        weak = max(self.policy.minimum_score, WEAK_MATCH_SCORE)
        # 沒有店裡名詞的接話（「為什麼」「然後呢」）本來就看不懂，補脈絡的那份
        # **同分就要贏**。兩個不同 query 的分數不是同一把尺，硬要求嚴格較高的話，
        # 打平時會留下無脈絡的那份——實測「我想漲價」→「為什麼」兩邊都是 0.8566，
        # 於是選到「客人為什麼會在活動期消費」，完全不是他在問的事。
        # 分數只是勉強及格的那條路照舊要嚴格較高：那裡的問題本身是看得懂的，
        # 同分就換掉會讓自足的問題被前一題帶走。
        dependent = is_follow_up(question)
        if recent_history and (not hits or hits[0].score < weak or dependent):
            previous_questions = [
                item["content"] for item in recent_history if item["role"] == "user"
            ][-2:]
            padded = self.retriever.retrieve(
                "\n".join(previous_questions + [question]), limit=self.top_k * 2
            )
            if padded and (
                not hits
                or (padded[0].score >= hits[0].score if dependent
                    else padded[0].score > hits[0].score)
            ):
                hits = padded
        decision = self.policy.evaluate(hits)
        if decision.action == "escalate":
            return hits, [], decision
        grounded_hits = [hit for hit in hits if hit.score >= self.policy.minimum_score]
        primary_hits = self._diversify(
            [hit for hit in grounded_hits if hit.category != "歷史輔導案例"]
        )
        historical_hits = [hit for hit in grounded_hits if hit.category == "歷史輔導案例"]
        if primary_hits:
            grounded_hits = (primary_hits + historical_hits[:1])[:self.top_k]
        else:
            grounded_hits = historical_hits[:2]
        return hits, grounded_hits, None

    @staticmethod
    def _citations(grounded_hits: list, mode: str, model_status: str) -> list[dict]:
        """生成失敗降級時不掛來源：那則回答並沒有用到這些知識。"""
        if mode != "llm" and model_status not in ("not_configured", "budget_exhausted"):
            return []
        return [hit.citation() for hit in grounded_hits]

    def _fit_citations(
        self, answer: str, grounded_hits: list, mode: str, model_status: str, tone: str
    ) -> tuple[str, list[dict]]:
        """只列答案真的引用到的來源，並把編號重編成 1..n。

        檢索一次撈三塊，但窄問題常常只有第一塊能用——模型每點都寫 [1] 是對的，
        錯的是我們仍把沒被引用的兩塊掛成「知識來源 2、3」，看起來像編號壞掉。
        只在會把 [n] 顯示出來的語氣做（客服／LINE 的編號在出口就被剝掉，
        照樣裁切會讓那兩種模式一個來源都不剩）。
        """
        citations = self._citations(grounded_hits, mode, model_status)
        shows_numbers = getattr(self.answerer, "requires_citations", lambda _t: True)(tone)
        if not citations or not shows_numbers:
            return answer, citations
        used = sorted({
            int(number) for number in CITATION_REF_PATTERN.findall(answer or "")
            if 1 <= int(number) <= len(citations)
        })
        # 一個都沒引用時不裁：那是引用守門的問題，把來源全砍掉只會更難查。
        if not used or len(used) == len(citations):
            return answer, citations
        renumber = {old: new for new, old in enumerate(used, start=1)}
        # 指不到任何來源的編號直接從畫面上拿掉。留著的話會出現一個點不開的
        # 「[99]」，而下面列出的來源根本沒有第 99 個。
        rewritten = CITATION_REF_PATTERN.sub(
            lambda match: f"[{renumber[int(match.group(1))]}]"
            if int(match.group(1)) in renumber else "",
            answer,
        )
        return rewritten, [citations[old - 1] for old in used]

    @staticmethod
    def _asked_questions(history: list[dict] | None, question: str) -> set[str]:
        """Everything already asked in this conversation, for follow-up dedup."""
        asked = {
            str(item.get("content", "")).strip()
            for item in (history or [])
            if isinstance(item, dict) and item.get("role") == "user"
        }
        asked.discard("")
        asked.add(question)
        return asked

    def _nearest_questions(self, hits: list, limit: int = 3) -> list[str]:
        """Turn the closest chunks into questions the designer can actually ask."""
        return self.followups.plan(hits, limit=limit)

    def _escalated_result(
        self,
        trace_id: str,
        conversation_id: str | None,
        decision,
        hits: list | None = None,
    ) -> dict:
        # 邊界題（離題／不當請求／問身分／被罵）是正常回答，不是轉人工。
        direct = getattr(decision, "action", "") == "direct"
        # 答不出來時的建議題目要是安全的：拿檢索不到的那批 hits 去衍生，
        # 會冒出「毛髮構造」這種完全不相干的題目。改用驗證過的開場題庫。
        followups = welcome_questions(limit=3)
        return {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered" if direct else "escalated",
            "reason": decision.reason,
            "answer": decision.message,
            "citations": [],
            "followups": followups,
            "answer_mode": "boundary" if direct else "policy",
            "model_status": "boundary" if direct else "policy",
        }

    def _speaker_note(self, recent_history: list[dict], question: str) -> str:
        note = getattr(self.answerer, "speaker_note", None)
        return note(self._speaker(recent_history, question)) if note else ""

    @staticmethod
    def _speaker(recent_history: list[dict], question: str) -> str:
        """他說過名字就記著，之後直接叫名字（記得名字卻不用，跟沒記一樣）。"""
        said = [item["content"] for item in recent_history if item.get("role") == "user"]
        return speaker_name(said + [question])

    def _smalltalk_result(
        self,
        trace_id: str,
        conversation_id: str | None,
        question: str,
        recent_history: list[dict],
        allow_model: bool = True,
        tone: str = "expert",
        kind: str = "smalltalk",
        deadline: float | None = None,
    ) -> dict:
        """閒聊／情緒／欲言又止：不查知識庫、不掛來源，但照樣記帳與稽核。"""
        kwargs = {
            "history": recent_history, "allow_model": allow_model,
            "tone": tone, "kind": kind,
        }
        if _accepts_speaker(self.answerer):
            kwargs["speaker"] = self._speaker(recent_history, question)
        # 閒聊也吃同一份時間預算：群組裡一句「哈囉」拖到 reply token 過期最傷。
        if deadline is not None and _accepts_kwarg(self.answerer.smalltalk, "deadline"):
            kwargs["deadline"] = deadline
        answer, mode, model_status, usage = self.answerer.smalltalk(question, **kwargs)
        return {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered",
            "reason": kind,
            "answer": answer,
            "citations": [],
            "answer_mode": mode,
            "model_status": model_status,
            "usage": usage,
            # 打招呼時給幾個開場題目讓他知道可以問什麼；情緒與欲言又止不要塞，
            # 那等於在他抒發的時候又派任務給他。
            "followups": welcome_questions(limit=3) if kind == "smalltalk" else [],
            "tone": tone,
        }

    def _enforce_quality(
        self, question, answer, grounded_hits, recent_history, tone, extra_instruction,
        deadline=None,
    ) -> tuple[str, dict, int]:
        """生成完之後的品質檢查：命中就帶著具體理由重打一次。

        串流路徑原本完全沒有這一段，所以網頁使用者從來沒有享受到守門
        （體檢 B1）。重打是非串流的，最終 result 會覆蓋前端顯示的串流文字。
        """
        empty = {
            "input_tokens": 0, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 0,
        }
        found = quality.problems(question, answer, tone=tone)
        retry = getattr(self.answerer, "retry_for_quality", None)
        if not found or not callable(retry):
            return answer, empty, 0
        log_model_failure("quality", detail=f"{len(found)} issue(s); retrying | {found[0][:60]}")
        try:
            retry_kwargs = {
                "history": recent_history, "tone": tone,
                "extra_instruction": extra_instruction,
            }
            if deadline is not None and _accepts_kwarg(retry, "deadline"):
                retry_kwargs["deadline"] = deadline
            improved, usage = retry(question, grounded_hits, found, **retry_kwargs)
        except TypeError:
            return answer, empty, 0
        usage = usage or empty
        # 重打還是不合格就送原本那則：它至少是通順的話，比降級訊息好。
        return (improved or answer), usage, 1

    def _generate(
        self, question, grounded_hits, recent_history, allow_model, tone,
        extra_instruction, want_followups, context_note, deadline=None,
    ):
        kwargs = {
            "history": recent_history,
            "allow_model": allow_model,
            "tone": tone,
            "extra_instruction": extra_instruction + self._speaker_note(recent_history, question),
            "include_followups": want_followups,
        }
        if context_note and _accepts_kwarg(self.answerer.answer, "context_note"):
            kwargs["context_note"] = context_note
        # 共同截止時間：測試替身不一定收，收的才傳。
        if deadline is not None and _accepts_kwarg(self.answerer.answer, "deadline"):
            kwargs["deadline"] = deadline
        return self.answerer.answer(question, grounded_hits, **kwargs)

    def _retry_count(self) -> int:
        return int(getattr(self.answerer, "last_retries", 0) or 0)

    def chat(
        self,
        message: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
        user_id: int | None = None,
        allow_model: bool = True,
        tone: str | None = None,
        extra_instruction: str = "",
        want_followups: bool = True,
        context_note: str = "",
        deadline: float | None = None,
    ) -> dict:
        question = self._validated_question(message)
        tone = normalize_tone(tone)
        recent_history = self._safe_history(history)
        trace_id = str(uuid4())
        hits, grounded_hits, escalation = self._route(question, recent_history)
        if escalation is not None and getattr(escalation, "action", "") == "smalltalk":
            result = self._smalltalk_result(
                trace_id, conversation_id, question, recent_history,
                allow_model=allow_model, tone=tone, kind=escalation.reason,
                deadline=deadline,
            )
            self._audit(question, result, [], user_id=user_id)
            return result
        if escalation is not None:
            result = self._escalated_result(trace_id, conversation_id, escalation, hits)
            self._audit(question, result, hits, user_id=user_id)
            return result
        answer, mode, model_status, usage = self._generate(
            question,
            grounded_hits,
            recent_history,
            allow_model=allow_model,
            tone=tone,
            extra_instruction=extra_instruction,
            want_followups=want_followups,
            context_note=context_note,
            deadline=deadline,
        )
        followups: list[str] = []
        if mode == "llm":
            # 沒要追問時模型不會產生 ▷ 行，這裡照樣過一次以防萬一。
            answer, followups = split_followups(answer)
        if want_followups:
            # 建議問題一律要能被回答，點下去才不會撞到「沒有足夠資料」。
            followups = self.followups.plan(
                grounded_hits,
                asked=self._asked_questions(history, question),
                candidates=followups,
                question=question,
            )
        else:
            followups = []
        answer, citations = self._fit_citations(answer, grounded_hits, mode, model_status, tone)
        result = {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered",
            "reason": "grounded",
            "answer": answer,
            "citations": citations,
            "answer_mode": mode,
            "model_status": model_status,
            "usage": usage,
            "followups": followups,
            "tone": tone,
            "retries": self._retry_count(),
        }
        self._audit(question, result, hits, user_id=user_id)
        return result

    def chat_stream(
        self,
        message: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
        user_id: int | None = None,
        allow_model: bool = True,
        tone: str | None = None,
        extra_instruction: str = "",
        context_note: str = "",
        deadline: float | None = None,
    ) -> Iterator[dict]:
        """Yield {"type":"delta"} events followed by one authoritative {"type":"result"}."""
        question = self._validated_question(message)
        tone = normalize_tone(tone)
        # 先吐一個開場事件：伺服器才能立刻送出 header 與第一批位元組。
        # 檢索與模型生成可能要好幾秒，中間完全沒有位元組會被閘道當成無回應（503）。
        yield {"type": "start"}
        recent_history = self._safe_history(history)
        trace_id = str(uuid4())
        hits, grounded_hits, escalation = self._route(question, recent_history)
        if escalation is not None and getattr(escalation, "action", "") == "smalltalk":
            result = self._smalltalk_result(
                trace_id, conversation_id, question, recent_history,
                allow_model=allow_model, tone=tone, kind=escalation.reason,
                deadline=deadline,
            )
            self._audit(question, result, [], user_id=user_id)
            yield {"type": "result", **result}
            return
        if escalation is not None:
            result = self._escalated_result(trace_id, conversation_id, escalation, hits)
            self._audit(question, result, hits, user_id=user_id)
            yield {"type": "result", **result}
            return
        empty_usage = {
            "input_tokens": 0, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 0,
        }
        answer = ""
        mode = "extractive"
        model_status = "not_configured"
        usage = empty_usage
        followups: list[str] = []
        retries = 0
        if self.answerer.model_enabled and allow_model:
            partial = ""
            model_status = "used"
            stream_kwargs = {"history": recent_history, "tone": tone}
            note = extra_instruction + self._speaker_note(recent_history, question)
            if note and _accepts_kwarg(self.answerer.stream_answer, "extra_instruction"):
                stream_kwargs["extra_instruction"] = note
            if context_note and _accepts_kwarg(self.answerer.stream_answer, "context_note"):
                stream_kwargs["context_note"] = context_note
            if deadline is not None and _accepts_kwarg(self.answerer.stream_answer, "deadline"):
                stream_kwargs["deadline"] = deadline
            try:
                for kind, payload in self.answerer.stream_answer(
                    question, grounded_hits, **stream_kwargs
                ):
                    if kind == "delta":
                        partial += payload
                        yield {"type": "delta", "text": payload}
                    elif kind == "usage":
                        usage = payload
            except Exception as exc:  # noqa: BLE001 - 任何失敗都要降級，但要留下原因
                log_model_failure("stream", exc, f"model={self.answerer.model_name}")
                model_status = "stream_failed"
            candidate, followups = split_followups(normalize_citation_marks(partial.strip()))
            # 客服模式不用引用守門（編號本來就不顯示），避免好答案被丟掉。
            needs_citation = getattr(self.answerer, "requires_citations", lambda _t: True)(tone)
            valid_citation = getattr(
                self.answerer, "has_valid_citation",
                lambda text, count: bool(re.search(r"\[\d+\]", text or "")),
            )
            if model_status == "used" and candidate and (
                not needs_citation or valid_citation(candidate, len(grounded_hits))
            ):
                answer = candidate
                mode = "llm"
            else:
                if model_status == "used":
                    # 模型有回但沒附引用：加上警語重打一次再放棄。
                    log_model_failure(
                        "stream",
                        detail=f"missing_citations chars={len(candidate or '')} model={self.answerer.model_name}; retrying",
                    )
                    retry = getattr(self.answerer, "retry_with_citations", None)
                    citation_kwargs = {"history": recent_history, "tone": tone}
                    if deadline is not None and retry and _accepts_kwarg(retry, "deadline"):
                        citation_kwargs["deadline"] = deadline
                    retried, retry_usage = retry(
                        question, grounded_hits, **citation_kwargs
                    ) if retry else ("", empty_usage)
                    usage = {key: usage.get(key, 0) + retry_usage.get(key, 0) for key in empty_usage}
                    if retried:
                        answer, followups = split_followups(retried)
                        mode = "llm"
                        model_status = "used"
                    else:
                        model_status = "missing_citations"
                if mode != "llm":
                    answer = self.answerer._extractive_answer(grounded_hits, model_failed=True)
                    followups = []
            if mode == "llm":
                # 品質守門原本只掛在非串流那條路，而網頁聊天全部走串流——
                # 「我陪你一起拆」這種空話在網頁上是原樣送出的（體檢 B1）。
                # 這裡放在兩條分支之外，是為了連「引用重試」補回來的那則也查：
                # 舊版重打補回 `[n]` 之後就直接送出，內容空不空完全沒查。
                before_quality = answer
                answer, extra_usage, retries = self._enforce_quality(
                    question, answer, grounded_hits, recent_history, tone, note, deadline,
                )
                usage = {key: usage.get(key, 0) + extra_usage.get(key, 0) for key in empty_usage}
                if answer != before_quality:
                    # 只有真的重打過才重新拆一次 ▷ 行。無條件再拆一次會把上面
                    # 已經拆出來的追問清成空的（重打前的 answer 早就沒有 ▷ 了），
                    # 模型寫的追問就全部作廢，只能退回相鄰知識——畫面上就是
                    # 問賣產品卻建議「我想自己開店」。
                    answer, requeried = split_followups(answer)
                    followups = requeried or followups
        else:
            answer, mode, model_status, usage = self.answerer.answer(
                question, grounded_hits, history=recent_history, allow_model=allow_model, tone=tone
            )
        answer, citations = self._fit_citations(answer, grounded_hits, mode, model_status, tone)
        result = {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered",
            "reason": "grounded",
            "answer": answer,
            "citations": citations,
            "answer_mode": mode,
            "model_status": model_status,
            "usage": usage,
            "tone": tone,
            "retries": retries,
            "followups": self.followups.plan(
                grounded_hits,
                asked=self._asked_questions(history, question),
                candidates=followups,
                question=question,
            ),
        }
        self._audit(question, result, hits, user_id=user_id)
        yield {"type": "result", **result}

    def summarize_title(
        self,
        message: str,
        answer: str,
        conversation_id: str | None = None,
        user_id: int | None = None,
        allow_model: bool = True,
    ) -> dict:
        # 走跟聊天同一套驗證與遮罩。這裡原本自己抄了一份長度檢查卻沒有遮罩，
        # 於是「客人 0912-345-678 一直沒回」在聊天被遮掉，標題那條路卻原樣送出。
        question = self._validated_question(message)
        title, model_status, usage = self.answerer.generate_title(
            question, str(answer or ""), allow_model=allow_model
        )
        result = {
            "trace_id": str(uuid4()),
            "conversation_id": conversation_id,
            "status": "title",
            "reason": model_status,
            "answer": title,
            "usage": usage,
        }
        self._audit(question, result, [], user_id=user_id)
        return {"title": title, "model_status": model_status}

    def record_usage(self, reason: str, usage: dict, user_id: int | None = None) -> None:
        """把「不是聊天」的模型呼叫也記進帳本（目前是後台的文件分析）。

        走跟聊天同一張 audits 表，月預算與後台總覽才看得到它。
        """
        self._audit(
            f"[{reason}]",
            {
                "trace_id": str(uuid4()),
                "conversation_id": None,
                "status": "usage",
                "reason": reason,
                "usage": usage,
                "tone": "",
                "retries": 0,
            },
            [],
            user_id=user_id,
        )

    def _normalize_history(self, history: list[dict] | None) -> list[dict]:
        """驗證前端／lurebot 送上來的對話脈絡。

        **AI 自己說過的話也要帶**：不帶的話模型每一輪都是失憶的，
        「然後呢」「再短一點」「你說錯了吧」全部接不上，閒聊指令裡那句
        「上一則問過就不要再問一次」更是做不到的要求（體檢 R1）。
        assistant 那幾則以 assistant 角色送出（不是指令），內容另外夾長度，
        而且不參與檢索、不進稽核——它本來就是這個使用者自己的對話。
        """
        if history is None:
            return []
        if not isinstance(history, list):
            raise ValueError("對話紀錄格式無效")
        if len(history) > 80:
            raise ValueError("對話紀錄格式無效")
        normalized = []
        # 全部驗過，但只有最後 8 則會進到模型脈絡與檢索。
        for item in history:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                raise ValueError("對話紀錄格式無效")
            content = str(item.get("content", "")).strip()
            if not content or len(content) > self.max_question_chars:
                raise ValueError("對話紀錄格式無效")
            if item["role"] == "assistant":
                content = content[:MAX_ASSISTANT_CONTEXT_CHARS]
            normalized.append({"role": item["role"], "content": content})
        recent = normalized[-8:]
        # assistant 那幾則是 client 送上來的，內容沒有辦法驗證。留著是為了接得上
        # 「然後呢」「再短一點」，但不可以讓它把整個脈絡佔滿——八則全是「AI 說過
        # 燙髮一律打五折」時，模型看到的就只剩那個假前提。真正的答案一律來自
        # 當下重新檢索的知識，這裡只是把可被塞入的份額壓到一半。
        while sum(1 for item in recent if item["role"] == "assistant") > MAX_ASSISTANT_TURNS:
            for index, item in enumerate(recent):
                if item["role"] == "assistant":
                    del recent[index]
                    break
        return recent

    def _audit(self, question: str, result: dict, hits: list, user_id: int | None = None) -> None:
        usage = result.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        cached_input_tokens = int(usage.get("cached_input_tokens", 0))
        cache_write_input_tokens = int(usage.get("cache_write_input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        self.store.add_audit({
            "trace_id": result["trace_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "conversation_id": result.get("conversation_id"),
            # Audit logs are long-lived; strip phone/email/ID-style PII first.
            "question": SENSITIVE_HISTORY_PATTERN.sub("〔已遮罩〕", question),
            "status": result["status"],
            "reason": result["reason"],
            "top_score": hits[0].score if hits else None,
            "chunk_ids": [hit.chunk_id for hit in hits],
            "user_id": user_id,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_input_tokens": cache_write_input_tokens,
            "output_tokens": output_tokens,
            "cost_twd": self.pricing.cost_twd(
                input_tokens,
                output_tokens,
                cached_input_tokens,
                cache_write_input_tokens,
            ),
            "model": self.answerer.model_name,
            "tone": result.get("tone", ""),
            "retries": int(result.get("retries", 0)),
        })
