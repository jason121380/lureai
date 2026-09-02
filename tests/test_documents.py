"""上傳檔案的文字萃取。

Office 的新格式是 ZIP 裡放 XML，PDF 的內容串流是 zlib 壓的——都在標準庫的
範圍內，所以這個專案的零依賴原則不用破例。讀不出來時的訊息要能直接照著做。
"""
import io
import unittest
import zipfile
import zlib

from app.documents import UnreadableDocument, _pdf_stream, extract_text


def zip_bytes(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


class OfficeTests(unittest.TestCase):
    def test_word_keeps_paragraphs_line_breaks_and_tables(self):
        data = zip_bytes({"word/document.xml": f"""<?xml version="1.0"?>
        <w:document {W}><w:body>
        <w:p><w:r><w:t>客訴處理原則</w:t></w:r></w:p>
        <w:p><w:r><w:t>先確認光線</w:t></w:r><w:br/><w:r><w:t>再看髮況</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格裡的字</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>"""})

        text = extract_text("手冊.docx", data)

        self.assertIn("客訴處理原則", text)
        self.assertIn("先確認光線 再看髮況", text)
        # 表格的字也要拿到，不然價目表整張會不見。
        self.assertIn("表格裡的字", text)

    def test_excel_resolves_shared_strings_and_keeps_rows(self):
        """數字直接存在儲存格，文字是存到 sharedStrings 再用索引指過去。"""
        data = zip_bytes({
            "xl/sharedStrings.xml": f'<?xml version="1.0"?><sst {S}>'
                                    "<si><t>指標</t></si><si><t>到店率</t></si></sst>",
            "xl/worksheets/sheet1.xml": f"""<?xml version="1.0"?><worksheet {S}><sheetData>
            <row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>
            <row><c><v>2026</v></c><c><v>0.22</v></c></row>
            </sheetData></worksheet>""",
        })

        text = extract_text("數據.xlsx", data)

        self.assertIn("指標 ｜ 到店率", text)
        self.assertIn("2026 ｜ 0.22", text)

    def test_powerpoint_splits_slides(self):
        data = zip_bytes({"ppt/slides/slide1.xml": f"""<?xml version="1.0"?>
        <p:sld xmlns:p="x" {A}>
        <a:p><a:r><a:t>第一張</a:t></a:r></a:p><a:p><a:r><a:t>重點是留客</a:t></a:r></a:p>
        </p:sld>"""})

        self.assertEqual(extract_text("簡報.pptx", data), "第一張\n重點是留客")

    def test_a_broken_archive_says_so(self):
        with self.assertRaises(UnreadableDocument) as caught:
            extract_text("壞掉.docx", b"not a zip")

        self.assertIn("損毀", str(caught.exception))


def build_pdf(text: str) -> bytes:
    """組一份會走「子集字型」那條路的 PDF。

    字元編碼從 1 開始編，真正的字要去 /ToUnicode 對照表查——這正是中文 PDF
    抓出來會是亂碼的原因。
    """
    codes = {char: index + 1 for index, char in enumerate(dict.fromkeys(text))}
    pairs = "".join(f"<{code:04X}> <{ord(char):04X}>\n" for char, code in codes.items())
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        f"{len(codes)} beginbfchar\n{pairs}endbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode()
    body = "".join(f"{codes[char]:04X}" for char in text)
    content = f"BT /F1 12 Tf 72 720 Td <{body}> Tj ET".encode()
    objects = {
        1: b"<< /Type /Page /Resources << /Font << /F1 2 0 R >> >> /Contents 4 0 R >>",
        2: b"<< /Type /Font /Subtype /Type0 /ToUnicode 3 0 R >>",
        3: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap),
        4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    }
    out = b"%PDF-1.4\n"
    for number, payload in objects.items():
        out += b"%d 0 obj\n%s\nendobj\n" % (number, payload)
    return out + b"%%EOF"


