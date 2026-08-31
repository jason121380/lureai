from dataclasses import dataclass

from .retrieval import SearchHit


FALLBACK_MESSAGE = "目前知識庫沒有足夠且已核准的資料，我幫您轉由專人確認。"


SENSITIVE_TOPICS = {
    "price_or_promotion": ("多少錢", "價格", "價錢", "費用", "優惠", "折扣", "特價", "活動價"),
    "live_schedule": (
        "營業時間", "幾點開", "幾點關", "幫我預約", "可以預約", "想預約",
        "預約今天", "預約明天", "預約後天", "有空嗎", "檔期", "指定設計師",
    ),
    "personal_or_payment": ("身分證", "信用卡", "卡號", "帳號", "付款", "電話號碼", "住址"),
    "health_or_medical": ("過敏", "紅腫", "傷口", "疼痛", "頭皮痛", "掉髮", "醫療", "醫生", "診斷"),
    "legal_refund_or_compensation": ("退款", "退費", "賠償", "補償", "法律", "提告", "保證效果"),
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
