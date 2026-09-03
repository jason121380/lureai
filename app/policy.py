from __future__ import annotations

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
    # 「提告」擋而「要告我」不擋是同一件事的兩種說法，界線要一致（體檢 B15）。
    "legal_refund_or_compensation": (
        "提告", "訴訟", "法律責任", "律師", "求償", "保證效果",
        "告我", "要告", "告上法院", "法院見", "申訴到", "消保官",
    ),
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
        # 不要無條件認錯——被頂一句就把立場收回去，跟品質守門擋的是同一件事。
        # 先承認沒接到重點（那是事實），再把話題拉回他的狀況（體檢 B16）。
        "我剛剛可能沒接到你的重點\n"
        "與其重講一次一樣的東西 我想先聽你說\n"
        "哪一段最不符合你的狀況",
    ),
    (
        "off_topic",
        ("股票", "台積電", "0050", "比特幣", "虛擬貨幣", "樂透", "運彩",
         "天氣", "食譜", "翻譯這段", "寫程式", "幫我寫作業", "政治", "選舉"),
        "這個不是我的專業唷 我專心陪你顧店裡和客人的事\n"
        "要不要先看私訊還是廣告那一段",
    ),
)

# 邊界題原本是純子字串比對，會誤傷正常的輔導問題：「這個活動對業績沒有幫助
# 要停嗎」不是在罵人、「下雨天氣客人都不來」也不是在問天氣。
# 下面這些詞在日常句子裡太常見，命中時要再過兩道閘（句子夠短、而且沒有在講
# 店裡的事）才算邊界題；「0050」「比特幣」這種強訊號不受限制。
WEAK_BOUNDARY_TERMS = frozenset({
    "天氣", "食譜", "翻譯這段", "政治", "選舉",
    "沒有幫助", "你在講什麼", "亂回", "答非所問", "你很爛", "你沒用",
})
BOUNDARY_MAX_CHARS = 15
# 出現這些字就是在講店裡的事，不是離題也不是在罵人。
COACHING_TERMS = (
    "客人", "顧客", "客戶", "業績", "營業額", "廣告", "投放", "私訊", "訊息", "預約",
    "到店", "客單", "回流", "回頭", "貼文", "限動", "作品", "社群", "版面", "素材",
    "髮", "染", "燙", "剪", "護髮", "頭皮", "店", "沙龍", "設計師", "助理", "櫃檯",
    "價格", "報價", "漲價", "評論", "預算", "追蹤", "話術", "活動", "促銷", "抽成",
    "排班", "訂金", "指名", "成交", "名額", "回覆", "文案", "標籤", "業務", "服務",
)


# 閒聊：打招呼、道謝、應聲、道別這種沒有輔導內容的話。這些不該進檢索——
# 撈不到東西就會回「我手邊的資料不夠」，一句「哈囉」被當成問題，講話就很硬。
#
# 舊版要求「整句正好等於某個詞」，結果設計師實際會打的話幾乎都漏掉：
# 「好 謝謝」「在嗎 教練」「了解 謝謝」全部掉進檢索，撈到不相干知識或
# 「我目前沒有資料」＋紅色徽章（體檢 B2，實測 39 句只認出 5 句）。
# 現在改成「把招呼語一層一層剝掉，看還剩不剩內容」：剝完什麼都不剩才是閒聊，
# 所以「謝謝 那廣告預算怎麼抓」剝完還剩「那廣告預算怎麼抓」，照樣走 RAG。
SMALLTALK_TERMS = (
    "哈囉", "哈嘍", "嗨", "hi", "hello", "hey", "你好", "妳好", "您好",
    "早安", "午安", "晚安", "早", "在嗎", "在不在", "有人在嗎",
    "謝謝", "謝啦", "謝了", "感謝", "感恩", "thanks", "thank you", "thx", "3q",
    "好", "好的", "好喔", "好唷", "了解", "瞭解", "收到", "知道了", "我知道了",
    "ok", "okay", "okok", "嗯", "恩", "沒問題", "辛苦了", "辛苦你了",
    "掰掰", "拜拜", "bye", "再見", "先這樣", "下次聊", "晚點聊",
    "厲害", "讚", "太強了", "你很棒", "不錯",
    # 應聲與附和：實測最常掉進 fallback 的一批。
    "對", "對啊", "對呀", "是喔", "是哦", "這樣啊", "這樣喔", "原來如此",
    "懂了", "了解了", "學到了", "受教了", "蛤", "哈哈", "哈哈哈", "笑死",
    "真的假的", "太棒了", "太好了", "很棒", "超棒", "沒錯",
    # 收尾與客套。
    "我試試", "我試試看", "我會試試", "我去試試", "麻煩你了", "麻煩你",
    "不好意思", "打擾了", "抱歉", "sorry", "明天再聊", "改天聊", "有空再聊",
    "先忙", "我先去忙", "晚點回你", "等一下弄", "等等弄", "去做", "去弄", "去忙",
    "很有幫助", "有幫助", "幫大忙", "得救了",
    # 開場客套（後面沒接問題時才算閒聊，接了就會剩下內容）。
    "請問", "問一下", "想問一下", "可以問一下", "我想問", "請教一下",
    "問個問題", "問你一個問題", "問你個問題",
)

