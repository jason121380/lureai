"""體檢 battery：設計師真的會打的話，分流與品質守門要判對。

這兩組是 2026-09-02 體檢報告的量測基準，數字直接對應報告第 8 節：
- 分流召回率：128 句實際訊息，舊版只對 64.8%（39 句閒聊只認出 5 句）。
- 品質守門：18 則好回答不可以被擋、8 則壞回答不可以放過。

改分流詞表或守門規則之前先跑這裡；擋掉一句正常的話比放過一句廢話更傷。
"""
import unittest

from app import quality
from app.policy import PolicyEngine
from run import PROFILES


# (訊息, 應該走哪一條路)
ROUTING_CASES = (
    # --- 打招呼、應聲、道謝、收尾 ---
    ("哈囉", "smalltalk:smalltalk"),
    ("嗨嗨 教練", "smalltalk:smalltalk"),
    ("早安～", "smalltalk:smalltalk"),
    ("謝謝你 很有幫助", "smalltalk:smalltalk"),
    ("好 我試試看", "smalltalk:smalltalk"),
    ("好的 我知道了", "smalltalk:smalltalk"),
    ("了解 謝謝", "smalltalk:smalltalk"),
    ("ok 那我先去做", "smalltalk:smalltalk"),
    ("收到！", "smalltalk:smalltalk"),
    ("哈哈", "smalltalk:smalltalk"),
    ("😂", "smalltalk:smalltalk"),
    ("👍", "smalltalk:smalltalk"),
    ("嗯嗯", "smalltalk:smalltalk"),
    ("對", "smalltalk:smalltalk"),
    ("對啊", "smalltalk:smalltalk"),
    ("是喔", "smalltalk:smalltalk"),
    ("真的假的", "smalltalk:smalltalk"),
    ("蛤", "smalltalk:smalltalk"),
    ("哦", "smalltalk:smalltalk"),
    ("好喔 那我等一下弄", "smalltalk:smalltalk"),
    ("我先去忙 晚點回你", "smalltalk:smalltalk"),
    ("辛苦了 教練 那我先去忙", "smalltalk:smalltalk"),
    ("在嗎", "smalltalk:smalltalk"),
    ("在嗎 教練", "smalltalk:smalltalk"),
    ("教練在嗎", "smalltalk:smalltalk"),
    ("請問在嗎", "smalltalk:smalltalk"),
    ("可以問一下嗎", "smalltalk:smalltalk"),
    ("我可以問個問題嗎", "smalltalk:smalltalk"),
    ("不好意思打擾了", "smalltalk:smalltalk"),
    ("晚安 明天再聊", "smalltalk:smalltalk"),
    ("先這樣 謝謝", "smalltalk:smalltalk"),
    ("好 謝謝", "smalltalk:smalltalk"),
    ("好的謝謝你", "smalltalk:smalltalk"),
    # --- 報喜：撈知識只會撈到「沒來的客人怎麼處理」，等於潑他冷水 ---
    ("太棒了", "smalltalk:smalltalk"),
    ("有效耶", "smalltalk:smalltalk"),
    ("成功了 客人約了", "smalltalk:smalltalk"),
    ("客人回我了！", "smalltalk:smalltalk"),
    ("我照你說的做 客人真的回了", "smalltalk:smalltalk"),
    # --- 情緒：只承接，不要派任務 ---
    ("今天好忙", "smalltalk:emotion"),
    ("今天客人好多 累死我了", "smalltalk:emotion"),
    ("好累 不想做了", "smalltalk:emotion"),
    ("今天被客人罵 心情很差", "smalltalk:emotion"),
    ("這個月業績好爛", "smalltalk:emotion"),
    ("客人又放我鴿子 氣死", "smalltalk:emotion"),
    ("我超煩的 助理一直做錯", "smalltalk:emotion"),
    ("我最近很焦慮 客人一直流失", "smalltalk:emotion"),
    ("我想放棄了", "smalltalk:emotion"),
    ("好煩 客人一直殺價", "smalltalk:emotion"),
    ("店長說我業績不夠 壓力好大", "smalltalk:emotion"),
    # 帶數字就不是單純抒發：他是拿數字問你怎麼看。
    ("有點沮喪 廣告花了五千只來一個", "rag"),
    ("我好累 該怎麼調整", "rag"),
    ("我最近很焦慮 客人一直流失 該怎麼辦", "rag"),
    # --- 欲言又止 ---
    ("算了", "smalltalk:hesitation"),
    ("算了 沒事", "smalltalk:hesitation"),
    ("沒事 我再想想", "smalltalk:hesitation"),
    # --- 自我介紹 ---
    ("我叫阿明 在中壢做五年", "smalltalk:self_intro"),
    ("我是新人 剛升設計師半年", "smalltalk:self_intro"),
    ("我是小美", "smalltalk:self_intro"),
    ("我在台中 店裡有五個設計師", "smalltalk:self_intro"),
    # --- 真的問題 ---
    ("私訊很多但沒人來", "rag"),
    ("客人問價格就消失", "rag"),
    ("廣告要投多少", "rag"),
    ("一週要發幾篇", "rag"),
    ("客人說太貴", "rag"),
    ("我想漲價", "rag"),
    ("你覺得我該不該漲價", "rag"),
    ("客人要退費怎麼辦", "rag"),
    ("客人染壞要我賠", "rag"),
    ("客人頭皮過敏怎麼辦", "rag"),
    ("我想離職 老闆要我賠違約金", "rag"),
    ("幫我看一下這則私訊怎麼回：嗨 請問染髮多少", "rag"),
    ("你是誰", "rag"),
    ("你到底是誰在回", "rag"),
    ("我的到店率 20%", "rag"),
    ("到店率 8% 正常嗎", "rag"),
    ("然後呢", "rag"),
    ("再短一點", "rag"),
    ("有沒有範例", "rag"),
    ("可以給我三個開場白嗎", "rag"),
    ("我不太會講話", "rag"),
    ("我很內向 不敢跟客人聊天", "rag"),
    ("謝謝 那廣告預算怎麼抓", "rag"),
    ("hi 我想問私訊", "rag"),
    ("不是這樣的", "rag"),
    ("你說錯了吧", "rag"),
    ("你確定嗎", "rag"),
    ("我不同意", "rag"),
    ("那第一步要做什麼", "rag"),
    ("這句話可以怎麼改", "rag"),
    ("幫我改得口語一點", "rag"),
    ("換一個", "rag"),
    ("還有嗎", "rag"),
    ("還有其他的嗎", "rag"),
    ("除了這個呢", "rag"),
    ("為什麼", "rag"),
    ("為什麼要這樣做", "rag"),
    ("這樣真的有用嗎", "rag"),
    ("我不懂", "rag"),
    ("看不懂", "rag"),
    ("什麼意思", "rag"),
    ("你可以講白話一點嗎", "rag"),
    ("我最近想投資自己去上課", "rag"),
    ("我覺得我是不是不適合這行", "rag"),
    ("這個月業績掉了三成", "rag"),
    ("客人放鳥我", "rag"),
    ("客人問能不能刷卡", "rag"),
    ("我想跟客人要 line 可以嗎", "rag"),
    ("客人要我留電話給他", "rag"),
    ("幫我預約明天的客人", "rag"),
    ("幫我寫一則假日促銷文案", "rag"),
    ("客人說我的評論是假的", "rag"),
    ("客人下雨天都不來 淡季怎麼辦", "rag"),
    # --- 邊界題：整句就是那件事才算 ---
    ("你是真人嗎", "boundary:identity"),
    ("你是AI嗎", "boundary:identity"),
    ("你根本不懂我", "boundary:hostile"),
    ("這個沒有幫助", "boundary:hostile"),
    ("你在講什麼 我聽不懂", "boundary:hostile"),
    ("可以幫我寫假評論嗎", "boundary:illegitimate_request"),
    ("客人問 0050 好不好", "boundary:off_topic"),
    ("客人跟我聊比特幣 我要怎麼接話", "boundary:off_topic"),
    # 同樣的字出現在正常的輔導問題裡就不可以誤傷。
    ("剛剛那個回答沒有幫助到我 可以換個方向嗎", "rag"),
    ("客人問我你在講什麼 我要怎麼解釋燙髮流程", "rag"),
    ("今天天氣好熱 客人都不出門", "rag"),
    ("我想寫一篇關於政治正確的貼文", "rag"),
    # --- 只有人能決定的事 ---
    ("客人說要告我", "sensitive:legal_refund_or_compensation"),
    ("客人問我頭皮紅腫要不要看醫生", "sensitive:health_or_medical"),
    ("店長要資遣我", "sensitive:labor_hr"),
    ("客人的信用卡刷不過怎麼辦", "sensitive:personal_or_payment"),
)

