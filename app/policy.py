import re

from dataclasses import dataclass

from .retrieval import SearchHit


# 預設的查無資料說法：不要提到知識庫、系統或操作方式，那聽起來就像機器人。
FALLBACK_MESSAGE = "這題我手邊的資料不夠 沒辦法給你準的答案\n還是你先跟我說一下你現在的數字"
# 敏感題（退費賠償、勞資、醫療）跟「查不到資料」是兩件事，訊息要分開。
# 這句只留給「真的只有人能決定」的題目（法律責任、醫療診斷、顧客個資）。
# 不要提「轉人工」——這裡沒有人工客服可以接手，說了等於把他丟在原地。
SENSITIVE_MESSAGE = "這題牽涉到法律或醫療 我不能幫你決定\n我可以陪你想怎麼跟客人說 你要先聊哪一邊"


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
    # 只擋真的有法律責任的字眼。「客人要退費」是每天都會遇到的客訴，
    # 擋掉等於這個產品最需要陪伴的時刻反而不說話（實測 QA 的 TASK 4b）。
    "legal_refund_or_compensation": ("提告", "訴訟", "法律責任", "律師", "求償", "保證效果"),
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


# 閒聊：打招呼、道謝、應聲、道別這種沒有輔導內容的話。這些不該進檢索——
# 撈不到東西就會回「我手邊的資料不夠」，一句「哈囉」被當成問題，講話就很硬。
# 條件放很緊（短訊息＋整句就是這些詞），像「謝謝 那廣告預算怎麼抓」這種
# 後面接了真問題的訊息不會被當成閒聊，照樣走 RAG。
SMALLTALK_TERMS = (
    "哈囉", "哈嘍", "嗨", "hi", "hello", "hey", "你好", "妳好", "您好",
    "早安", "午安", "晚安", "早", "在嗎", "在不在", "有人在嗎",
    "謝謝", "謝啦", "感謝", "感恩", "thanks", "thank you", "thx", "3q",
    "好", "好的", "好喔", "好唷", "了解", "瞭解", "收到", "知道了", "我知道了",
    "ok", "okay", "okok", "嗯", "恩", "沒問題", "辛苦了", "辛苦你了",
    "掰掰", "拜拜", "bye", "再見", "先這樣", "下次聊", "晚點聊",
    "厲害", "讚", "太強了", "你很棒", "不錯",
)

# 標點兩端都去；語助詞只從句尾去（「了解」的「了」在開頭，不能一起剝掉）。
SMALLTALK_PUNCTUATION = "~～!！?？.。,，、 "
SMALLTALK_PARTICLES = "啊呀喔唷哦囉了呢嗎吧欸耶"
# 允許在後面加一個稱呼（謝謝你、哈囉大家）。
SMALLTALK_SUFFIXES = ("你", "您", "妳", "大家", "唷", "喔")
# 超過這個長度就當成有內容的問題，一律走 RAG。
SMALLTALK_MAX_CHARS = 12

# 情緒句：對方在抒發，不是在問問題。只承接情緒，不檢索、不派任務、不要數字——
# 同理一句之後接「請給我私訊數 預約數 到店數」等於前功盡棄。
EMOTION_TERMS = (
    "好累", "很累", "累死", "累爆", "好煩", "很煩", "煩死", "不爽", "生氣", "火大",
    "好挫折", "很挫折", "難過", "想哭", "委屈", "無力", "沒動力", "提不起勁",
    "好焦慮", "很焦慮", "好慌", "很慌", "壓力好大", "壓力很大", "撐不住",
    "想放棄", "不想做了", "做不下去", "覺得自己很爛", "覺得自己沒用", "懷疑自己",
    "心情不好", "心情很差", "好無奈", "有點difficult", "有點沮喪", "沮喪",
)

# 有這些字就代表他其實在問問題／要東西，情緒判斷讓路給 RAG。
# 注意不要放「可以」這種日常詞：「本來可以接別的客人 有點不爽」是抒發，不是提問。
ACTION_MARKERS = (
    "?", "？", "嗎", "呢", "怎麼", "如何", "該不該", "要不要", "值不值得",
    "幫我", "給我", "教我", "建議", "方法", "怎辦", "該怎", "有沒有辦法",
    "什麼", "哪一", "哪個", "哪些", "幾個", "多久", "多少",
)

# 欲言又止：「算了 沒事」這種，不要當成問題，也不要放他走。
HESITATION_TERMS = ("算了", "沒事", "沒什麼", "沒有啦", "沒事了", "當我沒說", "不說了")

# 自我介紹：「我叫小婷」「我在板橋做三年」不是問題，撈不到知識就會掉到
# 「我手邊的資料不夠」，等於一開口就被打槍。這條只能在分流處理，改指令沒用。
SELF_INTRO_PATTERNS = (
    re.compile(r"我叫[\w\u4e00-\u9fff]{1,10}"),
    re.compile(r"我(?:的名字|名字)[是叫]"),
    re.compile(r"我是[\w\u4e00-\u9fff]{0,10}(?:設計師|助理|店長|老闆|新人)"),
    re.compile(r"我(?:現在)?在[\w\u4e00-\u9fff]{1,12}(?:店|沙龍|工作室|上班|做)"),
    re.compile(r"(?:做|待|待了|入行)\s*\d+\s*年"),
)