def build_pdf_with_form_xobject(text: str) -> bytes:
    """把內容包進 Form XObject——簡報／報表列印出來常常長這樣。

    頁面串流只剩 `/X1 Do`，真正的字在表單裡。不跟進去就只抓得到頁首頁尾。
    """
    codes = {char: index + 1 for index, char in enumerate(dict.fromkeys(text))}
    pairs = "".join(f"<{code:04X}> <{ord(char):04X}>\n" for char, code in codes.items())
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        f"{len(codes)} beginbfchar\n{pairs}endbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode()
    body = "".join(f"{codes[char]:04X}" for char in text)
    form = f"BT /F1 12 Tf 72 720 Td <{body}> Tj ET".encode()
    page_stream = b"q 1 0 0 1 0 0 cm /X1 Do Q"
    objects = {
        1: b"<< /Type /Page /Resources << /XObject << /X1 5 0 R >> >> /Contents 4 0 R >>",
        2: b"<< /Type /Font /Subtype /Type0 /ToUnicode 3 0 R >>",
        3: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap),
        4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(page_stream), page_stream),
        5: b"<< /Type /XObject /Subtype /Form /Resources << /Font << /F1 2 0 R >> >> "
           b"/Length %d >>\nstream\n%s\nendstream" % (len(form), form),
    }
    out = b"%PDF-1.4\n"
    for number, payload in objects.items():
        out += b"%d 0 obj\n%s\nendobj\n" % (number, payload)
    return out + b"%%EOF"


class PdfTests(unittest.TestCase):
    def test_subset_font_text_is_mapped_through_tounicode(self):
        """PDF 的字元編碼不是 Unicode，要查字型附的 /ToUnicode 對照表。

        少了這一步，中文 PDF 抓出來是一長串亂碼——而且長度夠長，光看長度
        擋不掉，使用者看到的就是滿滿的符號。
        """
        wanted = "客訴處理原則先確認是光線問題還是真的沒到位"

        text = extract_text("手冊.pdf", build_pdf(wanted))

        self.assertEqual(text, wanted)

    def test_text_inside_a_form_xobject_is_found(self):
        wanted = "服務後第三天傳關懷訊息，先問整理順不順，再自然邀請留評論。"
        text = extract_text("簡報.pdf", build_pdf_with_form_xobject(wanted))
        self.assertIn("服務後第三天", text)
        self.assertIn("邀請留評論", text)

    def test_a_slide_deck_printed_as_images_says_so(self):
        """使用者實際遇到的：投影片是圖片，只抓得到瀏覽器列印的頁首頁尾。

        以前這種會過關，然後把一行網址存成一則「知識」。
        """
        furniture = "2026/9/2下午3:05即時簡報\nhttps://www.taiwan-marketing.com/slides2/22"
        with self.assertRaises(UnreadableDocument) as caught:
            extract_text("簡報.pdf", build_pdf(furniture))
        self.assertIn("內容本身是圖片", str(caught.exception))
        self.assertIn(".pptx", str(caught.exception))

    def test_a_page_that_happens_to_cite_a_url_still_reads(self):
        """有網址不代表沒內容——只有「整份只剩網址」才要擋。"""
        wanted = "Google 商家的評論流程要放在售後關懷之後，詳細做法看 https://example.com/guide"
        text = extract_text("手冊.pdf", build_pdf(wanted))
        self.assertIn("售後關懷之後", text)

    def test_garbled_output_is_rejected_rather_than_shown(self):
        """有文字層但解不開字型時，抓到的是亂碼。給亂碼比明講讀不到更糟。"""
        junk = "".join(chr(0x2400 + index % 200) for index in range(300))
        pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Page /Contents 2 0 R >>\nendobj\n" \
              b"2 0 obj\nstream\n" + f"BT ({junk}) Tj ET".encode("latin-1", "ignore") + \
              b"\nendstream\nendobj\n%%EOF"

        with self.assertRaises(UnreadableDocument) as caught:
            extract_text("亂碼.pdf", pdf)

        self.assertIn(".docx", str(caught.exception))


class TextTests(unittest.TestCase):
    def test_plain_text_and_encodings(self):
        self.assertEqual(extract_text("a.txt", "第一行\n第二行".encode()), "第一行\n第二行")
        self.assertIn("中文", extract_text("b.txt", "中文".encode("big5")))

    def test_html_drops_tags_and_script(self):
        html = "<html><style>p{color:red}</style><body><p>網頁內文</p></body></html>".encode()

        self.assertIn("網頁內文", extract_text("page.html", html))
        self.assertNotIn("color:red", extract_text("page.html", html))

    def test_rtf_decodes_multi_byte_escapes_as_one_character(self):
        """一個中文字是連續兩個 \\'xx，一個一個解會各自變成半個字。"""
        escaped = "".join(f"\\'{byte:02x}" for byte in "客訴".encode("cp950"))
        data = ("{\\rtf1\\ansi " + escaped + " ok}").encode("cp950")

        self.assertIn("客訴", extract_text("note.rtf", data))


