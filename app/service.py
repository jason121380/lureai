from datetime import datetime, timezone
from uuid import uuid4

from .answer import AnswerEngine
from .policy import PolicyEngine
from .retrieval import Retriever
from .storage import KnowledgeStore


class CustomerService:
    def __init__(
        self,
        store: KnowledgeStore,
        retriever: Retriever,
        policy: PolicyEngine,
        answerer: AnswerEngine,
        top_k: int = 6,
        max_question_chars: int = 1200,
    ):
        self.store = store
        self.retriever = retriever
        self.policy = policy
        self.answerer = answerer
        self.top_k = top_k
        self.max_question_chars = max_question_chars

    def chat(self, message: str, conversation_id: str | None = None) -> dict:
        question = str(message or "").strip()
        if not question:
            raise ValueError("問題不可為空")
        if len(question) > self.max_question_chars:
            raise ValueError(f"問題不可超過 {self.max_question_chars} 個字")

        trace_id = str(uuid4())
        precheck = self.policy.precheck(question)
        if precheck.action == "escalate":
            result = {
                "trace_id": trace_id,
                "conversation_id": conversation_id,
                "status": "escalated",
                "reason": precheck.reason,
                "answer": precheck.message,
                "citations": [],
                "answer_mode": "policy",
            }
            self._audit(question, result, [])
            return result

        hits = self.retriever.retrieve(question, limit=self.top_k)
        decision = self.policy.evaluate(hits)
        if decision.action == "escalate":
            result = {
                "trace_id": trace_id,
                "conversation_id": conversation_id,
                "status": "escalated",
                "reason": decision.reason,
                "answer": decision.message,
                "citations": [],
                "answer_mode": "policy",
            }
            self._audit(question, result, hits)
            return result

        grounded_hits = [hit for hit in hits if hit.score >= self.policy.minimum_score]
        answer, mode = self.answerer.answer(question, grounded_hits)
        result = {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "status": "answered",
            "reason": "grounded",
            "answer": answer,
            "citations": [hit.citation() for hit in grounded_hits],
            "answer_mode": mode,
        }
        self._audit(question, result, hits)
        return result

    def _audit(self, question: str, result: dict, hits: list) -> None:
        self.store.add_audit({
            "trace_id": result["trace_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "conversation_id": result.get("conversation_id"),
            "question": question,
            "status": result["status"],
            "reason": result["reason"],
            "top_score": hits[0].score if hits else None,
            "chunk_ids": [hit.chunk_id for hit in hits],
        })
