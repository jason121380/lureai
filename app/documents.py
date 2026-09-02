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


# ---- PDF ------------------------------------------------------------------
# PDF 的字元編碼不是 Unicode：內嵌子集字型時，「客」可能是編號 0x0012。要拿到
# 真正的字，得去讀字型附的 /ToUnicode CMap（編號 → Unicode 的對照表）。
# 少了這一步，中文 PDF 抓出來就是一串亂碼——而且長度夠長，用長度判斷擋不掉。
PDF_OBJECT = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.DOTALL)
PDF_STREAM_BODY = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
PDF_PAGE = re.compile(rb"/Type\s*/Page\b")
PDF_CONTENTS = re.compile(rb"/Contents\s+(?:(\d+)\s+\d+\s+R|\[(.*?)\])", re.DOTALL)
PDF_RESOURCES_REF = re.compile(rb"/Resources\s+(\d+)\s+\d+\s+R")
PDF_FONT_DICT = re.compile(rb"/Font\s*<<(.*?)>>", re.DOTALL)
PDF_FONT_ENTRY = re.compile(rb"/([^\s/<>\[\]]+)\s+(\d+)\s+\d+\s+R")
PDF_TOUNICODE = re.compile(rb"/ToUnicode\s+(\d+)\s+\d+\s+R")
PDF_REF = re.compile(rb"(\d+)\s+\d+\s+R")

CMAP_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.DOTALL)
CMAP_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.DOTALL)
CMAP_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")
# 內容串流的四種東西：
#   1. [ ... ] TJ  一段文字＋字距（負數夠大＝一個空格）
#   2. ( ... ) Tj  或 < ... > Tj
#   3. /F1 12 Tf   換字型
#   4. x y Td／TD、T*、ET  換行
# **Td 不一定是換行**：它是「移動文字位置」，同一行做字距微調也用它。只有
# 垂直位移（第二個數字不是 0）才是真的換行——不分辨的話中文標題會被拆成
# 一個字一行。
PDF_TOKEN = re.compile(
    rb"\[(.*?)\]\s*TJ"
    rb"|(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)\s*(?:Tj|'|\")"
    rb"|/([^\s/<>\[\]]+)\s+[\d.]+\s+Tf"
    rb"|(-?[\d.]+)\s+(-?[\d.]+)\s+(?:Td|TD)"
    rb"|(?:-?[\d.]+\s+){4}(-?[\d.]+)\s+(-?[\d.]+)\s+Tm"
    rb"|\b(T\*|BT|ET)\b",
    re.DOTALL,
)
PDF_TJ_PART = re.compile(rb"(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)|(-?[\d.]+)")
PDF_ESCAPE = re.compile(rb"\\([nrtbf()\\]|[0-7]{1,3})")
PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
# TJ 陣列裡的負數是往回拉的字距，夠大就代表這裡有一個空格。
TJ_SPACE = 180.0


def _pdf_objects(data: bytes) -> dict[int, bytes]:
    return {int(m.group(1)): m.group(2) for m in PDF_OBJECT.finditer(data)}


def _pdf_stream(body: bytes) -> bytes:
    match = PDF_STREAM_BODY.search(body)
    if not match:
        return b""
    raw = match.group(1)
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


def _hex_to_text(token: bytes) -> str:
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", token)
    if len(digits) % 4:
        digits += b"0" * (4 - len(digits) % 4)
    return bytes.fromhex(digits.decode()).decode("utf-16-be", errors="ignore")


