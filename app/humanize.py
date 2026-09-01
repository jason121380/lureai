"""真人模擬：把大腦產出的回答變成「可以直接送進 LINE 的幾則短訊息」。

原本住在 lurebot（Flask 端）；大腦搬到這裡之後，語氣、長短、去標點、拆則
與回覆停頓一併移過來，lurebot 只負責等待秒數與送出。
"""

import random
import re


# 回覆停頓（秒）。LINE 的 reply token 只有 60 秒，扣掉生成時間後
# 上限抓 30 秒；lurebot 端還會再夾一次剩餘效期。
DELAYS = {
    "none": (0, 0),
    "short": (4, 12),
    "natural": (8, 25),
    "slow": (15, 30),
}

LENGTHS = {
    "short": "1-2 句",
    "medium": "2-4 句",
    "long": "4-6 句",
}

# 總字數硬上限，會寫進提示詞。
LENGTH_CAPS = {"short": 40, "medium": 80, "long": 130}

TONES = {
    "natural": "自然口語、像朋友聊天",
    "lively": "活潑熱情，多一點語助詞和表情符號",
    "calm": "沉穩專業、給人可靠的感覺",
    "humor": "幽默輕鬆，偶爾開個小玩笑",
}

# LINE 一次最多送幾則。
MAX_PARTS = 3

MAX_EXTRA_PROMPT = 1000

DEFAULT_STYLE = {
    "delay": "natural",
    "length": "short",
    "tone": "natural",
    "no_punct": True,
    "split_long": True,
    "extra_prompt": "",
}

STYLE_KEY = "bot_style"

CITATION_PATTERN = re.compile(r"\s*\[\d{1,2}\]")
PUNCTUATION_PATTERN = re.compile(r"[，。、；：！？,.;:!?]+")
SPLIT_CHARS = " ，。、；：！？,.;:!?"


def normalize_style(raw, base: dict | None = None) -> dict:
    """把後台或 lurebot 傳來的設定收斂成合法值；未知欄位一律回落預設。"""
    style = dict(base or DEFAULT_STYLE)
    if not isinstance(raw, dict):
        return style
    if raw.get("delay") in DELAYS:
        style["delay"] = raw["delay"]
    if raw.get("length") in LENGTHS:
        style["length"] = raw["length"]
    if raw.get("tone") in TONES:
        style["tone"] = raw["tone"]
    if "no_punct" in raw:
        style["no_punct"] = bool(raw["no_punct"])
    if "split_long" in raw:
        style["split_long"] = bool(raw["split_long"])
    if "extra_prompt" in raw:
        style["extra_prompt"] = str(raw["extra_prompt"] or "")[:MAX_EXTRA_PROMPT].strip()
    return style


def _context_lines(context) -> list[str]:
    """群組脈絡由 lurebot 提供（群組名、發話者、輔導階段），只當背景資訊。"""
    if not isinstance(context, dict):
        return []
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
    return lines


def style_instruction(style: dict, context=None) -> str:
    """依設定產生附加指示，接在 line 語氣後面送給模型。"""
    length = LENGTHS.get(style.get("length"), LENGTHS["short"])
    cap = LENGTH_CAPS.get(style.get("length"), LENGTH_CAPS["short"])
    tone = TONES.get(style.get("tone"), TONES["natural"])
    parts = ["\n\n## 這次回覆的個別設定（覆蓋前面的長度與語氣）"]
    parts.extend(_context_lines(context))
    parts.append(f"語氣{tone}。")
    parts.append(f"長度以 {length} 為主，全部加起來不超過 {cap} 個字。")
    if style.get("split_long"):
        parts.append(
            f"如果要講不只一句，用換行分成最多 {MAX_PARTS} 則短訊息，"
            "每行是一句完整的話（每行會單獨送出）。"
        )
    else:
        parts.append("只回一則訊息，不要換行。")
    if style.get("no_punct"):
        parts.append("不要使用任何標點符號，語句之間用空白分隔，像平常打 LINE 那樣。")
    extra = str(style.get("extra_prompt", "")).strip()
    if extra:
        parts.append(f"管理者的額外指示（優先遵守）：{extra}")
    return "\n".join(parts)


def strip_citations(text: str) -> str:
    """引用編號只給守門與稽核用，送進 LINE 前拿掉（來源另外回在 citations）。"""
    cleaned = CITATION_PATTERN.sub("", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def postprocess(reply_text: str, style: dict) -> list[str]:
    """去標點（空白分段）、依模型換行分則（備援對半切）。回傳訊息列表。"""
    text = strip_citations(reply_text)
    if style.get("no_punct"):
        text = PUNCTUATION_PATTERN.sub(" ", text)
    # 保留換行（模型用換行分則），只收斂行內空白。
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = text.strip()
    if not text:
        return []
    if not style.get("split_long"):
        return [re.sub(r"\s*\n+\s*", " ", text)]
    parts = [part.strip() for part in text.split("\n") if part.strip()]
    if len(parts) >= 2:
        return parts[:MAX_PARTS]
    single = parts[0]
    if len(single) <= 10:
        return [single]
    # 備援：模型沒分行的長句，在最接近中間的空白／標點處切成兩句。
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


def reply_delay(style: dict, rng: random.Random | None = None) -> float:
    """回覆停頓秒數；lurebot 收到後直接 sleep 這麼久再送出。"""
    low, high = DELAYS.get(style.get("delay"), DELAYS["natural"])
    if high <= 0:
        return 0.0
    return round((rng or random).uniform(low, high), 1)
