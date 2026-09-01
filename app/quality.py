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
COUNT_PATTERN = re.compile(r"([一二兩三四五六七八九十]|\d+)\s*(個|則|句|條|項|點|組)")

# 問到立場就要表態，不能第三次複述對方的話。
STANCE_QUESTION = re.compile(
    r"你覺得.{0,12}(嗎|呢)|該不該|要不要|值不值得|我適合|準備好了嗎|撐(下去|得住)|時機對"
)

# 只擋「把人推給不存在的對象」這件事。**不要擋「主管」**：沙龍當然有主管，
# 客訴 SOP、請假、輪值、早會、離職流程整份 ops 知識都在講主管，擋掉等於把
# 照著知識回答的正確答案判成不合格（使用者指正：「會有主管的」）。
# 「專人」「公司現行」同理，知識庫本來就有正當用法，只擋轉接的講法。
FORBIDDEN_PATTERN = re.compile(r"轉人工|轉接專人|會有專人|專人(為你|跟你|與你|再跟你)")


def _requested_count(question: str) -> int:
    """他要幾個？問句裡的數量詞（十個 hashtag）。"""
    match = COUNT_PATTERN.search(str(question or ""))
    if not match:
        return 0
    value = match.group(1)
    return COUNT_WORDS.get(value, 0) if value in COUNT_WORDS else int(value)


def _delivered_count(answer: str) -> int:
    """回覆裡數得出幾個項目：換行、頓號、編號都算。"""
    text = str(answer or "").strip()
    if not text:
        return 0
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) > 1:
        return len(lines)
    return len([part for part in re.split(r"[、,，]", text) if part.strip()])


def has_substance(answer: str) -> bool:
    """這一則到底有沒有給東西：數字、動作、或明確立場任一。"""
    text = str(answer or "")
    return bool(
        NUMBER_PATTERN.search(text)
        or ACTION_PATTERN.search(text)
        or STANCE_PATTERN.search(text)
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


def problems(question: str, answer: str, tone: str = "") -> list[str]:
    """回傳這一則回覆的問題清單；空清單＝可以送出去。

    `tone` 是聊天語氣（service／line）時會多檢查長度：那兩種是通訊軟體的
    短句，專家模式本來就該講完講透，不套這條。
    """
    text = str(answer or "").strip()
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
        rest = (text[: promise.start()] + text[promise.end() :]).replace("\n", "").strip()
        delivered = len(rest) >= 12
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
