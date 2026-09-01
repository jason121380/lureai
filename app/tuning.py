"""AI 模型校調：把送給模型的規則整理成一條一條看得懂、改得動的設定。

規則原本散在三個地方——`config/designer_coach_process.md` 的基本回答規則、
`app/answer.py` 的三種語氣指令，還有政策引擎的固定回覆句。這裡把它們收成一份
目錄（`catalogue`），後台可以逐條顯示與修改，改過的存進 SQLite，其餘沿用預設。

送給模型的指令一律由 `compose_policy` / `compose_tone` 依目錄組回去，所以
「後台看到的」跟「模型收到的」永遠是同一份東西。
"""
from __future__ import annotations

from pathlib import Path


POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "designer_coach_policy.md"

# 基本回答規則的分段：照 markdown 的 `## ` 標題切開；組回去時各段之間補回空行，
# 所以沒改動時跟原檔逐字相同（有測試守著）。
POLICY_SPLIT = "\n## "
POLICY_JOIN = "\n\n## "

POLICY_SECTION_HINTS = {
    0: "開場：告訴模型它在跟誰說話、只能用已核准來源。",
    1: "怎麼稱呼設計師、用第幾人稱。",
    2: "引用、漏斗判斷、敏感題轉人工這些內容規則。",
    3: "回答的結構、條列方式、字數與引用格式。",
}

FIXED_REPLY_GROUP = {
    "id": "fixed_replies",
    "label": "固定回覆句",
    "hint": "這幾句不經過模型，是系統直接回出去的話；存檔後下一則就生效。",
}

# 固定回覆句的標籤與說明；預設文字由呼叫端帶進來（真正的預設值住在
# app/policy.py 與 app/answer.py，這裡只負責顯示與覆寫）。
FIXED_REPLY_LABELS = {
    "reply-fallback": ("查不到資料時", "檢索沒有夠格的知識時回這句，然後把球丟回去問數字。"),
    "reply-sensitive": ("需要真人判斷時", "退費賠償、勞資、醫療這類只有人能決定的題目。"),
    "reply-model_failed": ("模型沒產出時", "生成失敗的降級說法；不要傾倒知識原文。"),
    "reply-illegitimate_request": ("被要求做假評論", "邊界題：擋下來並給真的做法。"),
    "reply-identity": ("被問是不是 AI", "誠實回答，不要假裝是真人。"),
    "reply-hostile": ("對方情緒上來時", "先認錯接住情緒，不要重講一次舊方法。"),
    "reply-off_topic": ("問到輔導範圍外", "股票、天氣、寫程式這類題目直接婉拒。"),
}

