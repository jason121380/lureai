import inspect
import re
from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

from .answer import AnswerEngine, log_model_failure, normalize_citation_marks, normalize_tone
from .followups import FollowupPlanner, welcome_questions
from .policy import PolicyEngine, speaker_name
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


CITATION_REF_PATTERN = re.compile(r"\[(\d{1,2})\]")

SENSITIVE_HISTORY_PATTERN = re.compile(
    r"(?:09\d{2}[- ]?\d{3}[- ]?\d{3}|(?:\+?886[- ]?)?9\d{8}|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"(?:身分證|信用卡|卡號|電話|手機|住址|地址)\s*[:：]?\s*\S+)",
    re.I,
)


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
        return question

    def _safe_history(self, history: list[dict] | None) -> list[dict]:
        return [
            item for item in self._normalize_history(history)
            if self.policy.precheck(item["content"]).action != "escalate"
            and not SENSITIVE_HISTORY_PATTERN.search(item["content"])
        ]

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
        # 被前面的客訴問題拉去撈送客流程）；撈不動時才補上前兩題當脈絡，
        # 讓「然後呢？」這種接話仍然有得查。
        hits = self.retriever.retrieve(question, limit=self.top_k * 2)
        if recent_history and (not hits or hits[0].score < self.policy.minimum_score):
            previous_questions = [
                item["content"] for item in recent_history if item["role"] == "user"
            ][-2:]
            padded = self.retriever.retrieve(
                "\n".join(previous_questions + [question]), limit=self.top_k * 2
            )
            if padded and (not hits or padded[0].score > hits[0].score):
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
        rewritten = CITATION_REF_PATTERN.sub(
            lambda match: f"[{renumber[int(match.group(1))]}]"
            if int(match.group(1)) in renumber else match.group(0),
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
    ) -> dict:
        """閒聊／情緒／欲言又止：不查知識庫、不掛來源，但照樣記帳與稽核。"""
        answer, mode, model_status, usage = self.answerer.smalltalk(
            question, history=recent_history, allow_model=allow_model, tone=tone, kind=kind,
            speaker=self._speaker(recent_history, question),
        ) if _accepts_speaker(self.answerer) else self.answerer.smalltalk(
            question, history=recent_history, allow_model=allow_model, tone=tone, kind=kind,
        )
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
            )
            self._audit(question, result, [], user_id=user_id)
            return result
        if escalation is not None:
            result = self._escalated_result(trace_id, conversation_id, escalation, hits)
            self._audit(question, result, hits, user_id=user_id)
            return result
        answer, mode, model_status, usage = self.answerer.answer(
            question,
            grounded_hits,
            history=recent_history,
            allow_model=allow_model,
            tone=tone,
            extra_instruction=extra_instruction + self._speaker_note(recent_history, question),
            include_followups=want_followups,
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
        if self.answerer.model_enabled and allow_model:
            partial = ""
            model_status = "used"
            try:
                for kind, payload in self.answerer.stream_answer(
                    question, grounded_hits, history=recent_history, tone=tone
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
            if model_status == "used" and candidate and (
                not needs_citation or re.search(r"\[\d+\]", candidate)
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
                    retried, retry_usage = retry(
                        question, grounded_hits, history=recent_history, tone=tone
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
            "followups": self.followups.plan(
                grounded_hits,
                asked=self._asked_questions(history, question),
                candidates=followups,
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
        question = str(message or "").strip()
        if not question:
            raise ValueError("問題不可為空")
        if len(question) > self.max_question_chars:
            raise ValueError(f"問題不可超過 {self.max_question_chars} 個字")
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

    def _normalize_history(self, history: list[dict] | None) -> list[dict]:
        if history is None:
            return []
        if not isinstance(history, list):
            raise ValueError("對話紀錄格式無效")
        if len(history) > 80:
            raise ValueError("對話紀錄格式無效")
        normalized = []
        # 全部驗過，但只有最後 8 則會進到模型脈絡與檢索。
        for item in history:
            if not isinstance(item, dict) or item.get("role") != "user":
                raise ValueError("對話紀錄格式無效")
            content = str(item.get("content", "")).strip()
            if not content or len(content) > self.max_question_chars:
                raise ValueError("對話紀錄格式無效")
            normalized.append({"role": item["role"], "content": content})
        return normalized[-8:]

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
        })
