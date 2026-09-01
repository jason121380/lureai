"""閒聊、情緒、欲言又止不進檢索——回話才不會硬邦邦。"""
import unittest

from app.policy import PolicyEngine


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.policy = PolicyEngine()

    def route(self, message: str) -> str:
        decision = self.policy.smalltalk(message) or self.policy.emotion_only(message)
        return decision.reason if decision else "rag"

    def test_greetings_and_acknowledgements_skip_retrieval(self):
        for message in ("哈囉", "嗨～", "嗨嗨", "謝謝", "謝謝你", "好的", "收到",
                        "ok", "晚安", "辛苦了", "掰掰", "嗯", "了解", "好喔"):
            with self.subTest(message=message):
                self.assertEqual(self.route(message), "smalltalk")

    def test_hesitation_gets_a_gentle_follow_up(self):
        for message in ("算了 沒事", "沒事", "當我沒說", "沒什麼"):
            with self.subTest(message=message):
                self.assertEqual(self.route(message), "hesitation")

    def test_pure_venting_is_acknowledged_not_researched(self):
        for message in ("好累", "壓力好大", "想放棄", "我最近很累 提不起勁",
                        "這個時段我本來可以接別的客人 有點不爽"):
            with self.subTest(message=message):
                self.assertEqual(self.route(message), "emotion")

    def test_emotion_with_a_question_still_uses_the_knowledge_base(self):
        """他自己問「該怎麼調」就是要方法了，這時候才去拿知識。"""
        for message in ("我好累 該怎麼調整", "壓力好大 要怎麼處理", "想放棄了 我還能做什麼"):
            with self.subTest(message=message):
                self.assertEqual(self.route(message), "rag")

    def test_real_questions_are_untouched(self):
        for message in ("客人說太貴怎麼接", "我的私訊很多但預約很少", "廣告一個月要抓多少預算",
                        "了解客人需求要問什麼", "在嗎 我想問廣告", "早安 今天要看什麼"):
            with self.subTest(message=message):
                self.assertEqual(self.route(message), "rag")

    def test_identity_question_still_hits_the_boundary_reply(self):
        self.assertIsNone(self.policy.smalltalk("你是ai嗎"))
        self.assertEqual(self.policy.boundary_reply("你是ai嗎").reason, "identity")


if __name__ == "__main__":
    unittest.main()
