import unittest
from types import SimpleNamespace

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


class GroundingDiagnosticTests(unittest.TestCase):
    def hit(self, text):
        return SimpleNamespace(text=text, locator="metric-1")

    def test_flags_substantive_claim_without_a_citation(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "先記錄每次私訊的回覆時間，再比較預約結果。",
            [self.hit("記錄私訊回覆時間，並和預約結果一起檢查。")],
            tone="expert",
        )

        self.assertEqual(result["uncited_claims"], ["先記錄每次私訊的回覆時間，再比較預約結果。"])

    def test_flags_invalid_citation_even_when_another_mark_is_valid(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics("先看私訊回覆率。[1] 再看預約率。[99]", [self.hit("私訊回覆率與預約率")])

        self.assertEqual(result["invalid_citations"], [99])

    def test_numeric_support_must_match_the_cited_topic(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "連續做 7 天、每天 3 次、每次 5-10 分鐘。[1]",
            [self.hit("客單價沒有標準答案；另一個範例提到 7 天。")],
        )

        self.assertEqual(result["unsupported_numbers"], ["7 天", "3 次", "5-10 分鐘"])

    def test_citation_to_an_unrelated_source_does_not_support_a_claim(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "先把廣告受眾年齡縮小再加預算。[1]",
            [self.hit("客單價沒有標準答案，要看定位與服務組合。")],
        )

        self.assertEqual(result["unsupported_claims"], ["先把廣告受眾年齡縮小再加預算。[1]"])

    def test_related_metric_and_number_are_supported(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "私訊轉預約率低於 10% 時，先檢查承接。[1]",
            [self.hit("私訊轉預約率低於 10% 時，先檢查承接話術。")],
        )

        self.assertEqual(result["unsupported_numbers"], [])
        self.assertFalse(result["quality_failed"])

    def test_user_funnel_fact_is_reused_as_numeric_evidence(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "目前有 30 則私訊，先整理回覆紀錄。[1]",
            [self.hit("私訊紀錄要包含回覆內容與後續預約結果。")],
            question="我這週有30則私訊",
        )

        self.assertEqual(result["unsupported_numbers"], [])

    def test_chinese_rate_claim_without_support_is_flagged(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics("下週客單價回升至少一成。[1]", [self.hit("客單價沒有標準答案。")])

        self.assertEqual(result["unsupported_numbers"], ["一 成"])
        self.assertTrue(result["quality_failed"])

    def test_number_cannot_be_borrowed_from_an_unrelated_metric_in_same_chunk(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "私訊轉預約率應達 10%。[1]",
            [self.hit("私訊轉預約率沒有標準答案。熟客折扣為 10%。")],
        )

        self.assertEqual(result["unsupported_numbers"], ["10 %"])

    def test_message_count_cannot_authorize_a_duration(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "私訊紀錄要持續追蹤 30 天。[1]",
            [self.hit("私訊紀錄要包含回覆內容與後續預約結果。")],
            question="我這週有30則私訊",
        )

        self.assertEqual(result["unsupported_numbers"], ["30 天"])

    def test_arabic_rate_range_never_crashes_and_is_supported(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "客單價預計上升 1-2 成。[1]",
            [self.hit("客單價預計上升 10-20%。")],
        )

        self.assertEqual(result["unsupported_numbers"], [])
        self.assertFalse(result["quality_failed"])

    def test_chinese_rate_range_normalizes_to_percent_range(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "客單價預計上升一至兩成。[1]",
            [self.hit("客單價預計上升 10-20%。")],
        )

        self.assertEqual(result["unsupported_numbers"], [])

    def test_answer_numeric_context_does_not_borrow_a_later_topic(self):
        from app.quality import grounding_diagnostics

        result = grounding_diagnostics(
            "私訊轉預約率應達 10%，熟客折扣要調整。[1]",
            [self.hit("私訊轉預約率沒有標準答案。熟客折扣為 10%。")],
        )

        self.assertEqual(result["unsupported_numbers"], ["10 %"])

    def test_service_and_smalltalk_are_not_forced_to_cite(self):
        from app.quality import grounding_diagnostics

        service = grounding_diagnostics("先問客人今天想整理哪一段", [], tone="service")
        smalltalk = grounding_diagnostics("嗨，我在～", [], tone="expert", substantive=False)

        self.assertFalse(service["quality_failed"])
        self.assertFalse(smalltalk["quality_failed"])


if __name__ == "__main__":
    unittest.main()
