import unittest

from app.answer import CITATION_RETRY_NOTE, normalize_citation_marks


class NormalizeCitationTests(unittest.TestCase):
    def test_fullwidth_citation_marks_become_halfwidth(self):
        self.assertEqual(normalize_citation_marks("先做健檢【1】，再改回覆（2）。"), "先做健檢[1]，再改回覆[2]。")
        self.assertEqual(normalize_citation_marks("目標拆解〔３〕"), "目標拆解[3]")

    def test_normal_text_and_brackets_with_words_are_untouched(self):
        self.assertEqual(normalize_citation_marks("這是（例如）說明 [1]"), "這是（例如）說明 [1]")
        self.assertEqual(normalize_citation_marks("【重點】先回一句"), "【重點】先回一句")

    def test_retry_note_names_the_required_format(self):
        self.assertIn("[編號]", CITATION_RETRY_NOTE)
        self.assertIn("半形", CITATION_RETRY_NOTE)


class CitationRangeTests(unittest.TestCase):
    """編號長得像引用，不等於那個來源存在。"""

    def test_a_number_outside_the_sources_is_not_a_citation(self):
        from app.answer import AnswerEngine

        self.assertFalse(AnswerEngine.has_valid_citation("可以先觀察回流率 [99]", 4))
        self.assertFalse(AnswerEngine.has_valid_citation("回流率 [0]", 4))
        self.assertFalse(AnswerEngine.has_valid_citation("完全沒有編號", 4))

    def test_a_number_inside_the_sources_counts(self):
        from app.answer import AnswerEngine

        self.assertTrue(AnswerEngine.has_valid_citation("回流率 [2]", 4))
        # 有一個對的就算數，多寫一個錯的由 `_fit_citations` 在顯示時拿掉。
        self.assertTrue(AnswerEngine.has_valid_citation("甲 [1] 乙 [99]", 4))


if __name__ == "__main__":
    unittest.main()