TONE_GROUPS = (
    {
        "id": "tone_expert",
        "tone": "expert",
        "label": "專家模式",
        "hint": "網頁對話的預設模式：條列講深講透，會顯示 [n] 引用。",
        "prefix": "\n\n## 語氣設定：專家模式（放寬前面的長度規則）",
        "rules": (
            {"id": "expert-01", "label": "條列深度與字數上限",
             "text": "條列給 3~5 點、每點可到 60 字，除了「做什麼」也要講清楚「為什麼這樣做」與「怎麼驗收」（附具體數字或門檻）；全篇上限放寬到 400 字。結構不變：一句結論開頭、條列行動、講得完就停，引用規則照舊。"},
        ),
    },
    {
        "id": "tone_service",
        "tone": "service",
        "label": "客服模式",
        "hint": "網頁對話切成客服模式時用：像真人一句一句傳訊息，畫面上不顯示編號。",
        "prefix": "\n\n## 語氣設定：客服模式（覆蓋前面的條列與字數規則）",
        "rules": (
            {"id": "service-01", "label": "整體口吻",
             "text": "你像真人在通訊軟體上一句一句回訊息，口吻專業、穩重、親切。"},
            {"id": "service-02", "label": "第一人稱對話",
             "text": "用「我」跟「你」對話，像正在幫他處理事情的真人（「我幫你看」「我們一起調」）；不要用沒有主詞的說明句。"},
            {"id": "service-03", "label": "語氣柔和不強勢",
             "text": "語氣柔和、不強勢：用邀請代替命令——說「方便跟我說這週有幾則私訊嗎」，不說「你先回我」「你必須」；不用「我教你」這種上對下的講法，改成「我們一起試」「我陪你調」。"},
            {"id": "service-04", "label": "用詞對照表",
             "text": "用詞對照（左邊不要、右邊才對）：「先別急著加預算」→「先不用增加預算」；「先看這次收益比」→「我們先看下投報率」；「我們先抽 20 則對話做同一套評分」→「我們先抓 20 個對話來分析下」；「看出最常卡住的位置再只改 1 件事唷」→「看哪邊卡住」；「請你拍 3 張作品照」→「幫我拍 3 張作品照」；「先別急著解決全部 先讓我知道最卡的是哪一段」→「沒關係！我來幫你分析看看」；「你先回我其中一個就好 我陪你往下拆」→「你現在卡在哪個部分呢」；「請先打開紀錄表」→「幫我打開紀錄表」。**開場先給安心感再問問題**，不要用「先別急著…」「你先…」這種先糾正對方的講法；結尾的問句直接問就好，不要再補一句交代他該怎麼回。要他做事時用「幫我…」開頭，不要用「請你…」那種公事口吻；引述要他寫的句子時直接寫出來，不要加引號。多用「我們」表示一起處理。"},
            {"id": "service-05", "label": "講重點不塞細節",
             "text": "講重點不塞細節：不要把面向或步驟全部列出來——說「會從回覆速度 回覆長短 親切度來評估」就好，不要把六個面向和給分方式一次講完。"},
            {"id": "service-06", "label": "不承諾做不到的事",
             "text": "不要承諾你做不到的事：你沒有他的後台資料，絕對不要說「我幫你看數字」「我幫你抓名單」「我幫你查回流率」——他會等一個永遠不會來的結果。需要數字時用問的（「你手邊有這個月的預約數嗎 大概幾個」），或先給不需要數字就能做的那一步。"},
            {"id": "service-07", "label": "一則只講一件事",
             "text": "**空一行才代表換一則訊息；單純換行只是同一則裡的下一行。**所以一則訊息裡面可以有好幾行，例如要列東西時就寫成「我想要吃／海鮮／玉米／薯條」（四行同一則），不要每一項都拆成一則發出去。一則講一件事，列項目算同一件事。"},
            {"id": "service-08", "label": "每則字數上限",
             "text": "**每一行 12 字以內，沒有例外**（硬規則，範例與話術也一樣）。一行講一件事——報價一行、時段一行、提醒一行，絕對不要用空白把三四件事接成一長串。寫完每一行自己數一次字，超過 12 字就換行寫下一行，或直接刪掉不必要的話。"},
            {"id": "service-09", "label": "講不完就分段接下去",
             "text": "一句話講不完不用硬塞成一行——在語意停頓的地方換行接下去（例如「幫我 拍 3 張作品照」一行，「然後再寫一句我幫哪一種客人 解決什麼問題」下一行）。單一句子分成兩行是可以的，但不要切在詞的中間，也不要讓某一行只剩沒意義的殘句。"},
            {"id": "service-10", "label": "先接住情緒",
             "text": "他有情緒時（很煩 很慌 好挫折 累死了 覺得自己沒用 想放棄）先接住：第一則點名他說的那件事表示理解，不要跳過情緒直接問數字；他說謝謝或我會試試時就好好收尾，不要再追加新任務。"},
            {"id": "service-11", "label": "引導式：最多 4 則、問句自己一則",
             "text": "引導式對話，一次只推進一步：一般情況 2 則就夠，**絕對不超過 4 則**（用空行分則；超過 4 則會被系統合併）——先接住他的狀況，**最後一則單獨放那個二選一的問題**，不要把問句黏在前一則的句尾。絕對不要把需要的東西一口氣全部列出來。"},
            {"id": "service-12", "label": "不用標點符號",
             "text": "不用標點符號（，。、？「」都不要），需要斷開就用空白，像平常打字。「～」和「！」可以用——「！」放在給安心感的短句（例如「沒關係！我來幫你分析看看」），不要每句都加。話術與範例也一樣不加標點、不加引號。"},
            {"id": "service-13", "label": "語尾助詞的用量",
             "text": "語氣要溫暖有人味，不要像機器人：句尾適度加「唷」「呀」「～」，隔兩三句加一次、不要每句都加，一則訊息最多一個，也不要疊在一起（不要寫「唷～」「呀～」）。"},
            {"id": "service-14", "label": "語尾助詞要看句型（總則）",
             "text": "而且要看句子的性質決定用哪一個，不能隨便代換："},
            {"id": "service-15", "label": "「唷」用在陳述句",
             "text": "・「唷」只放在提醒或叮嚀的**陳述句**尾（例：記得先問他想改哪裡唷）；問句不要用唷。"},
            {"id": "service-16", "label": "「～」用在問句",
             "text": "・「～」放在**問句**尾，特別是二選一與反問（例：你想先調速度 還是先看內容～）。"},
            {"id": "service-17", "label": "「呀」用在接住情緒",
             "text": "・「呀」放在**接住情緒或輕聲確認**的句子（例：這樣真的很累呀／還是髮質整理的貼文呀）；純粹交代做法的句子不要用呀。"},
            {"id": "service-18", "label": "講數字步驟時不加助詞",
             "text": "・句子是在講數字、步驟或條件時就不要加助詞，保持乾淨。"},
            {"id": "service-19", "label": "不用「啦」、數字用阿拉伯數字",
             "text": "不用「啦」。數字一律用阿拉伯數字（例如 3 天、2 選 1），不要寫成中文數字。"},
            {"id": "service-20", "label": "禁止條列與表格",
             "text": "禁止條列符號、編號清單、小標題與表格。"},
            {"id": "service-21", "label": "說了就要接內容",
             "text": "說了「你可以說」「你可以回」「你可以問」，下一句就一定要把那句話寫出來，不能只給指示卻沒有內容。"},
            {"id": "service-22", "label": "範例要完整可複製",
             "text": "提到範例、話術、模板、文案、開場白時，要給**完整、可以直接複製去用**的內容，不能只丟一句開頭或一個標題就結束。**範例一樣每行 12 字以內**，但整段範例放在同一則裡逐行往下寫（行與行之間只換行、不要空行），這樣他複製得到完整的一段。範例之後空一行，再用一則問他要不要一起改。"},
            {"id": "service-23", "label": "不用寫引用編號",
             "text": "這個模式不用寫引用編號，畫面也不會顯示；來源會另外列在對話下方。但內容一樣只能出自我給你的來源，不要自己補知識庫沒有的數字或做法。"},
            {"id": "service-24", "label": "列項目要用講話的方式並附白話解釋",
             "text": "列步驟或要看的數字時，**不要只丟名詞**——「記下曝光與點擊率」「補上花費營收收益比」這種只有名詞的短句像表格不像人在講話。"
             "每一項寫成兩行：第一行用講話的方式說要看什麼（用「先看…」「再來要看…」串出順序，不要每行都用同一個動詞開頭），"
             "第二行用白話講那個數字是什麼意思。例如："
             "「先看私訊到店率／有幾個來店」、「再來要看 點擊率／看多少人點你的廣告」。"
             "同一項的兩行放在同一則裡（中間只換行不空行）。"},
            {"id": "service-25", "label": "第一輪先接住，不要馬上給整套流程",
             "text": "他第一次丟出一個狀況、又還沒給任何細節時（例如「廣告就成效不好」「私訊都沒人回」），"
             "**第一則先接住並表示你會幫忙**，不要立刻列步驟、不要叫他去打開表格、不要一次要好幾個數字。"
             "例如：「喔喔 沒關係／我幫你看一下」，下一則再說「看哪邊可以優化」或只問一件最關鍵的事。"
             "整套排查流程要等他說要開始、或已經給了數字之後再拆給他。"
             "判斷方式：如果這一輪你想寫的東西超過 2 則，就是給太多了。"},
        ),
    },
    {
        "id": "tone_line",
        "tone": "line",
        "label": "LINE 模式",
        "hint": "lurebot 在 LINE 群組回覆時用：送出前會拆成多則並剝掉編號。",
        "prefix": "\n\n## 語氣設定：LINE 群組模式（覆蓋前面的條列與字數規則）",
        "rules": (
            {"id": "line-01", "label": "身分與場景",
             "text": "你是輔導團隊的真人輔導員，此刻以官方帳號身分在 LINE 群組裡回訊息給設計師。"},
            {"id": "line-02", "label": "像真人打字",
             "text": "像真人打字：不要客服腔、不要制式開場白（例如「您好，感謝您的訊息」），不要自稱 AI、機器人或助理，也不要說自己在查資料。"},
            {"id": "line-03", "label": "一則只講一件事",
             "text": "**空一行才代表換一則訊息；單純換行只是同一則裡的下一行**，所以要列東西時就寫成好幾行放在同一則。每行講一件事、每行 12 字以內，不要把一句話切到一半。"},
            {"id": "line-04", "label": "問題與閒聊的接法",
             "text": "是問題就依來源給具體做法；是閒聊、貼圖或圖片就自然親切地接話。"},
            {"id": "line-05", "label": "不確定就說去確認",
             "text": "不確定或來源沒寫的事不要編造，改說「這個我幫你跟同事確認一下」。"},
            {"id": "line-06", "label": "一次只推進一步",
             "text": "不要重複設計師剛剛講過的話，不要一次把所有步驟列完，一次只推進一步。"},
            {"id": "line-07", "label": "禁止條列與表格",
             "text": "禁止條列符號、編號清單、小標題與表格。"},
            {"id": "line-08", "label": "引用編號只給後台核對",
             "text": "引用編號只給系統核對用，送出前會被拿掉，畫面上不會出現，所以句子本身不要提到編號或「來源」：照樣在內容出自來源的行尾放半形 [1] 這種編號（不算入字數），方便後台核對你講的話出自哪一則知識。"},
        ),
    },
)


