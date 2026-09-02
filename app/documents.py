"""把上傳的檔案讀成純文字。

零依賴：Office 的新格式（docx／xlsx／pptx）其實是 ZIP 裡面放 XML，用標準庫的
`zipfile` ＋ `xml.etree` 就拆得開；PDF 的內容串流是 zlib 壓的，`zlib` 也在標準庫。

**這裡只負責「拿到文字」**，怎麼切成知識是 `app/extract.py` 的事。

讀不出來時一律丟 `UnreadableDocument`，訊息要直接告訴人下一步怎麼做——
「這個檔讀不到文字」對使用者沒有幫助，「掃描的 PDF 請先轉成文字檔」才有。
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib


class UnreadableDocument(Exception):
    """讀不出文字，訊息會原樣顯示給使用者。"""


# 直接當文字讀的副檔名。
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".log", ".yml", ".yaml", ".xml", ".htm", ".html", ".srt", ".vtt", ".tex",
}
OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}
# 舊版 Office 是 OLE 複合文件，格式完全不同，標準庫拆不開。
LEGACY_OFFICE = {".doc": "Word", ".xls": "Excel", ".ppt": "PowerPoint"}
SUPPORTED_LABEL = "Word（.docx）、Excel（.xlsx）、PowerPoint（.pptx）、PDF、RTF，以及 .txt .md .csv .json 等文字檔"

# OOXML 的命名空間很囉嗦，直接用結尾比對標籤名。
def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _decode(data: bytes) -> str:
    """猜編碼。**UTF-16 只在有 BOM 時才試**——沒有 BOM 的 UTF-16 幾乎任何
    偶數長度的位元組都吃得下，排在前面會把 Big5 的中文檔解成亂碼。
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    without_blocks = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    return re.sub(r"<[^>]+>", " ", without_blocks)


def _docx_text(archive: zipfile.ZipFile) -> str:
    """段落 = <w:p>，換行 = <w:br>／<w:tab>。表格的每個儲存格也是段落。"""
    parts: list[str] = []
    for name in archive.namelist():
        if not (name == "word/document.xml" or name.startswith("word/header")
                or name.startswith("word/footer")):
            continue
        root = ET.fromstring(archive.read(name))
        for paragraph in root.iter():
            if _tag(paragraph) != "p":
                continue
            pieces = []
            for node in paragraph.iter():
                tag = _tag(node)
                if tag == "t" and node.text:
                    pieces.append(node.text)
                elif tag in ("tab", "br"):
                    pieces.append(" ")
            line = "".join(pieces).strip()
            if line:
                parts.append(line)
    return "\n".join(parts)


