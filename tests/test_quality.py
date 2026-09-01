"""回覆品質守門的判斷邊界。

這裡守的是實測 QA 抓到的四種失分：延後回答、承諾沒交付、問到立場不表態、
以及輔導對話裡不該出現的角色（主管／轉人工）。同時要守住反向：正常的好回答
不能被擋，擋掉會變成降級訊息，比廢話更糟。
"""
import unittest

from app import quality


class DelaySentenceTests(unittest.TestCase):
    def test_pure_delay_sentence_is_rejected(self):
        for answer in (
            "我陪你一起拆這個問題",
            "我先幫你看一下狀況",
            "我們先把這件事拆開",
        ):
            self.assertTrue(quality.problems("廣告成效不好", answer), answer)

    def test_delay_sentence_with_real_content_passes(self):
        # 有陪伴語氣沒關係，重點是同一則有沒有給東西。
        answer = "我陪你看一下\n先把上週的到店人數記下來"
        self.assertEqual(quality.problems("廣告成效不好", answer), [])

    def test_stance_counts_as_substance(self):
        answer = "我陪你判斷\n我的傾向是先不要加預算"
        self.assertEqual(quality.problems("要加預算嗎", answer), [])


class PromiseTests(unittest.TestCase):
    def test_promise_without_delivery_is_rejected(self):
        found = quality.problems("幫我寫開場白", "我幫你寫一版開場白")
        self.assertTrue(any("成品" in item for item in found))

    def test_promise_with_delivery_passes(self):
        answer = (
            "我幫你寫一版\n"
            "哈囉 上次那個顏色現在還好嗎\n"
            "這週有空我幫你補一下色\n"
            "你大概哪天方便"
        )
        self.assertEqual(quality.problems("幫我寫開場白", answer), [])

    def test_requested_count_must_be_delivered(self):
        found = quality.problems("給我十個 hashtag", "#台北染髮\n#台北美髮")
        self.assertTrue(any("10" in item for item in found))

    def test_enough_items_passes(self):
        answer = "\n".join(f"#標籤{index}" for index in range(1, 11))
        self.assertEqual(quality.problems("給我十個 hashtag", answer), [])


class StanceTests(unittest.TestCase):
    def test_stance_question_without_stance_is_rejected(self):
        found = quality.problems("你覺得我該不該漲價", "漲價這件事有很多面向要看")
        self.assertTrue(any("表態" in item for item in found))

    def test_stance_question_with_stance_passes(self):
        answer = "我的傾向是可以漲\n你回頭客有 7 成 撐得住"
        self.assertEqual(quality.problems("你覺得我該不該漲價", answer), [])


class ForbiddenRoleTests(unittest.TestCase):
    def test_escalating_to_a_human_role_is_rejected(self):
        for answer in ("這個要問你的主管", "我幫你轉人工處理", "這邊會有專人跟你聯繫"):
            found = quality.problems("客人要退費", answer)
            self.assertTrue(any("獨立設計師" in item for item in found), answer)


class SafetyTests(unittest.TestCase):
    def test_empty_answer_is_not_flagged(self):
        self.assertEqual(quality.problems("任何問題", ""), [])

    def test_ordinary_good_answer_passes(self):
        answer = "先看私訊到店率\n這 30 個裡面有幾個真的來"
        self.assertEqual(quality.problems("廣告成效不好", answer), [])

    def test_retry_note_carries_every_reason(self):
        found = ["第一個問題", "第二個問題"]
        note = quality.retry_note(found)
        self.assertIn("第一個問題", note)
        self.assertIn("第二個問題", note)
        self.assertEqual(quality.retry_note([]), "")


if __name__ == "__main__":
    unittest.main()