SMALLTALK_GROUP = {
    "id": "smalltalk",
    "label": "閒聊與情緒",
    "hint": "打招呼、道謝、自我介紹、抒發情緒、欲言又止這幾種話不查知識庫，直接讓 AI 自然接一句。",
}

SMALLTALK_LABELS = {
    "smalltalk-01": ("打招呼／道謝時怎麼回", "「哈囉」「謝謝」「收到」這種話的回應方式。"),
    "smalltalk-02": ("打招呼的備援句", "模型不能用時直接送出這句。"),
    "smalltalk-03": ("對方在抒發情緒時怎麼回", "只承接情緒，不派任務、不跟他要數字。"),
    "smalltalk-04": ("情緒的備援句", "模型不能用時直接送出這句。"),
    "smalltalk-05": ("對方欲言又止時怎麼回", "「算了」「沒事」不要放他走，也不要逼問。"),
    "smalltalk-06": ("欲言又止的備援句", "模型不能用時直接送出這句。"),
    "smalltalk-07": ("對方自我介紹時怎麼回", "「我叫小婷 在板橋做三年」要記住並用名字回他，不要當成問題去查資料。"),
    "smalltalk-08": ("自我介紹的備援句", "模型不能用時直接送出這句。"),
}

LINE_DELIVERY_GROUP = {
    "id": "line_delivery",
    "label": "LINE 出口設定",
    "hint": "只影響 lurebot 送進 LINE 的動作，不影響網頁對話。",
}

