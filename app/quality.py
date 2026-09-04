"""回覆品質守門：診斷完之後給不出東西，是實測扣分最重的一項。

實測 201 輪裡有 51 次（25%）的主要內容只是「我陪你一起拆」這類延後回答的句子，
另外有 8 起「說要給成品卻沒有給」。這些用指令勸模型沒有用——模型每次都覺得
自己有回答——所以在這裡用程式判斷，不合格就帶著具體理由重新生成一次。

判斷刻意保守：寧可放過一則邊緣的，也不要把好答案擋掉（擋掉會變成降級訊息，
那比廢話更糟）。
"""
from __future__ import annotations

import re


# 「我陪你拆」這種把回答往後推的句型。單獨出現＝這一輪等於沒講。
DELAY_PATTERNS = (
    re.compile(r"我(先|會|再)*(陪|幫)你(一起)?(拆|看|釐清|理順|梳理|整理|判斷|想|走)"),
    re.compile(r"我們(先)?(一起)?把.{0,12}(拆開|拆一拆|理一理|看一遍)"),
)

# 專家模式規定每一點都要附 `[1]`，那個數字不是內容。不先剝掉的話
# 「我陪你一起拆這個問題 [1]」會被 `NUMBER_PATTERN` 當成「有給數字」而放行，
# 整個守門對專家模式等於失效——而那正是它最該擋的一種空話。
# 半形與全形都要認。實際跑的時候 `normalize_citation_marks` 已經先正規化過，
# 但這個函式在測試與知識掃描裡是單獨被呼叫的，自己認得出來才不會有死角。
CITATION_MARK = re.compile(r"[【〔\[（(]\s*[0-9０-９]{1,2}\s*[】〕\]）)]")

# 結尾那幾行「▷ 建議問題」不是回答，是給前端做按鈕用的。它們也絕對不能算進
# 「這一則有沒有給東西」——那幾行天生就有動詞跟數字（「那我該先改哪一步？」
# 「我需要多少預算？」），整段一起送檢的話，
#
#     我陪你一起拆這個問題 [1]
#     ▷ 那我該先改哪一步？
#     ▷ 我需要多少預算？
#
# 會被判成沒問題；但服務把追問拆走之後，使用者收到的正文就只剩第一行那句空話，
# 狀態還是 answered。守門要驗的是「他最後真的看到的東西」。
# 樣式與 `service.FOLLOWUP_PATTERN` 一致（那邊負責真的拆，這邊只負責不要算進來）。
FOLLOWUP_LINE = re.compile(r"^[▷›>]\s*.+$")

# 有這些東西才算真的給了內容：數字、可執行的動作、或明確立場。
NUMBER_PATTERN = re.compile(r"\d")
# 「問」要排除「問題」：「我陪你拆這個問題」正是要抓的空話，不是動作。
ACTION_PATTERN = re.compile(
    r"打開|記下|寫下|傳|發|問(?!題)|拍|改|調|加|減|停|留|抽|算|排|約|回|貼|列|收|降|漲|練"
)
# 注意不要放單獨的「我會」：「我會陪你判斷」正是要抓的迴避句，不是表態。
STANCE_PATTERN = re.compile(
    r"我的傾向|我建議|我認為|會建議|不要|別|先做|可以先|應該|值得|不值得|"
    r"不適合|適合|還不到|還太早|夠了|可以了|撐得住|撐不住"
)

# 說了要給成品，就一定要在同一則裡給出來。
PROMISE_PATTERN = re.compile(
    r"(幫你|我)(寫|列|排|整理|想)(一版|一份|一個|給你)|我列給你|我排給你|我給你(結論|一版|範本)"
)