# 這些是照著規則寫出來的好回答，一則都不可以被擋——擋掉會變成降級訊息。
GOOD_ANSWERS = (
    ("私訊多但預約少", "私訊多但沒人來 了解\n我們先看一個數字\n\n這 30 個裡面 有幾個真的來店～", "service"),
    ("廣告成效不好", "喔喔 沒關係！\n我幫你看一下\n\n你這檔廣告 花了多少 有幾個人私訊～", "service"),
    ("客人說太貴", "我懂 這個價位確實不低\n\n可以這樣回他\n差別在藥水跟時間\n你的底色要退兩次才會乾淨\n\n要不要我幫你改成你的口氣～", "service"),
    ("我到店率 20%", "20% 剛好及格唷\n十個問 兩個來 是正常的\n\n接下來要拚的是量\n\n你這個月私訊大概幾則～", "service"),
    ("你覺得我該不該漲價", "我的傾向是先不要漲\n\n你回流率 25% 還沒到 30%\n先把售後做起來\n\n你手邊有三個月的回流數字嗎～", "service"),
    ("客人要退費", "染壞要退費 先別慌\n第一步是把問題掌握清楚再道歉\n\n超出職權的部分請主管出面\n你先確認是顏色不對還是受損\n\n客人是說顏色不對 還是頭髮壞掉～", "service"),
    ("這週四五有空 幫我寫一則追蹤訊息", "好 直接用這個\n嗨～上次你問的霧感染\n我這週四五還有位子\n你大概什麼時候方便\n\n要不要一起改成你的語氣～", "service"),
    ("客人說再看看", "好唷 不急\n想的時候可以先想一件事\n你是想一次到位 還是先做一次看看\n\n這句直接傳給他就好唷", "service"),
    ("我好累 該怎麼調整", "這樣真的很累呀\n\n先把這週每天的下班時間記下來\n看是客人拖延 還是下班後的事太多\n\n你比較像哪一種～", "service"),
    ("新人前七天要怎麼帶", "前七天先不要教技術 先教流程\n\n第 1 天帶他看接待\n第 3 天讓他做洗頭\n第 7 天請主管一起檢視\n\n你的新人是完全沒經驗的嗎～", "service"),
    ("我想寫得自然一點", "可以把「您」改成「你」\n把「請問」拿掉\n\n嗨 上次染的顏色還喜歡嗎\n這週有空的話我幫你補個色\n\n這樣的口氣你覺得呢～", "service"),
    ("客人約好沒來", "客人沒來 先不要生氣\n\n傳這句就好\n嗨～今天等你到 3 點\n是不是臨時有事\n\n他之後有回你嗎～", "service"),
    ("私訊多但預約少", "先看私訊到店率，這 30 則裡真的來店的有幾個 [1]\n- 算出到店率，20% 以上才及格 [1]\n- 低於 10% 代表對話沒接住，先不要加預算 [2]\n- 抽 20 則對話用同一套評分看哪一段卡住 [3]", "expert"),
    ("你覺得我該不該漲價", "我的傾向是可以漲，一次抓 5-10% [1]\n- 先確認回流率有沒有 30% 以上 [2]\n- 提前一個月告知熟客，給一次原價 [3]", "expert"),
    ("你說錯了吧", "我再確認一次：你說的是回流率不是到店率，我剛剛把兩個混在一起講了，抱歉。回流率的健康區間是 30-40% [1]，你的 25% 還沒到，所以我的建議還是先做售後再談漲價 [2]。", "expert"),
    ("我可以漲價嗎", "可以，但這個階段先漲新客就好，熟客等回流率到 30% 再一起調 [1]", "expert"),
    ("這週四五有空 給我三個開場白", "- 嗨嗨 染髮看長度跟現在的底色 你可以拍一張現在的髮色給我嗎 [1]\n- 哈囉 上次的顏色現在還好嗎 這週有空我幫你補一下色 [2]\n- 嗨～上次你問的霧感染 我這週四五還有位子 [3]", "expert"),
    ("健檢要抽哪 20 則對話來看？", "抽最近一個月的，成交 10 則、沒成交 10 則 [1]\n- 成交與沒成交各半，才看得出差在哪 [1]\n- 不要挑最好的，挑隨機的 [2]", "expert"),
)

