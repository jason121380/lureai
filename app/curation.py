"""Knowledge quality checks powering the admin curation views.

The assistant can only answer as well as its sources read, so these checks
surface chunks that are fragments, mostly redaction markers, or carry a
meaningless title (an OCR'd slide filename, say).
"""

import re


MASK_PATTERN = re.compile(r"\[[^\]]{1,10}\]")
# 引用區塊（`> ...`）是可以直接複製去傳的逐字話術，**本來就該短**（規則是一行
# 12 字）。這些檢查是為了抓 OCR 碎片寫的，拿短句當「零碎」會把寫得最好的
# 話術範本全部誤判成待整理。判斷散文比例時先把逐字稿拿掉，只看說明的部分。
QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
BANNER_PATTERN = re.compile(r"【[^】]*】")
MEANINGLESS_TITLE = re.compile(r"^[\s\d\W_]*$|^(?:投影片|工作表|投)[\s\d\W]*$")

ISSUE_LABELS = {
    "fragment": "內容零碎",
    "over_masked": "遮罩過多",
    "weak_title": "標題無意義",
    "too_short": "內容過短",
}


def _body(text: str) -> str:
    return BANNER_PATTERN.sub("", str(text or "")).strip()


def mask_ratio(text: str) -> float:
    body = _body(text)
    if not body:
        return 0.0
    masked = sum(len(match.group(0)) for match in MASK_PATTERN.finditer(body))
    return masked / len(body)


def prose_ratio(text: str) -> float:
    """Share of the text that sits inside reasonably long sentences."""
    body = MASK_PATTERN.sub("", QUOTE_LINE.sub("", _body(text)))
    if not body:
        return 0.0
    sentences = re.split(r"[。！？!?\n]", body)
    connected = "".join(sentence for sentence in sentences if len(sentence.strip()) >= 15)
    return len(connected) / len(body)


def chunk_issues(chunk: dict) -> list[str]:
    text = str(chunk.get("text", ""))
    title = str(chunk.get("section_title") or chunk.get("title") or "")
    issues = []
    body = MASK_PATTERN.sub("", _body(text))
    if len(body.strip()) < 60:
        issues.append("too_short")
    elif prose_ratio(text) < 0.4:
        issues.append("fragment")
    if mask_ratio(text) >= 0.3:
        issues.append("over_masked")
    if MEANINGLESS_TITLE.match(title.strip()) or "[人名]" in title:
        issues.append("weak_title")
    return issues


def quality_report(chunks: list[dict], sample_limit: int = 40) -> dict:
    counts = {key: 0 for key in ISSUE_LABELS}
    samples: list[dict] = []
    flagged = 0
    for chunk in chunks:
        issues = chunk_issues(chunk)
        if not issues:
            continue
        flagged += 1
        for issue in issues:
            counts[issue] += 1
        if len(samples) < sample_limit:
            samples.append({
                "chunk_id": chunk.get("chunk_id", ""),
                "locator": chunk.get("locator", ""),
                "title": chunk.get("title", ""),
                "section_title": chunk.get("section_title", ""),
                "origin": chunk.get("origin", "file"),
                "issues": issues,
                "excerpt": " ".join(str(chunk.get("text", "")).split())[:160],
            })
    total = len(chunks)
    return {
        "total": total,
        "flagged": flagged,
        "healthy": total - flagged,
        "counts": counts,
        "labels": ISSUE_LABELS,
        "samples": samples,
    }