def _xlsx_text(archive: zipfile.ZipFile) -> str:
    """每一列變成一行、儲存格用「｜」隔開，表格結構才看得出來。"""
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root:
            if _tag(item) != "si":
                continue
            shared.append("".join(node.text or "" for node in item.iter() if _tag(node) == "t"))
    lines: list[str] = []
    sheets = sorted(n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    for name in sheets:
        root = ET.fromstring(archive.read(name))
        for row in root.iter():
            if _tag(row) != "row":
                continue
            cells: list[str] = []
            for cell in row:
                if _tag(cell) != "c":
                    continue
                value = ""
                for node in cell:
                    if _tag(node) == "v":
                        value = node.text or ""
                    elif _tag(node) == "is":
                        value = "".join(t.text or "" for t in node.iter() if _tag(t) == "t")
                # t="s" 代表這格的值是 sharedStrings 的索引。
                if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                cells.append(value.strip())
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                lines.append(" ｜ ".join(cells))
    return "\n".join(lines)


def _pptx_text(archive: zipfile.ZipFile) -> str:
    """一張投影片一段，投影片之間空一行。"""
    slides = sorted(
        (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
    )
    blocks: list[str] = []
    for name in slides:
        root = ET.fromstring(archive.read(name))
        lines: list[str] = []
        for paragraph in root.iter():
            if _tag(paragraph) != "p":
                continue
            line = "".join(node.text or "" for node in paragraph.iter() if _tag(node) == "t").strip()
            if line:
                lines.append(line)
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _ooxml_text(suffix: str, data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnreadableDocument("這個檔打不開，可能已經損毀或不是真的 Office 檔") from exc
    with archive:
        if suffix == ".docx":
            return _docx_text(archive)
        if suffix == ".xlsx":
            return _xlsx_text(archive)
        return _pptx_text(archive)


# PDF：把每個內容串流解壓，抓 Tj／TJ 這兩個「畫出文字」的運算子。
PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
PDF_TEXT_OP = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _pdf_text(data: bytes) -> str:
    chunks: list[str] = []
    for match in PDF_STREAM.finditer(data):
        raw = match.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass  # 沒壓縮的串流直接用
        for token in PDF_TEXT_OP.findall(raw):
            body = token[1:-1]
            body = re.sub(rb"\\([()\\])", rb"\1", body)
            text = body.decode("utf-8", errors="ignore")
            if text.strip():
                chunks.append(text)
    joined = " ".join(chunks)
    joined = re.sub(r"[ \t]{2,}", " ", joined).strip()
    # 掃描檔沒有文字層、CJK 又常常用子集字型（抓出來是亂碼），與其給一堆
    # 垃圾讓人以為壞掉，不如明講要怎麼繞過。
    if len(joined) < 40:
        raise UnreadableDocument(
            "這個 PDF 抓不到文字（掃描檔或用了特殊字型）。"
            "請用 Word 另存成 .docx，或把內容複製成 .txt 再上傳"
        )
    return joined


# 一個中文字在 RTF 裡是**連續兩個** \'xx，要整串收集起來一次解碼；
# 一個一個解會各自變成半個字，中文全部壞掉。
RTF_HEX_RUN = re.compile(r"(?:\\'[0-9a-fA-F]{2})+")
RTF_UNICODE = re.compile(r"\\u(-?\d+)\s?\??")
RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d*\s?|[{}]")


def _rtf_text(data: bytes) -> str:
    text = _decode(data)
    text = RTF_UNICODE.sub(lambda m: chr(int(m.group(1)) % 65536), text)

    def decode_run(match: re.Match) -> str:
        raw = bytes(int(pair, 16) for pair in re.findall(r"[0-9a-fA-F]{2}", match.group(0)))
        for encoding in ("cp950", "big5", "gb18030", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("cp1252", errors="ignore")

    text = RTF_HEX_RUN.sub(decode_run, text)
    return re.sub(r"\n{3,}", "\n\n", RTF_CONTROL.sub("", text))


def extract_text(name: str, data: bytes) -> str:
    """依副檔名把檔案讀成純文字。讀不出來就丟 UnreadableDocument。"""
    suffix = ("." + str(name or "").rsplit(".", 1)[-1].lower()) if "." in str(name or "") else ""
    if suffix in LEGACY_OFFICE:
        raise UnreadableDocument(
            f"{LEGACY_OFFICE[suffix]} 舊版格式（{suffix}）讀不了，"
            f"請用 {LEGACY_OFFICE[suffix]} 另存成新版再上傳"
        )
    if suffix in OOXML_SUFFIXES:
        text = _ooxml_text(suffix, data)
    elif suffix == ".pdf":
        text = _pdf_text(data)
    elif suffix == ".rtf":
        text = _rtf_text(data)
    elif suffix in TEXT_SUFFIXES:
        text = _decode(data)
        if suffix in (".htm", ".html", ".xml"):
            text = _strip_html(text)
    else:
        raise UnreadableDocument(f"不支援這種檔案（{suffix or '沒有副檔名'}）。目前可以讀 {SUPPORTED_LABEL}")
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()
    if not cleaned:
        raise UnreadableDocument("這個檔裡面沒有文字內容")
    return cleaned