# 從對話裡把名字撈出來，之後可以直接叫他的名字（記得名字就要用）。
NAME_PATTERN = re.compile(r"我(?:叫|的名字是|名字是)\s*([\w\u4e00-\u9fff]{1,10})")


def speaker_name(messages) -> str:
    """從使用者說過的話裡找出他的名字；找不到就回空字串。"""
    for text in reversed([str(item or "") for item in (messages or [])]):
        match = NAME_PATTERN.search(text)
        if match:
            return match.group(1).strip("。，、!！?？ ")[:10]
    return ""


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
        rules_provider=None,
    ):
        self.minimum_score = minimum_score
        self.blocked_topics = SENSITIVE_TOPICS if blocked_topics is None else blocked_topics
        self._fallback_message = fallback_message
        self._sensitive_message = sensitive_message
        # 後台「AI 模型校調」改過的固定回覆句；沒改就用建構時給的預設。
        self.rules_provider = rules_provider

    def _override(self, rule_id: str, default: str) -> str:
        if not self.rules_provider:
            return default
        try:
            value = (self.rules_provider() or {}).get(rule_id)
        except Exception:  # noqa: BLE001 - 讀不到就用預設，不能讓回答掛掉
            return default
        return value if value and value.strip() else default

    @property
    def fallback_message(self) -> str:
        return self._override("reply-fallback", self._fallback_message)

    @property
    def sensitive_message(self) -> str:
        return self._override("reply-sensitive", self._sensitive_message)

    def boundary_reply(self, question: str) -> PolicyDecision | None:
        """非輔導題直接給固定回應，不進檢索。"""
        normalized = "".join(str(question or "").lower().split())
        for reason, terms, message in BOUNDARY_REPLIES:
            if any(term in normalized for term in terms):
                return PolicyDecision("direct", reason, self._override(f"reply-{reason}", message))
        return None

    def smalltalk(self, question: str) -> PolicyDecision | None:
        """純打招呼／道謝／應聲就不要進檢索，讓模型自然接一句話。

        比對得很嚴：整句去掉語助詞後要正好是那個詞（或疊字、或加個稱呼）。
        用「包含」比對會把「好累」當成「好」，但那是有知識可查的情緒題。
        """
        raw = str(question or "")
        text_lower = raw.lower()
        normalized = "".join(text_lower.split()).strip(SMALLTALK_PUNCTUATION)
        if not normalized:
            return None
        if len(normalized) > SMALLTALK_MAX_CHARS:
            # 太長的一律當成有內容的問題，只有自我介紹例外（「我叫小婷 在板橋做三年」）。
            if not any(marker in text_lower for marker in ACTION_MARKERS):
                for pattern in SELF_INTRO_PATTERNS:
                    if pattern.search(raw):
                        return PolicyDecision("smalltalk", "self_intro")
            return None
        # 原句與「去掉句尾語助詞」的版本都比一次：「哈囉」本身是招呼語，
        # 「好喔」則要剝掉「喔」才等於「好」。
        candidates = {normalized, normalized.rstrip(SMALLTALK_PARTICLES)}
        for candidate in candidates:
            if not candidate:
                continue
            for term in SMALLTALK_TERMS:
                if candidate in (term, term * 2) or any(
                    candidate == term + suffix for suffix in SMALLTALK_SUFFIXES
                ):
                    return PolicyDecision("smalltalk", "smalltalk")
        if not any(marker in text_lower for marker in ACTION_MARKERS):
            for pattern in SELF_INTRO_PATTERNS:
                if pattern.search(raw):
                    return PolicyDecision("smalltalk", "self_intro")
        # 「算了 沒事」是兩個詞接在一起，逐個剝掉之後如果什麼都不剩就是欲言又止。
        remainder = normalized
        for term in HESITATION_TERMS:
            remainder = remainder.replace(term, "")
        if normalized != remainder and not remainder.strip(SMALLTALK_PARTICLES):
            return PolicyDecision("smalltalk", "hesitation")
        return None

    def emotion_only(self, question: str) -> PolicyDecision | None:
        """在抒發情緒又沒有提問時，只承接情緒，不進檢索。

        「這個時段我本來可以接別的客人 有點不爽」接一句「請給我數字」會把
        前面同理的效果全部抵銷；等他自己問「那我該怎麼調」再去拿知識。
        """
        text = str(question or "").lower()
        normalized = "".join(text.split())
        if not normalized:
            return None
        if any(marker in text for marker in ACTION_MARKERS):
            return None
        if any(term in normalized for term in EMOTION_TERMS):
            return PolicyDecision("smalltalk", "emotion")
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