# 這些是實測抓到的失分樣態，一則都不可以放過。
BAD_ANSWERS = (
    ("廣告成效不好", "沒關係 我陪你一起看\n\n我們慢慢來\n\n你想先從哪邊聊～", "service"),
    ("幫我寫開場白", "好 我幫你寫一版\n\n你想要親切一點 還是專業一點～", "service"),
    ("給我十個 hashtag", "#台北染髮 #霧感染\n\n其他你可以自己延伸唷", "service"),
    ("你覺得我該不該漲價", "漲價要看很多面向唷\n回流率 指名率 預約滿不滿\n\n你現在回流率多少～", "service"),
    ("客人要退費", "這個我幫你轉人工處理唷\n\n會有專人跟你聯繫", "service"),
    ("你說錯了吧", "抱歉 是我說錯了\n你說的才對", "service"),
    ("我可以漲價嗎", "建議漲價 5-10%\n不過現在不要漲價 先做回流", "service"),
    ("私訊多但預約少", "私訊多但預約少通常是因為報價太快、沒有問需求、沒有主動約時間，這三件事會讓客人問完價格就離開，你可以先抽二十則對話出來看看是哪一段卡住再決定要改哪一件事", "service"),
)


class RoutingBatteryTests(unittest.TestCase):
    """設計師實際會打的 128 句話，每一句都要走對路。"""

    @classmethod
    def setUpClass(cls):
        cls.policy = PolicyEngine(blocked_topics=PROFILES["designer_coach"]["blocked_topics"])

    def route(self, message: str) -> str:
        boundary = self.policy.boundary_reply(message)
        if boundary:
            return f"boundary:{boundary.reason}"
        chatty = self.policy.smalltalk(message) or self.policy.emotion_only(message)
        if chatty:
            return f"smalltalk:{chatty.reason}"
        precheck = self.policy.precheck(message)
        if precheck.action == "escalate":
            return f"sensitive:{precheck.reason}"
        return "rag"

    def test_every_message_takes_the_right_path(self):
        for message, expected in ROUTING_CASES:
            with self.subTest(message=message):
                self.assertEqual(self.route(message), expected)

    def test_small_talk_recall_stays_high(self):
        """報告第 8 節的指標：閒聊召回率 ≥ 90%（改版前是 13%）。"""
        chatty = [case for case in ROUTING_CASES if case[1].startswith("smalltalk:")]
        caught = sum(1 for message, expected in chatty if self.route(message) == expected)

        self.assertGreaterEqual(caught / len(chatty), 0.9)

    def test_no_ordinary_question_is_mistaken_for_a_boundary_reply(self):
        """邊界誤傷率要是 0：正常的輔導問題不可以收到固定婉拒句。"""
        wrongly_blocked = [
            message for message, expected in ROUTING_CASES
            if expected == "rag" and self.route(message).startswith("boundary:")
        ]

        self.assertEqual(wrongly_blocked, [])