class UnsupportedTests(unittest.TestCase):
    def test_legacy_office_tells_you_to_save_as_the_new_format(self):
        with self.assertRaises(UnreadableDocument) as caught:
            extract_text("舊.doc", b"\xd0\xcf\x11\xe0")

        self.assertIn("另存成新版", str(caught.exception))

    def test_a_scanned_pdf_says_what_to_do_instead(self):
        """掃描檔沒有文字層，給一堆亂碼比明講讀不到更糟。"""
        with self.assertRaises(UnreadableDocument) as caught:
            extract_text("scan.pdf", b"%PDF-1.4\nstream\nxx\nendstream")

        self.assertIn(".docx", str(caught.exception))

    def test_an_unknown_type_lists_what_is_supported(self):
        with self.assertRaises(UnreadableDocument) as caught:
            extract_text("photo.png", b"\x89PNG")

        self.assertIn("Word", str(caught.exception))
        self.assertIn("Excel", str(caught.exception))

    def test_an_empty_file_is_rejected(self):
        with self.assertRaises(UnreadableDocument):
            extract_text("empty.txt", b"   \n  ")


class DecompressionBombTests(unittest.TestCase):
    """限制要在解壓的時候做，不能只在最後檢查文字長度——那時候記憶體早就吃掉了。"""

    @staticmethod
    def _docx(payload: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("word/document.xml", payload)
        return buffer.getvalue()

    def test_a_tiny_docx_that_expands_enormously_is_rejected(self):
        body = (
            "<?xml version='1.0'?><w:document "
            "xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            "<w:p><w:r><w:t>" + "A" * 262144 + "</w:t></w:r></w:p></w:document>"
        ).encode()
        data = self._docx(body)
        self.assertLess(len(data), 5000, "測試前提：壓縮後應該很小")

        with self.assertRaises(UnreadableDocument):
            extract_text("bomb.docx", data)

    def test_a_normal_docx_still_reads(self):
        body = (
            "<?xml version='1.0'?><w:document "
            "xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            "<w:p><w:r><w:t>客訴處理流程第一步先聽完</w:t></w:r></w:p></w:document>"
        ).encode()

        self.assertIn("客訴處理流程", extract_text("ok.docx", self._docx(body)))

    def test_a_pdf_stream_with_an_absurd_ratio_is_rejected(self):
        raw = zlib.compress(b"B" * 262144, 9)
        with self.assertRaises(UnreadableDocument):
            _pdf_stream(b"stream\n" + raw + b"\nendstream")

    def test_a_normal_pdf_stream_still_decompresses(self):
        raw = zlib.compress(b"BT /F1 12 Tf (hello this is a page) Tj ET" * 20)

        self.assertTrue(_pdf_stream(b"stream\n" + raw + b"\nendstream"))


if __name__ == "__main__":
    unittest.main()


class ProseTests(unittest.TestCase):
    """日曆與純數字表格抓得到文字，但那不是知識。"""

    def test_a_calendar_produces_no_candidates(self):
        from app.extract import split_document

        calendar = "2026 年 9 月\n一二三四五六日\n1 2 3 4 5 6 7\n8 9 10 11 12 13 14"

        self.assertEqual(split_document("日曆.pdf", calendar), [])

    def test_real_content_still_produces_candidates(self):
        from app.extract import split_document

        text = (
            "客訴處理原則\n"
            "客人反映顏色不對時，先確認是光線問題還是真的沒到位。"
            "不要當場說「其實這樣很好看」，那會讓客人覺得你在唬他。"
        )

        proposals = split_document("手冊.pdf", text)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["section_title"], "客訴處理原則")

    def test_a_lone_url_is_not_knowledge(self):
        """一行網址就有三十幾個拉丁字母，光數字母會把列印頁尾當成有內容。"""
        from app.extract import split_document

        furniture = "2026/9/2 下午3:05 即時簡報\nhttps://www.taiwan-marketing.com/slides2/22"
        self.assertEqual(split_document("簡報.pdf", furniture), [])

    def test_a_price_table_is_still_knowledge(self):
        """有中文的表格要留著——價目表是有用的知識，不能跟日曆一起被丟掉。"""
        from app.extract import split_document

        table = (
            "項目 ｜ 價格 ｜ 說明\n"
            "染髮 ｜ 3800 ｜ 含護髮與吹整\n"
            "燙髮 ｜ 4200 ｜ 長髮另計\n"
            "接髮 ｜ 依長度報價 ｜ 需先看髮況"
        )

        self.assertTrue(split_document("價目表.xlsx", table))
