"""回覆品質守門的判斷邊界。

這裡守的是實測 QA 抓到的六種失分：延後回答、承諾沒交付、問到立場不表態、
把人推給不存在的對象（轉人工／會有專人）、被質疑就無條件認錯、同一則自相矛盾。
同時要守住反向：正常的好回答
不能被擋，擋掉會變成降級訊息，比廢話更糟——**「主管」不算違規**，沙龍當然有
主管，整份 ops 知識的客訴、請假、輪值、離職流程都在講主管。
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
    def test_punting_to_a_nonexistent_agent_is_rejected(self):
        for answer in ("我幫你轉人工處理", "這邊會有專人跟你聯繫", "我轉接專人給你"):
            found = quality.problems("客人要退費", answer)
            self.assertTrue(any("沒有人工客服" in item for item in found), answer)

    def test_real_roles_in_the_playbooks_pass(self):
        # 沙龍有主管、有專人、有公司現行公告，這些都是知識庫的原文用法。
        for question, answer in (
            ("客人客訴要怎麼處理", "超出職權的部分請主管出面\n你先掌握問題再道歉"),
            ("年假怎麼請", "年假須經主管核准 避開招生期"),
            ("櫃檯要怎麼整理", "檯面簡化 有專人收拾"),
            ("價格可以自己改嗎", "價格依公司現行公告為準 不要自己開"),
        ):
            self.assertEqual(quality.problems(question, answer), [], answer)


class CapitulationTests(unittest.TestCase):
    """被頂一句就道歉收回立場。輔導最怕這個——設計師來就是要一個站得住的判斷。"""

    def test_apologising_and_backing_down_with_no_reason_is_rejected(self):
        found = quality.problems("你說錯了吧", "抱歉，是我說錯了，你說的才對。")
        self.assertTrue(found)
        self.assertIn("收回", found[0])

    def test_correcting_yourself_with_a_reason_passes(self):
        # 真的講錯就該改口——只要說得出錯在哪裡、正確的是什麼。
        answer = "抱歉，我剛剛講錯了。原因是我把到店率當成回流率；到店率的及格線是 20%。"
        self.assertEqual(quality.problems("你說錯了吧", answer), [])

    def test_holding_the_position_passes(self):
        answer = "我的傾向還是先不要漲價。你上個月回流率 25%，還沒到 30-40% 的健康區間。"
        self.assertEqual(quality.problems("不對吧", answer), [])

    def test_apology_without_pushback_is_not_flagged(self):
        # 沒有人質疑他，那句「不好意思」只是禮貌用語。
        self.assertEqual(quality.problems("私訊怎麼開場？", "不好意思讓你久等，先問髮況。"), [])


class ContradictionTests(unittest.TestCase):
    def test_saying_both_do_and_dont_about_one_thing_is_rejected(self):
        answer = "現在建議漲價 5-10%。不過這個階段不要漲價，先把回流率做起來。"
        found = quality.problems("我可以漲價嗎", answer)
        self.assertTrue(any("漲價" in item and "又說" in item for item in found), found)

    def test_one_clear_stance_passes(self):
        answer = "我的傾向是可以漲價，幅度抓 5-10%，先從新客開始。"
        self.assertEqual(quality.problems("我可以漲價嗎", answer), [])

    def test_declining_it_outright_passes(self):
        # 「不建議漲價」裡面也有「建議漲價」，肯定詞的擋字要真的擋住。
        answer = "我不建議漲價，先把指名率做起來，回流率到 30% 再談。"
        self.assertEqual(quality.problems("我可以漲價嗎", answer), [])

    def test_a_qualifier_on_the_same_verb_is_not_a_contradiction(self):
        answer = "可以漲價，但不要漲太多，5-10% 就好。"
        self.assertEqual(quality.problems("我可以漲價嗎", answer), [])

    def test_no_curated_chunk_reads_as_self_contradictory(self):
        """278 塊人工整理的知識就是模型輸出的語感，一塊都不該被判成矛盾。"""
        import json
        from pathlib import Path

        rows = [
            json.loads(line)
            for line in Path("knowledge/designer_coaching_process.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        self.assertGreater(len(rows), 200)
        flagged = [row["locator"] for row in rows if quality.contradictions(row.get("text", ""))]
        self.assertEqual(flagged, [])


class SafetyTests(unittest.TestCase):
    def test_empty_answer_is_not_flagged(self):
        self.assertEqual(quality.problems("任何問題", ""), [])

    def test_ordinary_good_answer_passes(self):
        answer = "先看私訊到店率\n這 30 個裡面有幾個真的來"
        self.assertEqual(quality.problems("廣告成效不好", answer), [])

    def test_citation_number_is_not_content(self):
        """專家模式每一點都要附 `[1]`，那個數字不是他要的東西。

        不先剝掉的話「我陪你一起拆這個問題 [1]」會被當成「有給數字」而放行——
        整個守門對專家模式等於失效，而那正是它最該擋的一種空話。
        """
        for answer in (
            "我陪你一起拆這個問題",
            "我陪你一起拆這個問題 [1]",
            "我陪你一起拆這個問題[1]",
            "我陪你一起拆這個問題【1】",
        ):
            with self.subTest(answer=answer):
                self.assertTrue(quality.problems("廣告成效不好", answer), answer)

    def test_real_numbers_still_count_as_content(self):
        answer = "先看到店率 20% [1]\n低於基準就從邀約話術改起 [2]"
        self.assertEqual(quality.problems("廣告成效不好", answer), [])

    def test_followup_lines_are_not_content(self):
        """結尾那幾行「▷ 建議問題」是給前端做按鈕的，不是回答。

        它們天生有動詞跟數字（「那我該先改哪一步？」「我需要多少預算？」），
        整段一起送檢就會過關；但服務把追問拆走之後，使用者收到的正文只剩
        「我陪你一起拆這個問題」，狀態還是 answered。守門要驗他真的看到的東西。
        """
        answer = (
            "我陪你一起拆這個問題 [1]\n\n"
            "▷ 那我該先改哪一步？\n"
            "▷ 我需要多少預算？"
        )
        self.assertTrue(quality.problems("廣告成效不好", answer))

    def test_a_real_answer_with_followups_still_passes(self):
        answer = (
            "先看到店率 20% [1]\n低於基準就從邀約話術改起 [2]\n\n"
            "▷ 那我該先改哪一步？"
        )
        self.assertEqual(quality.problems("廣告成效不好", answer), [])

    def test_retry_note_carries_every_reason(self):
        found = ["第一個問題", "第二個問題"]
        note = quality.retry_note(found)
        self.assertIn("第一個問題", note)
        self.assertIn("第二個問題", note)
        self.assertEqual(quality.retry_note([]), "")


if __name__ == "__main__":
    unittest.main()