# 標點兩端都去；語助詞只從句尾去（「了解」的「了」在開頭，不能一起剝掉）。
SMALLTALK_PUNCTUATION = "~～!！?？.。,，、 "
SMALLTALK_PARTICLES = "啊呀喔唷哦囉了呢嗎吧欸耶喲"
# 允許在後面加一個稱呼（謝謝你、哈囉大家）。
SMALLTALK_SUFFIXES = ("你", "您", "妳", "大家", "唷", "喔")
# 剝招呼語時順手剝掉的稱呼與人稱代名詞（「在嗎 教練」「沒事 我再想想」）。
# 這些字自己沒有內容，留著會讓剝完的殘句永遠不為空。
SMALLTALK_FILLERS = (
    "教練", "老師", "大家", "各位", "我", "你", "妳", "您", "他", "它",
    # 連接詞與助動詞自己沒有內容：「ok 那我先去做」剝完要是空的才算閒聊。
    "那", "就", "可以", "會", "先",
)
# 超過這個長度就當成有內容的問題，一律走 RAG。
SMALLTALK_MAX_CHARS = 12

# 長的先剝，否則「好的」會先被「好」吃掉半個、殘句變成「的」。
SMALLTALK_STRIPPABLE = tuple(sorted(
    SMALLTALK_TERMS + SMALLTALK_FILLERS, key=len, reverse=True
))

HESITATION_TERMS_SEED = (
    "算了", "沒事", "沒什麼", "沒有啦", "沒事了", "當我沒說", "不說了",
    "我再想想", "再想想", "我再看看", "再看看", "沒關係", "沒差",
)

HESITATION_STRIPPABLE = tuple(sorted(
    HESITATION_TERMS_SEED + SMALLTALK_TERMS + SMALLTALK_FILLERS, key=len, reverse=True
))

# 報喜：「客人回我了！」「成功了 客人約了」不是問題，硬走檢索會撈到
# 「沒來的客人怎麼處理」，等於在他高興的時候潑冷水。
CELEBRATION_TERMS = (
    "成功", "有效", "有用", "真的回", "真的來", "回我了", "約到", "成交",
    "客人回", "客人約", "太棒", "太好", "有人約", "有人問", "被指名",
)
CELEBRATION_MAX_CHARS = 20
# 有否定詞就不是報喜（「客人都不回我」）。
NEGATION_TERMS = ("不", "沒", "別", "未", "難", "少")

# 只有符號或表情的訊息（😂 👍）沒有內容可查，一律當閒聊。
HAS_WORD_CHARS = re.compile(r"[0-9A-Za-z㐀-鿿]")

