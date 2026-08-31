from dataclasses import dataclass

from .retrieval import SearchHit


FALLBACK_MESSAGE = "目前知識庫沒有足夠且已核准的資料，我幫您轉由專人確認。"


# 提問者是「設計師本人」，不是顧客。談客人的報價、追客、頭皮狀況、店內請假
# 規定都是輔導範圍，知識庫也寫得很清楚，轉人工反而幫不到他；真正要轉人工的是
# 只有人能決定或會有法律責任的事。
SENSITIVE_TOPICS = {
    "live_schedule": (
        "幫我預約", "可以幫我約", "預約今天", "預約明天", "預約後天",
        "現在有空嗎", "現在的價目", "目前價目", "現行價目",
    ),
    "personal_or_payment": ("身分證", "信用卡", "卡號", "銀行帳號", "付款資料", "住家地址"),
    "health_or_medical": ("醫療", "醫生", "就醫", "診斷", "處方", "藥物治療"),
    "legal_refund_or_compensation": ("退款", "退費", "賠償", "補償", "提告", "訴訟", "法律責任", "保證效果"),
    "labor_hr": ("勞基法", "勞動法", "資遣", "解雇", "職業災害", "勞資爭議"),
}


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    message: str = ""


class PolicyEngine:
    def __init__(
        self,
        minimum_score: float = 0.72,
        blocked_topics: dict | None = None,
        fallback_message: str = FALLBACK_MESSAGE,
    ):
        self.minimum_score = minimum_score
        self.blocked_topics = SENSITIVE_TOPICS if blocked_topics is None else blocked_topics
        self.fallback_message = fallback_message

    def precheck(self, question: str) -> PolicyDecision:
        normalized = "".join(str(question or "").lower().split())
        for reason, terms in self.blocked_topics.items():
            if any(term in normalized for term in terms):
                return PolicyDecision("escalate", reason, self.fallback_message)
        return PolicyDecision("continue", "passed")

    def evaluate(self, hits: list[SearchHit]) -> PolicyDecision:
        if not hits:
            return PolicyDecision("escalate", "no_results", self.fallback_message)
        top = hits[0]
        if top.score < self.minimum_score:
            return PolicyDecision("escalate", "low_confidence", self.fallback_message)
        for hit in hits:
            if not all((hit.chunk_id, hit.title, hit.source_file, hit.locator)):
                return PolicyDecision("escalate", "missing_citation", self.fallback_message)
        return PolicyDecision("answer", "grounded")
