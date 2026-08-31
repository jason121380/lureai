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


if __name__ == "__main__":
    unittest.main()