# 「三個」「十個」這種數量承諾，數得出來就要數。
COUNT_WORDS = {
    "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
# 只有「他真的在要東西」才算數量承諾。舊版抓任何「N 個／N 則」，於是
# 「健檢要抽哪 20 則對話來看」「私訊 30 則但沒人來」都被當成「要列 20 項」，
# 每一次都白白重打一次（體檢 B6：開場題庫 100 題中 3 題、問法種子 7 條）。
# 動詞一定要在數字前面，「這週來了 3 個客人」才不會被誤判。
REQUEST_COUNT_PATTERN = re.compile(
    r"(?:給我|給你|幫我|我要|列|寫|想|提供|做|生成|產出|舉)"
    r"[^。\n]{0,6}?([一二兩三四五六七八九十]|\d{1,2})\s*[個則句條項點組]"
)
# 超過這個數量就不是「列清單」而是在講事實（20 則對話、30 則私訊）。
MAX_REQUESTED_COUNT = 10

# 問到「量」的問句算給了東西：他答不出來也知道下一步要去查什麼。
# 「你想先從哪邊聊」這種沒有指向的問句不算。
CONCRETE_QUESTION = re.compile(r"多少|幾個|幾則|幾天|幾次|幾點|幾成|幾位|幾%|百分之")

# 問到立場就要表態，不能第三次複述對方的話。
STANCE_QUESTION = re.compile(
    r"你覺得.{0,12}(嗎|呢)|該不該|要不要|值不值得|我適合|準備好了嗎|撐(下去|得住)|時機對"
)

# TASK 4c：他沒提預算，回覆卻主動勸「先不要加預算」（使用者回報：「講到廣告
# 不好 總是會回先不要加預算 這非正相關」，實測「廣告沒訊息」的回答第一句就是
# 「先別加預算」）。來源知識裡的預算警語是寫給「正想加預算」或「詢問多但預約
# 少（問題在承接）」的情境的；prompt 勸不動——模型照「只能根據來源」把警語
# 一起抄出來——所以在這裡用程式擋。
# 問題側刻意放寬（提到花費、成本、投放、預約、到店都算有脈絡），寧可放過也不
# 誤殺：擋錯的代價是好答案被重打一次，放過的代價只是多一句囉唆。
UNSOLICITED_BUDGET_ADVICE = re.compile(
    r"(?:先別|先不要|不要急著|不用急著|別急著|先不用|不用|不需要|別)"
    r"(?:急著)?(?:增加|加|提高|放大)\s*預算"
)
BUDGET_CONTEXT = re.compile(r"預算|花|錢|費用|成本|加碼|投|預約|到店|來店|成交")

# 只擋「把人推給不存在的對象」這件事。**不要擋「主管」**：沙龍當然有主管，
# 客訴 SOP、請假、輪值、早會、離職流程整份 ops 知識都在講主管，擋掉等於把
# 照著知識回答的正確答案判成不合格（使用者指正：「會有主管的」）。
# 「專人」「公司現行」同理，知識庫本來就有正當用法，只擋轉接的講法。
FORBIDDEN_PATTERN = re.compile(r"轉人工|轉接專人|會有專人|專人(為你|跟你|與你|再跟你)")

# TASK 5a：被質疑就道歉、把原本正確的立場整個推翻。輔導最怕這個——設計師來
# 問就是要一個站得住的判斷，一被頂就縮回去等於沒有人在給意見。
# 只擋「純認錯」：道歉之後有給理由或重新表態的不算，模型本來就該修正真的講錯的地方。
PUSHBACK_PATTERN = re.compile(
    r"你(說|講)?錯|不對吧|不是這樣|才不是|真的嗎|你確定|確定嗎|我不同意|亂講|哪有|怎麼可能"
)
APOLOGY_PATTERN = re.compile(
    r"抱歉|不好意思|對不起|是我(說|講|寫|弄)錯|我錯了|我(剛剛|剛才)?(說|講)錯|更正"
)
REASON_PATTERN = re.compile(r"因為|原因|依據|根據|來源|理由|差別在|之所以|所以我|判斷是|實際上|其實")

# TASK 5b：同一則裡對同一件事又說可以又說不要，看的人不知道到底要做什麼。
# 抓法是「肯定詞＋兩個字」與「否定詞＋兩個字」撞在同一個詞上（建議漲價／不要漲價）。
# 取兩個字是刻意的：抓長一點會被後面的字岔開（「漲價 5-10%」vs「漲太多」本來就
# 是不同的話）。肯定詞前面要擋掉「不」「別」，否則「不建議漲價」自己會對上自己。
# 拿 278 塊知識全文跑過，零誤判；殘留的邊緣情況（「建議先觀察兩週／不要觀察太久」）
# 誤判的代價只是重打一次，第二次不合格照樣送原本那則。
POSITIVE_ADVICE = re.compile(r"(?<![不別])(?:建議|可以|應該|值得)(?:先)?([一-鿿]{2})")
NEGATIVE_ADVICE = re.compile(r"(?:不建議|不要|不應該|先不要|先別|別)(?:先)?([一-鿿]{2})")


def capitulated(question: str, answer: str) -> bool:
    """被頂了一句就道歉收回，而且沒給任何理由或新的立場。"""
    if not PUSHBACK_PATTERN.search(str(question or "")):
        return False
    if not APOLOGY_PATTERN.search(str(answer or "")):
        return False
    text = str(answer or "")
    return not (REASON_PATTERN.search(text) or STANCE_PATTERN.search(text))


def contradictions(answer: str) -> set[str]:
    """同一則裡又叫人做又叫人不要做的那件事。"""
    text = str(answer or "")
    return set(POSITIVE_ADVICE.findall(text)) & set(NEGATIVE_ADVICE.findall(text))


def _requested_count(question: str) -> int:
    """他要幾個？只有明確在要東西（給我十個 hashtag）才算。"""
    match = REQUEST_COUNT_PATTERN.search(str(question or ""))
    if not match:
        return 0
    value = match.group(1)
    wanted = COUNT_WORDS.get(value, 0) if value in COUNT_WORDS else int(value)
    return wanted if wanted <= MAX_REQUESTED_COUNT else 0


def _delivered_count(answer: str) -> int:
    """回覆裡數得出幾個項目：換行、頓號、編號都算。"""
    text = str(answer or "").strip()
    if not text:
        return 0
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) > 1:
        return len(lines)
    return len([part for part in re.split(r"[、,，]", text) if part.strip()])


