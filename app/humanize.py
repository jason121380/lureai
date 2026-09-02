"""真人模擬：把大腦產出的回答變成「可以直接送進 LINE 的幾則短訊息」。

沒有可調參數——回覆的語氣、長短與斷句規則寫在 `line` 語氣（`app/answer.py`）裡，
這裡只負責送出前的固定動作：剝掉引用編號、去標點、拆則、給一個像真人的停頓秒數。
lurebot 收到之後就是等秒數、照順序送出。
"""

import random
import re


# 回覆停頓（秒）。LINE 的 reply token 只有 60 秒，扣掉生成時間後上限抓 25 秒；
# lurebot 端還會再夾一次剩餘效期。
DELAY_RANGE = (8, 25)

# 同一次回覆裡，每一則之間再等幾秒才送下一則——真人不會三則同時跳出來。
# **至少 3 秒**（使用者指定）：低於 3 秒看起來還是像機器一次倒三則，
# 收訊的人根本來不及讀完上一則。
MESSAGE_GAP_RANGE = (3, 5)

# LINE 一次最多送幾則（和 line 語氣裡寫的規則一致）。
MAX_PARTS = 3

# 一則最多幾行；模型忘了空行時這裡自己重排。
MAX_LINES_PER_PART = 2

# 一行最多幾個字。跟 `line`／`service` 語氣裡寫的 12 字規則一致。
# 只數行數是擋不住的：模型常常回一整行 120 字、一個換行都沒有，
# 行數檢查看到「1 行」就直接放行，畫面上就是一大坨。
MAX_CHARS_PER_LINE = 12

CITATION_PATTERN = re.compile(r"\s*\[\d{1,2}\]")
# 半形的「.」與「:」只有在不是夾在數字中間時才是標點。整組剝掉的話
# 「8.5%」會變成「8 5%」、「10:30」會變成「10 30」，數字直接講錯（體檢 B5）。
# 「！」不剝：規則明文說「！」和「～」可以用（放在給安心感的短句），
# 剝掉等於程式在推翻自己寫給模型的規則。
PUNCTUATION_PATTERN = re.compile(r"[，。、；：？,;?]+|(?<!\d)[.:]|[.:](?!\d)")
SPLIT_CHARS = " ，。、；：！？,.;:!?"
# 前端 `cleanChatLine` 會把條列記號與引號拿掉，這裡也要拿掉，兩邊才會一致
# （體檢 B10：同一段文字兩邊拆出來的結果 10 個樣本有 5 個不同）。
LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")
QUOTE_CHARS = "「」『』（）()"


def context_instruction(context) -> str:
    """群組脈絡由 lurebot 提供（群組名、發話者、輔導階段、最近對話），只當背景資訊。"""
    if not isinstance(context, dict):
        return ""
    lines = []
    group = " ".join(str(context.get("group_name", "")).split())[:60]
    speaker = " ".join(str(context.get("speaker", "")).split())[:40]
    stage = " ".join(str(context.get("stage", "")).split())[:60]
    summary = " ".join(str(context.get("summary", "")).split())[:400]
    if group:
        lines.append(f"你正在 LINE 群組「{group}」裡回覆。")
    if speaker:
        lines.append(f"現在跟你說話的設計師是「{speaker}」。")
    if stage:
        lines.append(f"這個群組目前的輔導階段：{stage}。")
    if summary:
        lines.append(f"這個群組的近況摘要：{summary}")
    recent = context.get("recent")
    if isinstance(recent, list) and recent:
        transcript = [
            " ".join(str(item).split())[:200] for item in recent[-20:] if str(item).strip()
        ]
        if transcript:
            lines.append("最近的群組對話（由舊到新，僅供理解脈絡）：\n" + "\n".join(transcript))
    if not lines:
        return ""
    return "\n\n## 這一則的群組脈絡\n" + "\n".join(lines)


# 兩個字以內的一段不會是獨立的句子，它是後面那段的一部分（「抓 20 則來看」被
# 空白切成「抓」「20」「則來看」）。不先黏回去的話，斷行會落在「20」跟「則」中間。
GLUE_BELOW = 3


def _glue_fragments(segments: list[str]) -> list[str]:
    glued: list[str] = []
    pending = ""
    for segment in segments:
        pending = f"{pending} {segment}" if pending else segment
        if len(segment) >= GLUE_BELOW:
            glued.append(pending)
            pending = ""
    if pending:
        if glued:
            glued[-1] = f"{glued[-1]} {pending}"
        else:
            glued.append(pending)
    return glued