# 情緒句：對方在抒發，不是在問問題。只承接情緒，不檢索、不派任務、不要數字——
# 同理一句之後接「請給我私訊數 預約數 到店數」等於前功盡棄。
EMOTION_TERMS = (
    "好累", "很累", "累死", "累爆", "好煩", "很煩", "煩死", "超煩", "不爽", "生氣", "火大",
    "氣死", "好挫折", "很挫折", "難過", "想哭", "委屈", "無力", "沒動力", "提不起勁",
    "好焦慮", "很焦慮", "好慌", "很慌", "壓力好大", "壓力很大", "撐不住", "心累",
    "想放棄", "不想做了", "做不下去", "覺得自己很爛", "覺得自己沒用", "懷疑自己",
    "心情不好", "心情很差", "好無奈", "有點difficult", "有點沮喪", "沮喪",
    "好忙", "很忙", "忙死", "忙翻", "好爛", "很爛", "爛透", "白做工", "做白工",
)

# 情緒句裡出現「可以量的數字」時就不要只承接情緒——他是拿數字來問你怎麼看
# （「有點沮喪 廣告花了五千只來一個」），只回一句同理等於沒在聽（體檢 B14）。
# 只認阿拉伯數字與「數字＋單位」的中文寫法；「三個月了」這種時間長度不算，
# 那仍然是在抒發。
METRIC_NUMBER = re.compile(
    r"\d|[一二兩三四五六七八九十百]\s*[百千萬]|[一二兩三四五六七八九十百千萬]\s*[成元塊％%]"
)

# 有這些字就代表他其實在問問題／要東西，情緒判斷讓路給 RAG。
# 注意不要放「可以」這種日常詞：「本來可以接別的客人 有點不爽」是抒發，不是提問。
ACTION_MARKERS = (
    "?", "？", "嗎", "呢", "怎麼", "如何", "該不該", "要不要", "值不值得",
    "幫我", "給我", "教我", "建議", "方法", "怎辦", "該怎", "有沒有辦法",
    "什麼", "哪一", "哪個", "哪些", "幾個", "多久", "多少",
)

# 欲言又止：「算了 沒事」這種，不要當成問題，也不要放他走。
HESITATION_TERMS = HESITATION_TERMS_SEED

# 自我介紹：「我叫小婷」「我在板橋做三年」不是問題，撈不到知識就會掉到
# 「我手邊的資料不夠」，等於一開口就被打槍。這條只能在分流處理，改指令沒用。
SELF_INTRO_PATTERNS = (
    re.compile(r"我叫[\w\u4e00-\u9fff]{1,10}"),
    re.compile(r"我(?:的名字|名字)[是叫]"),
    re.compile(r"我是[\w\u4e00-\u9fff]{0,10}(?:設計師|助理|店長|老闆|新人)"),
    # 「我是小美」也是自我介紹；限定整句就只有這幾個字，才不會吃掉
    # 「我是不是該調價」那種真問題（那句也會先被提問字擋掉）。
    re.compile(r"^我(?:叫|是)(?![不沒別想要])[\w\u4e00-\u9fff]{1,4}$"),
    # 空白要算進去：「我在台中 店裡有五個設計師」中間隔了一個空格。
    re.compile(r"我(?:現在)?在[\w\s\u4e00-\u9fff]{1,12}(?:店|沙龍|工作室|上班|做)"),
    re.compile(r"(?:做|待|待了|入行)\s*\d+\s*年"),
)

# 從對話裡把名字撈出來，之後可以直接叫他的名字（記得名字就要用）。
NAME_PATTERN = re.compile(r"我(?:叫|的名字是|名字是)\s*([\w\u4e00-\u9fff]{1,10})")


def _strip_fillers(text: str, terms) -> str:
    """把招呼語、稱呼、語助詞一層一層剝掉，回傳剩下的內容。

    長的詞先剝（`SMALLTALK_STRIPPABLE` 已排序），否則「好的」會先被「好」
    吃掉半個、殘句變成一個孤零零的「的」。
    """
    remainder = text
    while remainder:
        stripped = remainder.strip(SMALLTALK_PUNCTUATION)
        matched = next((term for term in terms if term and term in stripped), "")
        if matched:
            remainder = stripped.replace(matched, "", 1)
            continue
        # 比不到詞才剝句尾語助詞。順序反過來的話「哈囉」會先被剝成「哈」、
        # 「在嗎」變成「在」、「我知道了」變成「我知道」，整組都認不出來。
        trimmed = stripped.rstrip(SMALLTALK_PARTICLES)
        if trimmed == stripped:
            return stripped
        remainder = trimmed
    return remainder


