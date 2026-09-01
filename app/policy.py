from dataclasses import dataclass

from .retrieval import SearchHit


# 預設的查無資料說法：不要提到知識庫、系統或操作方式，那聽起來就像機器人。
FALLBACK_MESSAGE = "這題我手邊的資料不夠 沒辦法給你準的答案\n還是你先跟我說一下你現在的數字"
# 敏感題（退費賠償、勞資、醫療）跟「查不到資料」是兩件事，訊息要分開。
SENSITIVE_MESSAGE = "這題要人來判斷比較保險唷\n我先不亂給方向 你跟主管確認過我們再接著談"


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



# 邊界題：不進檢索，直接用固定回應。設計師問股票、要求寫假評論、問「你是不是
# AI」、或情緒上來罵人時，硬走 RAG 只會撈到不相干知識然後崩潰（健檢報告 P0-3）。
BOUNDARY_REPLIES = (
    (
        "illegitimate_request",
        ("假評論", "假的評論", "假五星", "假好評", "刷評論", "刷好評", "灌評論",
         "假留言", "寫假的", "假數據", "假客人"),
        "這個我不能幫你做唷 假評論被抓到會傷到你的招牌\n"
        "我們可以用真的方式衝評論 服務完當場邀請客人留言 並指名你\n"
        "要我陪你把邀請的句子寫出來嗎",
    ),
    (
        "identity",
        ("你是ai嗎", "你是不是ai", "你是真人還是", "你是真人嗎", "你是機器人",
         "你是不是機器人", "你是人嗎"),
        "我是 AI 教練唷 背後接的是我們自己整理的輔導知識\n"
        "所以我給的方法都有出處 不是隨口說的\n"
        "你想先從哪一段聊 私訊還是預約",
    ),
    (
        "hostile",
        ("你根本不懂", "你懂什麼", "你很爛", "你沒用", "廢物", "答非所問",
         "你在講什麼", "亂回", "沒有幫助"),
        "你講得對 我剛剛沒有接到你的重點 抱歉\n"
        "我們不重講一次舊方法\n"
        "你覺得最不符合你狀況的是哪一段",
    ),
    (
        "off_topic",
        ("股票", "台積電", "0050", "比特幣", "虛擬貨幣", "樂透", "運彩",
         "天氣", "食譜", "翻譯這段", "寫程式", "幫我寫作業", "政治", "選舉"),
        "這個不是我的專業唷 我專心陪你顧店裡和客人的事\n"
        "要不要先看私訊還是廣告那一段",
    ),
)


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
        sensitive_message: str = SENSITIVE_MESSAGE,
    ):
        self.minimum_score = minimum_score
        self.blocked_topics = SENSITIVE_TOPICS if blocked_topics is None else blocked_topics
        self.fallback_message = fallback_message
        self.sensitive_message = sensitive_message

    def boundary_reply(self, question: str) -> PolicyDecision | None:
        """非輔導題直接給固定回應，不進檢索。"""
        normalized = "".join(str(question or "").lower().split())
        for reason, terms, message in BOUNDARY_REPLIES:
            if any(term in normalized for term in terms):
                return PolicyDecision("direct", reason, message)
        return None

    def precheck(self, question: str) -> PolicyDecision:
        normalized = "".join(str(question or "").lower().split())
        for reason, terms in self.blocked_topics.items():
            if any(term in normalized for term in terms):
                return PolicyDecision("escalate", reason, self.sensitive_message)
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