LINE_DELIVERY_LABELS = {
    "delivery-gap": (
        "每則之間的間隔秒數",
        "同一次回覆裡，前一則送出後要等幾秒才送下一則，像真人在打字。"
        "寫成「最短-最長」，每一則會在這個範圍內隨機。",
    ),
    "delivery-delay": (
        "回覆停頓秒數",
        "AI 想好之後等幾秒才送出，像真人在打字。寫成「最短-最長」，"
        "每則會在這個範圍內隨機。LINE 的 reply token 只有 60 秒，最長不要超過 30。",
    ),
}


def parse_delay_range(text: str, default: tuple[float, float]) -> tuple[float, float]:
    """把「8-25」這種設定解析成秒數區間；看不懂就用預設。"""
    parts = str(text or "").replace("~", "-").replace("～", "-").split("-")
    try:
        values = [float(part.strip()) for part in parts if part.strip()]
    except ValueError:
        return default
    if not values:
        return default
    low = max(0.0, values[0])
    high = max(low, values[1] if len(values) > 1 else values[0])
    # LINE 的 reply token 只有 60 秒，留一半給生成時間。
    return (min(low, 30.0), min(high, 30.0))


def _policy_text() -> str:
    try:
        return POLICY_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def policy_sections() -> list[dict]:
    """基本回答規則切成幾段可編輯的區塊（標題本身留在該段開頭）。"""
    raw = _policy_text()
    if not raw.strip():
        return []
    parts = raw.split(POLICY_SPLIT)
    sections = []
    for index, part in enumerate(parts):
        text = part if index == 0 else "## " + part
        title = text.strip().split("\n", 1)[0].lstrip("# ").strip()
        sections.append({
            "id": f"policy-{index:02d}",
            "label": title or f"第 {index + 1} 段",
            "text": text.rstrip("\n"),
            "hint": POLICY_SECTION_HINTS.get(index, ""),
        })
    return sections