def _is_bare_aside(normalized: str) -> bool:
    """整句就是那件事（短、而且沒有在講店裡的事）才算離題或敵意。"""
    if len(normalized) > BOUNDARY_MAX_CHARS:
        return False
    return not any(term in normalized for term in COACHING_TERMS)


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
        """非輔導題直接給固定回應，不進檢索。

        比對到的詞如果是日常句子裡也很常見的那幾個（`WEAK_BOUNDARY_TERMS`），
        還要整句夠短、而且沒有在講店裡的事才算數。純子字串比對會把
        「這個活動對業績沒有幫助 要停嗎」當成罵人、把「下雨天氣客人都不來」
        當成在問天氣（體檢 B3）。
        """
        normalized = "".join(str(question or "").lower().split())
        for reason, terms, message in BOUNDARY_REPLIES:
            matched = [term for term in terms if term in normalized]
            if not matched:
                continue
            if all(term in WEAK_BOUNDARY_TERMS for term in matched) and not _is_bare_aside(normalized):
                continue
            return PolicyDecision("direct", reason, self._override(f"reply-{reason}", message))
        return None

    def smalltalk(self, question: str) -> PolicyDecision | None:
        """純打招呼／道謝／應聲／報喜就不要進檢索，讓模型自然接一句話。

        判斷方式是「把招呼語一層一層剝掉，看還剩不剩內容」：
        「好 謝謝」剝完是空的 → 閒聊；
        「謝謝 那廣告預算怎麼抓」剝完還剩「那廣告預算怎麼抓」 → 照常走 RAG。
        舊版要求整句正好等於某個詞，設計師實際會打的話幾乎都漏掉（體檢 B2）。
        """
        raw = str(question or "")
        text_lower = raw.lower()
        normalized = "".join(text_lower.split()).strip(SMALLTALK_PUNCTUATION)
        if not normalized:
            return None
        # 只有表情或符號（😂 👍）沒有東西可查。
        if not HAS_WORD_CHARS.search(normalized):
            return PolicyDecision("smalltalk", "smalltalk")
        if len(normalized) <= SMALLTALK_MAX_CHARS:
            residue = _strip_fillers(normalized, SMALLTALK_STRIPPABLE)
            if not residue:
                return PolicyDecision("smalltalk", "smalltalk")
            # 「算了 沒事」「沒事 我再想想」：剝掉欲言又止的說法之後也不剩東西。
            if not _strip_fillers(normalized, HESITATION_STRIPPABLE):
                return PolicyDecision("smalltalk", "hesitation")
        if self._is_celebration(text_lower, normalized):
            return PolicyDecision("smalltalk", "smalltalk")
        if not any(marker in text_lower for marker in ACTION_MARKERS):
            for pattern in SELF_INTRO_PATTERNS:
                if pattern.search(raw):
                    return PolicyDecision("smalltalk", "self_intro")
        return None

    @staticmethod
    def _is_celebration(text_lower: str, normalized: str) -> bool:
        """他在報好消息（客人回我了、成功約到了），不是在問問題。"""
        if len(normalized) > CELEBRATION_MAX_CHARS:
            return False
        if any(term in normalized for term in NEGATION_TERMS):
            return False
        if any(marker in text_lower for marker in ACTION_MARKERS):
            return False
        return any(term in normalized for term in CELEBRATION_TERMS)

    def emotion_only(self, question: str) -> PolicyDecision | None:
        """在抒發情緒又沒有提問時，只承接情緒，不進檢索。

        「這個時段我本來可以接別的客人 有點不爽」接一句「請給我數字」會把
        前面同理的效果全部抵銷；等他自己問「那我該怎麼調」再去拿知識。
        但句子裡已經有可以量的數字時就要走 RAG——他是拿數字來問你怎麼看，
        只回一句同理等於沒在聽（體檢 B14）。
        """
        text = str(question or "").lower()
        normalized = "".join(text.split())
        if not normalized:
            return None
        if any(marker in text for marker in ACTION_MARKERS):
            return None
        if METRIC_NUMBER.search(normalized):
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
