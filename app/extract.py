"""把上傳的文件切成「可以被引用的知識」。

後台的「新增知識」改成拖檔進來之後，這裡負責把一份原始文件變成幾塊
候選知識（標題／分類／主題／內容），交給人在畫面上確認、修改，按儲存才寫進去。
**檔案本身不留**，只留萃取出來的知識。

兩條路徑：
- 有模型時交給模型重寫成完整句子（`propose_chunks`）。
- 沒有模型或模型失敗時走 `split_document` 的規則切法，一樣給得出東西——
  這是後台工具，寧可給一版讓人改，也不要跳錯誤要人自己貼。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .answer import extract_usage, mask_contacts
from .domains import COACHING, OPERATIONS, classify


# 一塊知識的目標長度。下限跟 `curation.chunk_issues` 的「內容過短」同一條線
# （60 字），低於它的段落往前併，不然存進去馬上被後台標成待整理。
# 上限是怕一塊塞好幾個主題，檢索時分數會被稀釋。
MIN_CHUNK_CHARS = 60
MAX_CHUNK_CHARS = 1200
# 一份文件最多切幾塊：畫面上要一塊一塊確認，超過這個數量沒有人看得完。
MAX_CHUNKS = 30

# 一塊候選知識至少要有這麼多「字」（中日韓或拉丁字母）才算得上文句。
# 日曆、純數字表格抓出來全是數字與符號，硬切成知識只會塞垃圾進索引。
MIN_WORD_CHARS = 20
WORD_CHARS = re.compile(r"[一-鿿　-〿0-9A-Za-z]")
LETTERS = re.compile(r"[一-鿿A-Za-z]")

HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
# 「1. 標題」「一、標題」這種也算段落標題（Word 貼過來常見）。
NUMBERED = re.compile(r"^\s{0,3}(?:\d{1,2}[.)]|第?[一二三四五六七八九十]{1,3}[、.])\s*(\S.{0,40})$")
BULLET = re.compile(r"^\s*[-*•]\s+")


# 網址不是文句。一行 https://www.taiwan-marketing.com/slides2/22 就有三十幾個
# 拉丁字母，光數字母的話，只抓到列印頁尾的 PDF 也會被當成有內容。
URL = re.compile(r"https?://\S+|www\.\S+")


def has_prose(text: str) -> bool:
    """這段裡面有沒有文句。數字不算（日曆整份都是數字），網址也不算。"""
    return len(LETTERS.findall(URL.sub("", str(text or "")))) >= MIN_WORD_CHARS


def _clean(text: str) -> str:
    """收斂空白，保留換行結構。"""
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in str(text or "").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _title_from(body: str, fallback: str) -> str:
    """沒有標題時，用第一句當標題。"""
    first = ""
    for line in body.splitlines():
        stripped = BULLET.sub("", line.strip())
        if stripped:
            first = stripped
            break
    first = re.split(r"[。！？!?\n]", first)[0].strip("「」『』：: ")
    return (first or fallback)[:40]


def split_document(name: str, text: str) -> list[dict]:
    """規則切法：先照標題切，沒有標題就照空行併成段。

    這條路不呼叫模型，所以一定給得出結果——沒有 API key 的環境（本機、
    預算用完）後台也還是能用。
    """
    body = _clean(text)
    if not body:
        return []
    base = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", str(name or "")).strip() or "上傳的文件"

    sections: list[dict] = []
    current = {"title": "", "lines": []}

    def flush() -> None:
        content = _clean("\n".join(current["lines"]))
        if content:
            sections.append({"title": current["title"], "text": content})

    for line in body.splitlines():
        heading = HEADING.match(line)
        numbered = None if heading else NUMBERED.match(line)
        if heading or numbered:
            flush()
            current = {"title": (heading.group(2) if heading else numbered.group(1)).strip(), "lines": []}
            continue
        current["lines"].append(line)
    flush()

    # 沒有任何標題時，照空行分段再併到目標長度。
    if len(sections) <= 1 and (not sections or not sections[0]["title"]):
        blocks = [block for block in re.split(r"\n\s*\n", body) if block.strip()]
        sections = []
        buffer: list[str] = []
        for block in blocks:
            buffer.append(block.strip())
            if len("\n\n".join(buffer)) >= MIN_CHUNK_CHARS * 4:
                sections.append({"title": "", "text": "\n\n".join(buffer)})
                buffer = []
        if buffer:
            sections.append({"title": "", "text": "\n\n".join(buffer)})

    # 太短的往前併，太長的照空行切開——兩邊都會讓知識變得難用。
    merged: list[dict] = []
    for section in sections:
        if merged and len(section["text"]) < MIN_CHUNK_CHARS:
            merged[-1]["text"] = f"{merged[-1]['text']}\n\n{section['title']}\n{section['text']}".strip()
            continue
        merged.append(dict(section))
    proposals: list[dict] = []
    for section in merged:
        pieces = [section["text"]]
        while pieces and len(pieces[0]) > MAX_CHUNK_CHARS:
            head = pieces.pop(0)
            cut = head.rfind("\n\n", 0, MAX_CHUNK_CHARS)
            if cut <= 0:
                cut = MAX_CHUNK_CHARS
            pieces = [head[:cut].strip(), head[cut:].strip()] + pieces
        for index, piece in enumerate(pieces):
            if not piece:
                continue
            title = section["title"] or _title_from(piece, base)
            if index:
                title = f"{title}（續）"
            if not has_prose(piece):
                continue  # 日曆／純數字表格：切出來也不是知識
            proposals.append({
                "section_title": title[:80],
                "category": base[:20],
                "domain": "",
                "text": piece[:MAX_CHUNK_CHARS],
            })
            if len(proposals) >= MAX_CHUNKS:
                return proposals
    return proposals


INSTRUCTION = (
    "你在整理一份美髮沙龍的內部教材，要把它變成 AI 助理可以引用的知識庫條目。\n"
    "規則：\n"
    "1. 用完整句子的繁體中文重寫，不要照抄破碎的條列或表格傾印。\n"
    "2. 一條只講一個主題，內容 80 到 800 字，包含判斷標準、步驟或話術範例。\n"
    "3. 標題寫成看得懂在講什麼的短句，20 字以內，不要用「第一章」這種編號。\n"
    "4. 分類用 2 到 6 個字的名詞（例如 售後與回流、私訊流程、店務營運）。\n"
    "5. domain 只能是 operations（店務營運管理）或 coaching（設計師一對一行銷輔導）。\n"
    "6. 原文沒寫的事不要自己補，尤其是數字。\n"
    f"7. 最多輸出 {MAX_CHUNKS} 條。\n\n"
    '只輸出 JSON：{"items":[{"section_title":"","category":"","domain":"","text":""}]}'
)


def _parse_items(text: str) -> list[dict]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    cleaned: list[dict] = []
    for item in items[:MAX_CHUNKS]:
        if not isinstance(item, dict):
            continue
        body = str(item.get("text", "")).strip()
        title = " ".join(str(item.get("section_title", "")).split())
        if not body or not title or not has_prose(body):
            continue
        domain = " ".join(str(item.get("domain", "")).split())
        category = " ".join(str(item.get("category", "")).split())[:20]
        cleaned.append({
            "section_title": title[:80],
            "category": category,
            "domain": domain if domain in (OPERATIONS, COACHING) else classify(category),
            "text": body[:MAX_CHUNK_CHARS],
        })
    return cleaned


EMPTY_USAGE = {
    "input_tokens": 0, "cached_input_tokens": 0,
    "cache_write_input_tokens": 0, "output_tokens": 0,
}


def propose_chunks(
    answerer, name: str, text: str, allow_model: bool = True,
) -> tuple[list[dict], str, dict]:
    """回傳（候選知識, 用了哪條路徑, 這次的用量）。模型不通就用規則切法，不讓後台開天窗。

    用量一定要回傳並記帳：這條路一次送兩萬多字進模型，是整個系統單次最貴的
    呼叫，卻曾經完全不進帳本——後台看到的月花費會比實際少。
    """
    fallback = split_document(name, text)
    if not (allow_model and getattr(answerer, "model_enabled", False)):
        return fallback, "rules", dict(EMPTY_USAGE)
    # 上傳的文件裡可能夾著客人的電話與 Email，而萃取出來的東西會存進知識庫。
    # 走跟聊天同一層遮罩（`answer.mask_contacts`）。
    body_text = mask_contacts(str(text)[:24000])
    payload = {
        "model": os.environ["LLM_MODEL"],
        "instructions": INSTRUCTION,
        "input": [{"role": "user", "content": f"檔名：{name}\n\n內容：\n{body_text}"}],
        "reasoning": {"effort": "low"},
        "max_output_tokens": 16000,
        "store": False,
    }
    request = urllib.request.Request(
        f"{os.environ['LLM_BASE_URL'].rstrip('/')}/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(getattr(answerer, "timeout", 60.0), 60.0)) as response:
            body = json.loads(response.read())
    except (OSError, ValueError, KeyError, urllib.error.URLError, TimeoutError):
        return fallback, "rules", dict(EMPTY_USAGE)
    usage = extract_usage(body)
    output = body.get("output_text")
    if not isinstance(output, str):
        output = ""
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    output = content["text"]
                    break
    items = _parse_items(output)
    # 模型有回但解析不出東西時仍然要記帳——錢已經花掉了。
    return (items, "model", usage) if items else (fallback, "rules", usage)