def _parse_cmap(stream: bytes) -> tuple[dict[int, str], int]:
    """讀 /ToUnicode CMap，回傳（編號 → 字, 一個編號佔幾個位元組）。"""
    mapping: dict[int, str] = {}
    width = 1
    for block in CMAP_BFCHAR.findall(stream):
        tokens = CMAP_HEX.findall(block)
        for source, target in zip(tokens[::2], tokens[1::2]):
            width = max(width, len(source) // 2)
            mapping[int(source, 16)] = _hex_to_text(b"<" + target + b">")
    for block in CMAP_BFRANGE.findall(stream):
        tokens = CMAP_HEX.findall(block)
        for low, high, target in zip(tokens[::3], tokens[1::3], tokens[2::3]):
            width = max(width, len(low) // 2)
            start, stop, base = int(low, 16), int(high, 16), int(target, 16)
            if stop - start > 65535:
                continue
            for offset in range(stop - start + 1):
                mapping[start + offset] = chr(base + offset)
    return mapping, width


def _decode_show(token: bytes, cmap: dict[int, str] | None, width: int) -> str:
    if token.startswith(b"<"):
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", token)
        raw = bytes.fromhex(digits.decode()) if len(digits) % 2 == 0 else b""
    else:
        raw = PDF_ESCAPE.sub(
            lambda m: PDF_ESCAPES.get(m.group(1), bytes([int(m.group(1), 8)]))
            if m.group(1) in PDF_ESCAPES or m.group(1).isdigit() else m.group(1),
            token[1:-1],
        )
    if not cmap:
        return raw.decode("latin-1", errors="ignore")
    step = max(1, width)
    out = []
    for index in range(0, len(raw) - step + 1, step):
        out.append(cmap.get(int.from_bytes(raw[index:index + step], "big"), ""))
    return "".join(out)


# 同一列的兩段文字 Y 座標會有一點點差（下標、不同字級），差這麼多以內算同一行。
SAME_LINE = 2.0
CJK = re.compile(r"[一-鿿　-〿＀-￯]")


def _needs_space(before: str, after: str) -> bool:
    """兩段之間要不要補空格。

    中日韓文字本來就不用空格——PDF 常常一個字一次定位（字距微調），
    每次都補的話「客訴處理原則」會變成「客 訴 處 理 原 則」。
    """
    if not before or not after:
        return False
    if CJK.match(before[-1]) or CJK.match(after[0]):
        return False
    return before[-1] not in " \n" and after[0] not in " \n"


def _pdf_page_text(content: bytes, fonts: dict[bytes, tuple[dict, int]]) -> str:
    """把一頁的內容串流變成文字。

    **要追 Y 座標**：表格／日曆型的 PDF 每個儲存格都各自定位，只看「有沒有
    移動」的話會變成一個字一行。同一列的儲存格 Y 一樣，要接在同一行。
    """
    pieces: list[str] = []
    cmap: dict[int, str] | None = None
    width = 1
    y = None          # 目前這段文字的基線位置
    line_y = None     # 現在這一行的基線位置
    pending_space = False

    def newline() -> None:
        if pieces and pieces[-1] != "\n":
            pieces.append("\n")

    def move_to(new_y: float) -> None:
        nonlocal line_y, pending_space
        if line_y is not None and abs(new_y - line_y) > SAME_LINE:
            newline()
            pending_space = False
        elif line_y is not None:
            # 同一行的下一段：是不是要空一格，等看到下一段的第一個字再決定。
            pending_space = True
        line_y = new_y

    def push(text: str) -> None:
        nonlocal pending_space
        if not text:
            return
        if pending_space and _needs_space("".join(pieces[-1:]), text):
            pieces.append(" ")
        pending_space = False
        pieces.append(text)

    for match in PDF_TOKEN.finditer(content):
        array, single, font, _tx, ty, _tm_x, tm_y, breaker = match.groups()
        if font is not None:
            cmap, width = fonts.get(font, (None, 1))
        elif tm_y is not None:          # a b c d e f Tm ——絕對位置
            try:
                y = float(tm_y)
                move_to(y)
            except ValueError:
                pass
        elif array is not None:
            for part in PDF_TJ_PART.finditer(array):
                if part.group(1):
                    push(_decode_show(part.group(1), cmap, width))
                else:
                    try:
                        if float(part.group(2)) <= -TJ_SPACE and pieces and pieces[-1] != " ":
                            pieces.append(" ")
                    except ValueError:
                        pass
        elif single is not None:
            push(_decode_show(single, cmap, width))
        elif ty is not None:            # tx ty Td ——相對位移
            try:
                y = (y or 0.0) + float(ty)
                move_to(y)
            except ValueError:
                pass
        elif breaker is not None:
            # BT 只重設「相對位移」的累加，**不要重設 line_y**：表格的每個
            # 儲存格常常各自包在一組 BT…ET 裡，重設的話同一列會被拆成好幾行。
            # ET 也不換行，換不換行一律交給 Y 座標決定。
            if breaker == b"BT":
                y = None
            elif breaker == b"T*":
                newline()
                line_y = None
    return "".join(pieces)


# 「看得懂的字」：中日韓、拉丁字母、數字、空白與常見標點。子集字型沒解開時
# 抓到的是控制字元與隨機符號，這個比例會掉到很低。
READABLE = re.compile(
    r"[一-鿿　-〿＀-￯0-9A-Za-z\s.,;:!?%()\-—、。，！？「」『』（）]"
)


def _readable_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(READABLE.findall(text)) / len(text)


def _pdf_text(data: bytes) -> str:
    objects = _pdf_objects(data)
    cmaps: dict[int, tuple[dict, int]] = {}

    def cmap_for(font_id: int) -> tuple[dict, int]:
        body = objects.get(font_id, b"")
        ref = PDF_TOUNICODE.search(body)
        if not ref:
            # 組合字型（Type0）的實際字型在 /DescendantFonts 裡。
            for child in PDF_REF.findall(body):
                child_body = objects.get(int(child), b"")
                ref = PDF_TOUNICODE.search(child_body)
                if ref:
                    break
        if not ref:
            return ({}, 1)
        key = int(ref.group(1))
        if key not in cmaps:
            cmaps[key] = _parse_cmap(_pdf_stream(objects.get(key, b"")))
        return cmaps[key]

    pages: list[str] = []
    for body in objects.values():
        if not PDF_PAGE.search(body):
            continue
        resources = body
        ref = PDF_RESOURCES_REF.search(body)
        if ref:
            resources = objects.get(int(ref.group(1)), b"")
        fonts: dict[bytes, tuple[dict, int]] = {}
        font_dict = PDF_FONT_DICT.search(resources)
        if font_dict:
            for name, font_id in PDF_FONT_ENTRY.findall(font_dict.group(1)):
                mapping, width = cmap_for(int(font_id))
                if mapping:
                    fonts[name] = (mapping, width)
        contents = PDF_CONTENTS.search(body)
        stream_ids: list[int] = []
        if contents and contents.group(1):
            stream_ids.append(int(contents.group(1)))
        elif contents and contents.group(2):
            stream_ids.extend(int(ref) for ref in PDF_REF.findall(contents.group(2)))
        text = "".join(
            _pdf_page_text(_pdf_stream(objects.get(sid, b"")), fonts) for sid in stream_ids
        )
        if text.strip():
            pages.append(text)

    joined = re.sub(r"[ \t]{2,}", " ", "\n\n".join(pages)).strip()
    # 掃描檔完全沒有文字層（長度 0），有文字層但解不開字型時抓到的是亂碼
    # （長度夠長，光看長度擋不掉）。真正的判斷是「看得懂的字」佔多少；
    # 長度只留一個很低的下限，免得把內容本來就短的 PDF 也擋掉。
    if len(joined) < 16 or _readable_ratio(joined) < 0.7:
        raise UnreadableDocument(
            "這個 PDF 抓不到文字（掃描檔，或用了讀不出對照表的字型）。"
            "請用 Word 另存成 .docx，或把內容複製成 .txt 再上傳"
        )
    return joined


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