# 一行以問號、問句助詞或波浪號收尾就是在問他，不是在給他東西。
QUESTION_LINE = re.compile(r"(?:[?？～~]|嗎|呢|好不好|可以嗎)\s*$")


def _deliverable_body(text: str) -> str:
    """扣掉純問句的行之後，還剩下多少真的寫給他的內容。"""
    lines = [line.strip() for line in str(text or "").split("\n")]
    return "".join(
        line for line in lines if line and not QUESTION_LINE.search(line)
    ).strip()


def has_substance(answer: str) -> bool:
    """這一則到底有沒有給東西：數字、動作、明確立場或一個具體的問題。

    「問一件具體的事」也算內容：客服模式的規則要求第一輪先接住再問一件事
    （「我幫你看一下／你這檔廣告花了多少 有幾個人私訊」），照做卻被判成
    「只寫了我幫你看」是規則跟守門互相打架（體檢 B7）。
    只認問到「量」的問句——「你想先從哪邊聊」那種空問句照樣要擋。
    """
    text = str(answer or "")
    return bool(
        NUMBER_PATTERN.search(text)
        or ACTION_PATTERN.search(text)
        or STANCE_PATTERN.search(text)
        or CONCRETE_QUESTION.search(text)
    )


# 一則訊息裡連寫這麼多字又完全沒換行，畫面上就是一坨。前端與 LINE 出口都會
# 自己斷行（`humanize.wrap_line`），但那只是把一坨排整齊，救不了「話太多」。
WALL_CHARS = 40


def wall_of_text(answer: str) -> bool:
    """有沒有哪一則是「連寫一長串又一個換行都沒有」。

    只針對聊天語氣。已經自己分行的不算——那是排版好的內容，不是一坨。
    """
    for block in re.split(r"\n[ \t]*\n+", str(answer or "")):
        block = block.strip()
        if "\n" in block:
            continue
        if len(re.sub(r"\s", "", block)) > WALL_CHARS:
            return True
    return False


def _answer_body(answer: str) -> str:
    """把結尾那幾行「▷ 建議問題」拿掉，剩下的才是回答本身。"""
    lines = str(answer or "").rstrip().splitlines()
    while lines:
        line = lines[-1].strip()
        if not line or FOLLOWUP_LINE.match(line):
            lines.pop()
        else:
            break
    return "\n".join(lines).rstrip()


