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

# LINE 一次最多送幾則（和 line 語氣裡寫的規則一致）。
MAX_PARTS = 3

CITATION_PATTERN = re.compile(r"\s*\[\d{1,2}\]")
PUNCTUATION_PATTERN = re.compile(r"[，。、；：！？,.;:!?]+")
SPLIT_CHARS = " ，。、；：！？,.;:!?"


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


def strip_citations(text: str) -> str:
    """引用編號只給稽核核對用，送進 LINE 前拿掉（來源另外回在 citations）。"""
    cleaned = CITATION_PATTERN.sub("", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def postprocess(reply_text: str) -> list[str]:
    """去引用、去標點（空白分段）、依模型換行分則（備援對半切）。回傳訊息列表。"""
    text = PUNCTUATION_PATTERN.sub(" ", strip_citations(reply_text))
    # 保留換行（模型用換行分則），只收斂行內空白。
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = text.strip()
    if not text:
        return []
    parts = [part.strip() for part in text.split("\n") if part.strip()]
    if len(parts) >= 2:
        if len(parts) <= MAX_PARTS:
            return parts
        # 超過上限就把中間併成一則，不要直接砍掉尾巴——砍掉會連收尾的問句
        # 和範例正文一起消失，設計師收到的就是一段沒講完的話。
        return [parts[0], " ".join(parts[1:-1]), parts[-1]]
    single = parts[0]
    if len(single) <= 10:
        return [single]
    # 備援：模型沒分行的長句，在最接近中間的空白處切成兩句。
    mid = len(single) // 2
    best = -1
    for index, char in enumerate(single):
        if char in SPLIT_CHARS and (best == -1 or abs(index - mid) < abs(best - mid)):
            best = index
    if best <= 0 or best >= len(single) - 1:
        best = mid
    head = single[:best].strip(SPLIT_CHARS)
    tail = single[best:].strip(SPLIT_CHARS)
    return [part for part in (head, tail) if part] or [single]


def reply_delay(rng: random.Random | None = None,
                delay_range: tuple[float, float] | None = None) -> float:
    """回覆停頓秒數；lurebot 收到後直接 sleep 這麼久再送出，像真人打字。

    區間可在後台「AI 模型校調 → LINE 出口設定」改，沒設定就用 DELAY_RANGE。
    """
    low, high = delay_range or DELAY_RANGE
    if high <= low:
        return round(max(0.0, low), 1)
    return round((rng or random).uniform(low, high), 1)