class QualityBatteryTests(unittest.TestCase):
    def test_no_good_answer_is_blocked(self):
        blocked = [
            (question, quality.problems(question, answer, tone=tone))
            for question, answer, tone in GOOD_ANSWERS
            if quality.problems(question, answer, tone=tone)
        ]

        self.assertEqual(blocked, [])

    def test_every_known_failure_is_caught(self):
        missed = [
            question for question, answer, tone in BAD_ANSWERS
            if not quality.problems(question, answer, tone=tone)
        ]

        self.assertEqual(missed, [])

    def test_a_number_in_the_question_is_not_a_shopping_list(self):
        """「抽 20 則對話」不是「列 20 項」——舊版每一次都白白重打一次。"""
        for question in ("健檢要抽哪 20 則對話來看？", "20 則對話記完之後我要看什麼？",
                         "私訊 30 則但沒人來", "這週來了 3 個客人"):
            with self.subTest(question=question):
                self.assertEqual(quality._requested_count(question), 0)

    def test_an_actual_request_still_counts(self):
        self.assertEqual(quality._requested_count("給我十個 hashtag"), 10)
        self.assertEqual(quality._requested_count("可以給我三個開場白嗎"), 3)


class RetrievalGuardTests(unittest.TestCase):
    """問法索引是人工標註的「這句話問的是哪一塊」，整句對上就要贏。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path

        from app.ingest import ingest_jsonl
        from app.retrieval import Retriever
        from app.storage import KnowledgeStore

        root = Path(__file__).resolve().parents[1]
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = KnowledgeStore(Path(cls.temp.name) / "battery.db")
        ingest_jsonl(
            cls.store,
            root / "knowledge" / "designer_coaching_process.jsonl",
            expected_access_level="internal_coaching",
        )
        cls.retriever = Retriever(cls.store)
        cls.policy = PolicyEngine()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.temp.cleanup()

    def test_a_hand_written_phrasing_finds_its_own_chunk(self):
        for question, locator in (
            ("私訊多久要回", "chat-04"),
            ("要不要加 emoji", "chat-07"),
            ("二選一怎麼問", "chat-10"),
            ("20 則怎麼抽", "chat-02"),
            ("廣告一天要投多少", "ads-04"),
            ("客人說太貴了", "chat-16"),
            ("回流率多少算正常", "metric-02"),
            ("新人怎麼帶", "ops-35"),
        ):
            with self.subTest(question=question):
                hits = self.retriever.retrieve(question, limit=3)
                self.assertTrue(hits, question)
                self.assertEqual(hits[0].locator, locator, [h.locator for h in hits])

    def test_unrelated_questions_stay_below_the_confidence_line(self):
        """加重問法索引之後，不相關的題目一樣不可以爬過 0.72。"""
        for question in ("明天天氣如何", "台積電要買嗎", "幫我寫一段 python", "晚餐吃什麼"):
            with self.subTest(question=question):
                hits = self.retriever.retrieve(question, limit=1)
                top = hits[0].score if hits else 0.0
                self.assertLess(top, self.policy.minimum_score, f"{question} -> {top}")

    def test_follow_ups_stay_on_the_topic_that_was_just_answered(self):
        """使用者回報：問「我不會賣產品要怎麼開口」，三個建議全在問開店。

        原因是相鄰知識的候選池每一輪都整份轉一格，把唯一那筆同分類的
        產品銷售知識轉走，第一個候選就掉到職涯手冊開頭的開店評估。
        """
        from app.followups import FollowupPlanner

        planner = FollowupPlanner(self.store, self.retriever, self.policy)
        question = "我不會賣產品要怎麼開口？"
        hits = [
            hit for hit in self.retriever.retrieve(question, limit=8)
            if hit.score >= self.policy.minimum_score
        ][:4]

        picked = planner.plan(hits, asked={question}, question=question)

        self.assertTrue(picked)
        for suggestion in picked:
            with self.subTest(suggestion=suggestion):
                self.assertNotIn("開店", suggestion)
                self.assertNotIn("毛髮構造", suggestion)
        self.assertTrue(
            any("推銷" in item or "產品" in item for item in picked), picked
        )

    def test_an_off_topic_candidate_is_rejected_even_when_it_is_answerable(self):
        """「答得出來」跟「跟這一輪有關」是兩件事。

        知識庫裡幾乎每一題都答得出來，所以只驗前者等於沒驗——實測問
        「我不會賣產品要怎麼開口」，模型寫的「自己開店」「毛髮構造」
        「廣告投多少」三題全部通過。
        """
        question = "我不會賣產品要怎麼開口"
        hits = self.retriever.retrieve(question, limit=4)
        off_topic = [
            "我想自己開店要準備什麼？",
            "毛髮構造有哪三種鏈鍵？",
            "廣告一天要投多少錢？",
        ]
        from app.followups import FollowupPlanner

        planner = FollowupPlanner(self.store, self.retriever, self.policy)
        for candidate in off_topic:
            with self.subTest(candidate=candidate):
                # 前提：這些題目本身都答得出來，所以擋掉它們的一定是相關性。
                self.assertTrue(planner._answerable(candidate), candidate)

        picked = planner.plan(hits, candidates=off_topic, question=question)

        for candidate in off_topic:
            self.assertNotIn(candidate, picked)
        self.assertTrue(picked, "擋掉離題的之後仍要給得出建議")
        joined = " ".join(picked)
        for banned in ("開店", "毛髮", "鏈鍵"):
            self.assertNotIn(banned, joined, picked)

    def test_a_thin_topic_still_suggests_the_closest_thing(self):
        """分類只有一兩塊、又都被當成來源用掉時，長尾要先給最接近的。

        「訂金與爽約」整個分類只有 2 塊知識，兩塊都是這一題的來源，所以同分類
        鄰居一個都不剩。舊版直接落到整份語料的輪替，冒出「開店損益兩平」。
        """
        question = "訂金要收多少"
        hits = self.retriever.retrieve(question, limit=4)

        from app.followups import FollowupPlanner

        planner = FollowupPlanner(self.store, self.retriever, self.policy)
        picked = planner.plan(hits, question=question)

        self.assertTrue(picked)
        self.assertNotIn("開店", " ".join(picked), picked)

    def test_follow_ups_do_not_repeat_the_same_chunk(self):
        """三個建議全出自同一塊知識等於只給了一個選擇。"""
        from app.followups import FollowupPlanner

        planner = FollowupPlanner(self.store, self.retriever, self.policy)
        question = "廣告一天要投多少錢才夠？"
        hits = [
            hit for hit in self.retriever.retrieve(question, limit=8)
            if hit.score >= self.policy.minimum_score
        ][:4]

        picked = planner.plan(hits, asked={question}, question=question)

        self.assertEqual(len(picked), len(set(picked)))
        self.assertGreaterEqual(len(picked), 2)

    def test_the_alias_index_is_stored_one_phrase_per_line(self):
        """整句比對靠換行分隔；用空白接起來就分不出問法的邊界。"""
        row = self.store.connection.execute(
            "SELECT aliases FROM chunks WHERE locator = 'chat-04'"
        ).fetchone()

        phrases = [line for line in str(row["aliases"]).split("\n") if line.strip()]
        self.assertIn("私訊多久要回", phrases)


if __name__ == "__main__":
    unittest.main()
