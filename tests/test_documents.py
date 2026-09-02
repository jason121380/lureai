"""上傳檔案的文字萃取。

Office 的新格式是 ZIP 裡放 XML，PDF 的內容串流是 zlib 壓的——都在標準庫的
範圍內，所以這個專案的零依賴原則不用破例。讀不出來時的訊息要能直接照著做。
"""
import io
import unittest
import zipfile

from app.documents import UnreadableDocument, extract_text


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


if __name__ == "__main__":
    unittest.main()
