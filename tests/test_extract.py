"""上傳文件 → 候選知識的切法。

後台的「新增知識」改成拖檔進來，這裡守著兩件事：規則切法一定給得出東西
（沒有 API key 的環境也要能用），以及模型回來的東西一定被清乾淨才顯示。
"""
import unittest

from app import extract
from app.curation import chunk_issues


DOC = """# 客訴當下的處理原則

客人反映顏色不對時，先確認是光線問題還是真的沒到位。不要當場說「其實這樣很好看」，那會讓客人覺得你在唬他。先承認他看到的事實，再給技術面的說明。

## 什麼時候可以安排修補

頭髮的狀況允許再上一次色才排修補。一週內連續兩次漂染會讓髮尾斷裂，寧可等一週，也不要當場硬做。等待期間給客人一支溫和的洗髮精。

## 退費的界線在哪裡

金額大或牽涉賠償時要先跟主管確認，不要當場承諾。先問客人最在意的是顏色還是髮質，多數人講退費其實是想要有人負責。把修補放在退費前面，給他一個比拿錢更好的選項。
"""


class SplitTests(unittest.TestCase):
    def test_headings_become_separate_chunks(self):
        chunks = extract.split_document("客訴手冊.md", DOC)

        self.assertEqual(
            [chunk["section_title"] for chunk in chunks],
            ["客訴當下的處理原則", "什麼時候可以安排修補", "退費的界線在哪裡"],
        )
        self.assertTrue(all(chunk["text"] for chunk in chunks))

    def test_the_file_name_becomes_the_category(self):
        chunks = extract.split_document("客訴手冊.md", DOC)

        self.assertTrue(all(chunk["category"] == "客訴手冊" for chunk in chunks))

    def test_short_sections_are_merged_not_dropped(self):
        """低於 60 字的段落往前併，不要丟掉——丟掉就是把使用者的內容吃掉。"""
        doc = "# 大標\n\n" + "這段夠長。" * 20 + "\n\n## 小標\n\n很短。\n"

        chunks = extract.split_document("測試.md", doc)

        self.assertEqual(len(chunks), 1)
        self.assertIn("小標", chunks[0]["text"])
        self.assertIn("很短", chunks[0]["text"])

    def test_plain_text_without_headings_still_produces_chunks(self):
        chunks = extract.split_document("純文字.txt", "沒有標題的一段話。" * 40)

        self.assertTrue(chunks)
        self.assertTrue(chunks[0]["section_title"])

    def test_每塊都撐得過後台的品質檢查(self):
        for chunk in extract.split_document("客訴手冊.md", DOC):
            issues = chunk_issues({"text": chunk["text"], "section_title": chunk["section_title"]})
            self.assertNotIn("too_short", issues, chunk["section_title"])
            self.assertNotIn("weak_title", issues, chunk["section_title"])

    def test_empty_document_gives_nothing(self):
        self.assertEqual(extract.split_document("空的.txt", "   \n\n "), [])

    def test_a_long_document_is_capped(self):
        doc = "\n\n".join(f"# 第 {i} 節\n\n{'內容夠長的一段話。' * 12}" for i in range(60))

        chunks = extract.split_document("很長.md", doc)

        self.assertLessEqual(len(chunks), extract.MAX_CHUNKS)


class ModelOutputTests(unittest.TestCase):
    def test_json_in_a_code_fence_is_parsed(self):
        raw = ('```json\\n{"items":[{"section_title":"標題","category":"售後與回流",'
               '"domain":"coaching","text":"客人反映顏色不對時先確認是光線問題還是真的沒到位不要當場說其實這樣很好看"}]}\\n```')

        items = extract._parse_items(raw)

        self.assertEqual(items[0]["section_title"], "標題")
        self.assertEqual(items[0]["domain"], "coaching")

    def test_an_unknown_domain_is_reclassified(self):
        raw = ('{"items":[{"section_title":"標題","category":"顧客服務",'
               '"domain":"亂寫的","text":"客人反映顏色不對時先確認是光線問題還是真的沒到位不要當場說其實這樣很好看"}]}')

        items = extract._parse_items(raw)

        self.assertIn(items[0]["domain"], ("operations", "coaching"))

    def test_items_without_a_title_or_body_are_dropped(self):
        # 沒標題、沒內容、以及「有文字但根本不成句」（日曆那種）都要被丟掉。
        raw = ('{"items":[{"section_title":"","text":"客人反映顏色不對時先確認是光線問題還是真的沒到位不要當場說其實這樣很好看"},'
               '{"section_title":"有標題","text":""},'
               '{"section_title":"數字表","text":"1 2 3 4 5 6 7 8 9 10"},'
               '{"section_title":"好的","text":"客人反映顏色不對時先確認是光線問題還是真的沒到位不要當場說其實這樣很好看"}]}')

        items = extract._parse_items(raw)

        self.assertEqual([item["section_title"] for item in items], ["好的"])

    def test_garbage_output_parses_to_nothing(self):
        self.assertEqual(extract._parse_items("模型今天不想回 JSON"), [])


class FallbackTests(unittest.TestCase):
    def test_without_a_model_the_rules_still_deliver(self):
        class NoModel:
            model_enabled = False

        items, source, _usage = extract.propose_chunks(NoModel(), "客訴手冊.md", DOC)

        self.assertEqual(source, "rules")
        self.assertTrue(items)

    def test_budget_exhausted_falls_back_to_the_rules(self):
        class WithModel:
            model_enabled = True

        items, source, _usage = extract.propose_chunks(WithModel(), "客訴手冊.md", DOC, allow_model=False)

        self.assertEqual(source, "rules")
        self.assertTrue(items)


if __name__ == "__main__":
    unittest.main()
