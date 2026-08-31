from datetime import datetime, timezone
import re
from typing import Iterator
from uuid import uuid4

from .answer import AnswerEngine
from .policy import PolicyEngine
from .retrieval import Retriever
from .storage import KnowledgeStore
from .usage import UsagePricing


FOLLOWUP_PATTERN = re.compile(r"^[▷›>]\s*(.+)$")


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

    def _route(self, question: str, recent_history: list[dict]) -> tuple[list, list, object | None]:
        """Return (all_hits, grounded_hits, escalation_decision_or_None)."""
        precheck = self.policy.precheck(question)
        if precheck.action == "escalate":
            return [], [], precheck
        previous_questions = [
            item["content"] for item in recent_history if item["role"] == "user"
        ][-2:]
        retrieval_query = "\n".join(previous_questions + [question])
        hits = self.retriever.retrieve(retrieval_query, limit=self.top_k)
        decision = self.policy.evaluate(hits)
        if decision.action == "escalate":
            return hits, [], decision
        grounded_hits = [hit for hit in hits if hit.score >= self.policy.minimum_score]
        primary_hits = [hit for hit in grounded_hits if hit.category != "歷史輔導案例"]
        historical_hits = [hit for hit in grounded_hits if hit.category == "歷史輔導案例"]
        if primary_hits:
            grounded_hits = (primary_hits + historical_hits[:1])[:self.top_k]
        else:
            grounded_hits = historical_hits[:2]
        return hits, grounded_hits, None

    def _escalated_result(self, trace_id: str, conversation_id: str | None, decision) -> dict:
        return {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "escalated",
            "reason": decision.reason,
            "answer": decision.message,
            "citations": [],
            "answer_mode": "policy",
        }

    def chat(
        self,
        message: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
        user_id: int | None = None,
        allow_model: bool = True,
    ) -> dict:
        question = self._validated_question(message)
        recent_history = self._safe_history(history)
        trace_id = str(uuid4())
        hits, grounded_hits, escalation = self._route(question, recent_history)
        if escalation is not None:
            result = self._escalated_result(trace_id, conversation_id, escalation)
            self._audit(question, result, hits, user_id=user_id)
            return result
        answer, mode, model_status, usage = self.answerer.answer(
            question,
            grounded_hits,
            history=recent_history,
            allow_model=allow_model,
        )
        followups: list[str] = []
        if mode == "llm":
            answer, followups = split_followups(answer)
        result = {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered",
            "reason": "grounded",
            "answer": answer,
            "citations": [hit.citation() for hit in grounded_hits],
            "answer_mode": mode,
            "model_status": model_status,
            "usage": usage,
            "followups": followups,
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
    ) -> Iterator[dict]:
        """Yield {"type":"delta"} events followed by one authoritative {"type":"result"}."""
        question = self._validated_question(message)
        recent_history = self._safe_history(history)
        trace_id = str(uuid4())
        hits, grounded_hits, escalation = self._route(question, recent_history)
        if escalation is not None:
            result = self._escalated_result(trace_id, conversation_id, escalation)
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
                    question, grounded_hits, history=recent_history
                ):
                    if kind == "delta":
                        partial += payload
                        yield {"type": "delta", "text": payload}
                    elif kind == "usage":
                        usage = payload
            except Exception:
                model_status = "stream_failed"
            candidate, followups = split_followups(partial.strip())
            if model_status == "used" and candidate and re.search(r"\[\d+\]", candidate):
                answer = candidate
                mode = "llm"
            else:
                if model_status == "used":
                    model_status = "missing_citations"
                answer = self.answerer._extractive_answer(grounded_hits, model_failed=True)
                followups = []
        else:
            answer, mode, model_status, usage = self.answerer.answer(
                question, grounded_hits, history=recent_history, allow_model=allow_model
            )
        result = {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered",
            "reason": "grounded",
            "answer": answer,
            "citations": [hit.citation() for hit in grounded_hits],
            "answer_mode": mode,
            "model_status": model_status,
            "usage": usage,
            "followups": followups,
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
        normalized = []
        for item in history[-8:]:
            if not isinstance(item, dict) or item.get("role") != "user":
                raise ValueError("對話紀錄格式無效")
            content = str(item.get("content", "")).strip()
            if not content or len(content) > self.max_question_chars:
                raise ValueError("對話紀錄格式無效")
            normalized.append({"role": item["role"], "content": content})
        return normalized

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