def wrap_line(line: str, cap: int = MAX_CHARS_PER_LINE) -> list[str]:
    """把過長的一行斷成每行 cap 字以內。

    標點已經被拿掉，空白就是模型唯一的斷句記號，所以照空白切、再把短的併回去，
    才不會切在詞的中間。單一段本身就超過 cap 時（沒有空白可切）才硬切，
    而且要超過兩倍才硬切——寧可有一行 13 字，也不要把詞切斷。

    **字數只算內容、不算空白**：空白是分隔符不是字。把空白算進去的話，
    「先看回覆率 這週抓 20 則」這種一行 11 個字的正常句子會被拆成兩行。
    """
    segments = _glue_fragments([seg for seg in str(line or "").split(" ") if seg])
    if not segments:
        return []
    lines: list[str] = []
    current = ""
    for segment in segments:
        while len(segment) > cap * 2:
            if current:
                lines.append(current)
                current = ""
            lines.append(segment[:cap])
            segment = segment[cap:]
        if not current:
            current = segment
        elif len(current.replace(" ", "")) + len(segment.replace(" ", "")) <= cap:
            current = f"{current} {segment}"
        else:
            lines.append(current)
            current = segment
    if current:
        lines.append(current)
    return lines


def strip_citations(text: str) -> str:
    """引用編號只給稽核核對用，送進 LINE 前拿掉（來源另外回在 citations）。"""
    cleaned = CITATION_PATTERN.sub("", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def postprocess(reply_text: str) -> list[str]:
    """去引用、去標點（空白分段）、依空行分則（備援對半切）。回傳訊息列表。

    一則訊息裡面可以有好幾行（LINE 也支援換行），所以**空一行才代表換一則**，
    單純換行只是同一則裡的下一行。
    """
    text = strip_citations(reply_text)
    # 條列記號與引號先拿掉（跟前端 `cleanChatLine` 同一套），再處理標點。
    text = "\n".join(
        LIST_MARKER.sub("", line).translate({ord(char): " " for char in QUOTE_CHARS})
        for line in text.split("\n")
    )
    text = PUNCTUATION_PATTERN.sub(" ", text)
    # 保留換行（模型用空行分則），只收斂行內空白。
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = text.strip()
    if not text:
        return []
    parts = [
        "\n".join(line for line in block.split("\n") if line.strip()).strip()
        for block in re.split(r"\n[ \t]*\n+", text)
    ]
    parts = [part for part in parts if part]
    # 模型忘了空行時，一則會塞進七八行——先重排成每則最多 2 行。
    reflowed = []
    for part in parts:
        # 先把過長的行斷開，再數行數。順序反了就擋不住「一行 120 字」。
        lines = [wrapped for line in part.split("\n") for wrapped in wrap_line(line)]
        for index in range(0, len(lines), MAX_LINES_PER_PART):
            reflowed.append("\n".join(lines[index:index + MAX_LINES_PER_PART]))
    parts = reflowed
    if len(parts) >= 2:
        if len(parts) <= MAX_PARTS:
            return parts
        # 超過上限就把中間併成一則，不要直接砍掉尾巴——砍掉會連收尾的問句
        # 和範例正文一起消失，設計師收到的就是一段沒講完的話。
        # 併起來時用換行接，不要接成一長條。
        return [*parts[:MAX_PARTS - 2], "\n".join(parts[MAX_PARTS - 2:-1]), parts[-1]]
    single = parts[0]
    # 上面的 wrap_line 已經保證每行不超過 12 字，所以剩下的單則要嘛本來就短、
    # 要嘛是一個切不開的長詞。舊版只要超過 10 字就對半硬切，於是
    # 「你這個月私訊大概幾則呢～」被切成兩則、中間還隔 3~5 秒送出——收到的人
    # 會以為對方打到一半按了送出（體檢 B4）。
    # 現在只有「真的長到一則裝不下」（超過兩行的量）才切，而且一定切在空白處。
    if "\n" in single or len(single.replace(" ", "")) <= MAX_CHARS_PER_LINE * 2:
        return [single]
    mid = len(single) // 2
    best = -1
    for index, char in enumerate(single):
        if char == " " and (best == -1 or abs(index - mid) < abs(best - mid)):
            best = index
    if best <= 0 or best >= len(single) - 1:
        # 沒有空白可切就不要硬切在詞中間，整句送出去比切壞好。
        return [single]
    head = single[:best].strip(SPLIT_CHARS)
    tail = single[best:].strip(SPLIT_CHARS)
    return [part for part in (head, tail) if part] or [single]


def message_gaps(count: int, rng: random.Random | None = None,
                 gap_range: tuple[float, float] | None = None) -> list[float]:
    """每一則送出前要等幾秒：第一則用 reply_delay，後面幾則各等一小段。

    lurebot 照這個列表依序 sleep 再送，訊息才會像真人一則一則打出來，
    而不是三則同時跳出來。
    """
    low, high = gap_range or MESSAGE_GAP_RANGE
    picker = rng or random
    if count <= 1:
        return []
    if high <= low:
        return [round(max(0.0, low), 1)] * (count - 1)
    return [round(picker.uniform(low, high), 1) for _ in range(count - 1)]


def reply_delay(rng: random.Random | None = None,
                delay_range: tuple[float, float] | None = None) -> float:
    """回覆停頓秒數；lurebot 收到後直接 sleep 這麼久再送出，像真人打字。

    區間可在後台「AI 模型校調 → LINE 出口設定」改，沒設定就用 DELAY_RANGE。
    """
    low, high = delay_range or DELAY_RANGE
    if high <= low:
        return round(max(0.0, low), 1)
    return round((rng or random).uniform(low, high), 1)