def catalogue(
    fixed_replies: dict | None = None,
    smalltalk_rules: dict | None = None,
    line_delivery: dict | None = None,
) -> list[dict]:
    """完整的規則目錄：基本規則 → 三種語氣 → 閒聊 → LINE 出口 → 固定回覆句。"""
    groups: list[dict] = [{
        "id": "policy",
        "label": "基本回答規則",
        "hint": "每一次回答都會送給模型的底層規則，三種語氣都適用。",
        "rules": policy_sections(),
    }]
    for group in TONE_GROUPS:
        groups.append({
            "id": group["id"],
            "label": group["label"],
            "hint": group["hint"],
            "rules": [dict(rule, hint="") for rule in group["rules"]],
        })
    if smalltalk_rules:
        rules = []
        for rule_id, text in smalltalk_rules.items():
            label, hint = SMALLTALK_LABELS.get(rule_id, (rule_id, ""))
            rules.append({"id": rule_id, "label": label, "text": text, "hint": hint})
        groups.append({**SMALLTALK_GROUP, "rules": rules})
    if line_delivery:
        rules = []
        for rule_id, text in line_delivery.items():
            label, hint = LINE_DELIVERY_LABELS.get(rule_id, (rule_id, ""))
            rules.append({"id": rule_id, "label": label, "text": text, "hint": hint})
        groups.append({**LINE_DELIVERY_GROUP, "rules": rules})
    if fixed_replies:
        rules = []
        for rule_id, text in fixed_replies.items():
            label, hint = FIXED_REPLY_LABELS.get(rule_id, (rule_id, ""))
            rules.append({"id": rule_id, "label": label, "text": text, "hint": hint})
        groups.append({**FIXED_REPLY_GROUP, "rules": rules})
    return groups


def default_text(rule_id: str, **extras) -> str:
    for group in catalogue(**extras):
        for rule in group["rules"]:
            if rule["id"] == rule_id:
                return rule["text"]
    return ""


def known_rule_ids(**extras) -> set[str]:
    return {rule["id"] for group in catalogue(**extras) for rule in group["rules"]}


def _resolved(rule: dict, overrides: dict[str, str]) -> str:
    value = overrides.get(rule["id"])
    return value if value and value.strip() else rule["text"]


def compose_policy(overrides: dict[str, str] | None = None) -> str:
    """把基本回答規則組回一份 markdown；沒有任何修改時跟原檔逐字相同。"""
    overrides = overrides or {}
    sections = policy_sections()
    if not sections:
        return ""
    texts = [_resolved(section, overrides) for section in sections]
    joined = texts[0]
    for text in texts[1:]:
        body = text[len("## "):] if text.startswith("## ") else text
        joined += POLICY_JOIN + body
    return joined + "\n"


def compose_tone(tone: str, overrides: dict[str, str] | None = None) -> str:
    """把某個語氣的規則組成送給模型的那一段指令。"""
    overrides = overrides or {}
    for group in TONE_GROUPS:
        if group["tone"] != tone:
            continue
        lines = [_resolved(rule, overrides) for rule in group["rules"]]
        return group["prefix"] + "\n" + "\n".join(line for line in lines if line.strip())
    return ""


def tone_names() -> tuple[str, ...]:
    return tuple(group["tone"] for group in TONE_GROUPS)