def problems(question: str, answer: str, tone: str = "") -> list[str]:
    """回傳這一則回覆的問題清單；空清單＝可以送出去。

    `tone` 是聊天語氣（service／line）時會多檢查長度：那兩種是通訊軟體的
    短句，專家模式本來就該講完講透，不套這條。
    """
    # 先還原成「使用者最後真的會看到的正文」再判斷：引用編號是格式不是內容
    # （見 CITATION_MARK），結尾的 ▷ 建議問題會被拆去做按鈕（見 FOLLOWUP_LINE）。
    text = CITATION_MARK.sub("", _answer_body(answer)).strip()
    if not text:
        return []
    found: list[str] = []

    # TASK 1：延後回答的句型單獨出現。
    if any(pattern.search(text) for pattern in DELAY_PATTERNS) and not has_substance(text):
        found.append(
            "這則只寫了「我陪你／我幫你看」卻沒有給任何判斷、數字或動作。"
            "把你想拆的那件事直接講出來。"
        )

    # TASK 2：承諾了成品卻沒交付。把承諾句本身扣掉，看剩下有沒有東西——
    # 用整則長度判斷會誤殺 LINE 那種很短但真的有給的回覆。
    promise = PROMISE_PATTERN.search(text)
    if promise:
        rest = text[: promise.start()] + text[promise.end() :]
        # 只剩一句「你想要親切一點 還是專業一點～」不算交付——那是又把球丟回去。
        # 所以把純問句的行扣掉之後再數字數。
        delivered = len(_deliverable_body(rest)) >= 12
    else:
        delivered = True
    if promise and not delivered:
        found.append(
            "你說了要幫他寫／列／排一版，但這則沒有把成品寫出來。"
            "同一則就要給完整內容，給不出來就不要做這個承諾。"
        )

    # TASK 2：數量承諾要數得出來。
    wanted = _requested_count(question)
    if wanted >= 3 and _delivered_count(text) < wanted:
        found.append(f"他要 {wanted} 個，你給的不到 {wanted} 個。要列滿。")

    # TASK 3：問到立場就要表態。
    if STANCE_QUESTION.search(str(question or "")) and not STANCE_PATTERN.search(text):
        found.append(
            "他直接問你的判斷，這則沒有表態。"
            "第一句就講「我的傾向是…」，再用他給過的數字說明理由。"
        )

    # 聊天語氣才檢查長度：一則連寫 40 字以上又不換行，收到的人只會滑過去。
    if str(tone or "") in ("service", "line") and wall_of_text(text):
        found.append(
            "有一則連寫了一長串又沒有換行。規則是每行 12 字、一則最多 2 行、"
            "最多 3 則——講不完就只講最關鍵的那一件，其餘等他問再說。"
        )

    # TASK 5a：一被質疑就認錯收回。
    if capitulated(question, text):
        found.append(
            "他只是質疑了一句，你就道歉把原本的判斷收回去，而且沒說為什麼。"
            "先講你原本的依據是什麼，真的講錯就說清楚錯在哪裡、正確的是什麼；"
            "沒講錯就把立場守住。"
        )

    # TASK 5b：同一則自相矛盾。
    conflict = contradictions(text)
    if conflict:
        topic = "、".join(sorted(conflict))
        found.append(
            f"同一則裡對「{topic}」又說可以又說不要，看的人不知道到底要做什麼。"
            "選一個立場講清楚，有前提就把前提寫出來。"
        )

    # TASK 4c：他沒提預算，回覆卻主動勸「先不要加預算」。
    if UNSOLICITED_BUDGET_ADVICE.search(text) and not BUDGET_CONTEXT.search(str(question or "")):
        found.append(
            "他沒提到預算，你卻叫他「先不要加預算」——答非所問。"
            "來源裡那句是寫給正想加預算、或詢問多但預約少的人看的，"
            "把它拿掉，直接回答他問的那件事。"
        )

    # TASK 4b：把人推給不存在的對象。
    punt = FORBIDDEN_PATTERN.search(text)
    if punt:
        found.append(
            f"不要說「{punt.group(0)}」：這裡沒有人工客服也沒有專人可以接手，"
            "你就是那個要給答案的人。自己給技術面判斷與溝通做法。"
        )
    return found


def retry_note(found: list[str]) -> str:
    """把問題寫成給模型的重試提示。"""
    if not found:
        return ""
    lines = "\n".join(f"- {item}" for item in found)
    return (
        "\n\n注意：你上一次的回答被品質檢查擋下來了，原因："
        f"\n{lines}\n"
        "這一次直接把具體內容寫出來，長度規則照舊。"
    )
