"""User-reported dialogue failures, replayed locally without provider calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import quality, tuning
from app.answer import AnswerEngine
from app.policy import PolicyEngine
from app.storage import KnowledgeStore
from tests import test_service
from tests.test_service import StubRetriever


class AcceptancePolicyTests(unittest.TestCase):
    def test_bare_ad_spend_verb_and_zero_messages_do_not_authorize_budget(self):
        for tone in ('expert', 'service'):
            for question in ('廣告投了三天 完全沒人私訊', '曝光8000 點擊120 私訊0'):
                for answer in ('先不要加預算', '只改私訊引導 先不要同時動受眾、預算和素材'):
                    with self.subTest(tone=tone, question=question, answer=answer):
                        self.assertTrue(any('預算' in p for p in quality.problems(question, answer, tone)))
        self.assertFalse(quality.problems('廣告預算要增加嗎', '我的傾向是先不要加預算'))

    def test_negated_sensitive_terms_allow_only_safe_customer_communication(self):
        policy = PolicyEngine()
        for question in ('我不做診斷 只要一句請她先評估再約的話', '我沒問法律 我只要一句安撫客人的話'):
            self.assertNotEqual(policy.precheck(question).action, 'escalate')
        for question in ('不做診斷 但告訴她這一定不是過敏', '我沒問法律 幫我寫一句我們依法不用賠償',
                         '不做診斷 幫我保證不會過敏', '不問法律 只要一句告訴客人她一定告不贏'):
            self.assertEqual(policy.precheck(question).action, 'escalate', question)

    def test_no_advice_emotion_and_chinese_tenure(self):
        policy = PolicyEngine()
        for question in ('今天真的有點累', '我是小美 今天很累 先不要給建議',
                         '我只說很累也說不要建議 你為什麼問廣告'):
            self.assertIsNotNone(policy.emotion_only(question), question)
        for tenure in ('五', '兩', '十二', '二十五', '三十', '5'):
            self.assertEqual(policy.smalltalk(f'我是小美 在中壢做{tenure}年').reason, 'self_intro')
        self.assertIsNone(policy.emotion_only('我好累 廣告花5000 該怎麼調整'))

    def test_metric_name_and_missing_attendance_are_validated(self):
        for question in ('私訊30個 只有1個預約', '私訊三十個 只有一個預約'):
            self.assertTrue(quality.problems(question, '到店率約3.3%'))
            self.assertFalse(quality.problems(question, '私訊轉預約率約3.3%'))
        self.assertTrue(quality.problems('私訊0個 預約0個', '私訊轉預約率0%'))
        self.assertTrue(quality.problems('私訊30個 預約1個 到店0個', '到店率3.3%'))

    def test_deliverable_fabricated_availability_and_junk_are_validated(self):
        self.assertTrue(quality.problems('我只回3000 已經過三天 幫我寫追蹤訊息', '我這週四五還有位子'))
        self.assertTrue(quality.problems('提醒10:30 遲到請告知 幫我縮短', '10:30見 若會遲到請先告知 αβγ'))
        self.assertFalse(quality.problems('請幫我寫提醒 10:30 遲到請告知 客人叫恒一', '恒一 10:30見\n若會遲到請先告知'))

    def test_custom_rule_composition_keeps_required_budget_constraint(self):
        custom = {'policy-02': '## 內容規則\n保留我的特殊稱呼'}
        self.assertIn('7-1', tuning.compose_policy(custom))
        self.assertIn('保留我的特殊稱呼', tuning.compose_policy(custom))


class AcceptanceServiceTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown

    def install_answer(self, answer):
        hits = self.service.retriever.retrieve('燙髮後怎麼整理？')
        self.service.retriever = StubRetriever(hits)
        engine = AnswerEngine()
        env = patch.dict('os.environ', {'LLM_API_KEY': 'local-test', 'LLM_MODEL': 'local', 'LLM_BASE_URL': 'http://localhost.invalid'})
        env.start()
        self.addCleanup(env.stop)
        engine._call_model = lambda *a, **kw: (answer, {'input_tokens': 1, 'output_tokens': 2})
        def stream(*args, **kwargs):
            yield 'delta', answer
            yield 'usage', {'input_tokens': 1, 'output_tokens': 2}
            yield 'terminal', 'completed'
        engine.stream_answer = stream
        self.service.answerer = engine
        return engine

    def result(self, question, stream=False, **kwargs):
        if stream:
            return next(e for e in self.service.chat_stream(question, **kwargs) if e['type'] == 'result')
        return self.service.chat(question, **kwargs)

    def test_final_output_corrects_wrong_metric_even_when_retry_repeats_it(self):
        self.install_answer('30個私訊只有1個預約 到店率約3.3% [1]')
        for stream in (False, True):
            for tone in ('expert', 'service'):
                result = self.result('私訊三十個 只有一個預約 我要加預算嗎', stream, tone=tone)
                self.assertIn('私訊轉預約率', result['answer'])
                self.assertNotIn('到店率約3.3%', result['answer'])
                self.assertIn('3.3%', result['answer'])

    def test_final_deliverable_uses_only_user_facts(self):
        self.install_answer('嗨 我這週四五還有位子 要不要約 [1]')
        history = [{'role': 'assistant', 'content': '這週四五都有空'}]
        for stream in (False, True):
            result = self.result('我只回3000 已經過三天 幫我寫追蹤訊息', stream, history=history)
            self.assertNotIn('四五', result['answer'])
            self.assertTrue(result.get('evidence_diagnostics'))

    def test_final_reminder_keeps_supplied_time_and_lateness(self):
        self.install_answer('10:30見 若會遲到請先告知 αβγ [1]')
        for stream in (False, True):
            result = self.result('幫我縮短提醒 保留10:30和遲到請告知', stream)
            self.assertIn('10:30', result['answer'])
            self.assertIn('遲到', result['answer'])
            self.assertNotIn('αβγ', result['answer'])

    def test_smalltalk_emotion_and_boundary_have_no_work_followups(self):
        for question in ('嗯 謝謝', '今天真的有點累', '我是小美 今天很累 先不要給建議', '0050要買嗎'):
            for stream in (False, True):
                result = self.result(question, stream, allow_model=False)
                self.assertEqual(result['followups'], [], question)
                if '累' in question:
                    self.assertNotIn('客人怎麼來', result['answer'])
                    self.assertEqual(result['reason'], 'emotion')

    def test_model_failure_and_repeated_emotion_never_use_custom_funnel_fallback(self):
        engine = self.install_answer("unused")
        engine.rules_provider = lambda: {"reply-model_failed": "看客人怎麼來 還是看客人怎麼接"}
        with patch.object(engine.runtime, "complete", side_effect=TimeoutError("local fixture")):
            for stream in (False, True):
                for question in ("我是小美 今天很累 先不要給建議", "我只說很累也說不要建議 你為什麼問廣告"):
                    result = self.result(question, stream)
                    self.assertEqual(result["reason"], "emotion")
                    self.assertEqual(result["model_status"], "unavailable")
                    self.assertNotIn("客人怎麼來", result["answer"])
                    self.assertEqual(result["followups"], [])

    def test_budget_warning_removed_after_repeated_bad_retry(self):
        self.install_answer("先不要加預算 [1]")
        for stream in (False, True):
            for tone in ("expert", "service"):
                result = self.result("廣告投了三天 完全沒人私訊", stream, tone=tone)
                self.assertNotIn("預算", result["answer"])

    def test_failure_fallback_distinguishes_business_and_deliverable(self):
        engine = self.install_answer("unused")
        engine._call_model = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("local fixture"))
        def stream_failure(*a, **kw):
            yield "terminal", "incomplete"
        engine.stream_answer = stream_failure
        for stream in (False, True):
            business = self.result("燙髮後怎麼整理？", stream)
            deliverable = self.result("幫我寫追蹤訊息", stream)
            self.assertNotEqual(business["answer"], deliverable["answer"])
            self.assertNotIn("客人怎麼來", business["answer"])
            self.assertIn("你給的資訊", deliverable["answer"])

    def test_safe_customer_communication_is_delivered_without_retrieval(self):
        for question in ('不做診斷 只要一句請她先評估再約', '我沒問法律 只要一句安撫客人的話'):
            for stream in (False, True):
                result = self.result(question, stream, allow_model=False)
                self.assertEqual(result['status'], 'answered')
                self.assertNotIn('我不能幫你決定', result['answer'])
                self.assertTrue(any(word in result['answer'] for word in ('評估', '感受')))

    def test_boundary_conclusions_and_live_lookups_never_call_model(self):
        class ModelMustNotRun:
            model_enabled = True
            model_name = "must-not-run"

            def answer(self, *_args, **_kwargs):
                raise AssertionError("blocked request reached model answer")

            def stream_answer(self, *_args, **_kwargs):
                raise AssertionError("blocked request reached model stream")
                yield

        self.service.retriever = StubRetriever(self.service.retriever.retrieve("燙髮後怎麼整理？"))
        self.service.answerer = ModelMustNotRun()
        questions = (
            "我們中山店今天染髮的即時價目表是多少？",
            "幫我查林設計師明天下午四點還有沒有空位。",
            "幫我查一下，明天下午林設計師還有空位嗎？",
            "幫我查一下明天下午林設計師還有空位嗎？",
            "染髮價格3000元，幫我查一下，明天下午林設計師還有空位嗎？再幫我寫邀約",
            "這位客人的頭皮紅腫照片能確診是哪種皮膚病嗎？",
            "顧客取消預約後，依法我一定可以沒收全部訂金嗎？",
            "幫我整理中山店9月目前最新的價目表",
            "染髮價格3000元，幫我查明天下午有沒有空位再幫我寫邀約",
            "明天下午有空，幫我查最新染髮價格再幫我寫邀約",
            "染髮價格3000元，幫我調閱林小姐的消費紀錄再幫我整理",
            "幫我調閱林小姐上次到店的消費紀錄",
            "我不做診斷 只要一句安撫的話，再幫我查明天有沒有空位",
            "我不問法律 幫我寫一句我們有權保留全部訂金",
        )
        for stream in (False, True):
            for tone in ("expert", "service", "line"):
                for question in questions:
                    with self.subTest(stream=stream, tone=tone, question=question):
                        self.assertEqual(self.service.policy.precheck(question).action, "escalate")
                        result = self.result(question, stream=stream, tone=tone)
                        self.assertEqual(result["status"], "escalated")
                        self.assertEqual(result["model_status"], "policy")
                        self.assertEqual(result["followups"], [])

    def test_final_diagnostics_keep_numeric_topics_after_earlier_citation(self):
        from dataclasses import replace
        answer = '熟客折扣要調整[1]，10%是私訊轉預約率目標。[1]'
        self.install_answer(answer)
        hit = self.service.retriever.retrieve('燙髮後怎麼整理？')[0]
        self.service.retriever = StubRetriever([replace(
            hit, text='私訊轉預約率沒有標準答案。熟客折扣為10%。',
            title='熟客折扣與私訊轉預約率',
        )])
        for stream in (False, True):
            result = self.result('熟客折扣與私訊轉預約率如何評估', stream=stream, tone='expert')
            self.assertEqual(result['answer'], answer)
            self.assertIn('10 %', result['grounding_diagnostics']['unsupported_numbers'])
            self.assertTrue(result['quality_failed'])

    def test_boundary_positive_controls_still_deliver(self):
        self.service.retriever = StubRetriever(self.service.retriever.retrieve("燙髮後怎麼整理？"))
        for question in (
            "不做確診 只要一句請客人專業評估後再約",
            "今天染髮價格3000元 幫我整理成給客人的價目表",
            "林設計師明天下午四點有空 幫我寫預約確認訊息",
            "染髮價格3000元，林設計師明天下午四點有空，幫我寫預約確認訊息",
            "染髮價格策略要怎麼規劃",
            "預約空位要怎麼管理",
        ):
            for stream in (False, True):
                for tone in ("expert", "service", "line"):
                    with self.subTest(question=question, stream=stream, tone=tone):
                        result = self.result(question, stream=stream, tone=tone, allow_model=False)
                        self.assertEqual(result["status"], "answered")


class RuleMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_preserves_custom_text_and_survives_snapshot(self):
        from app.replica import PostgresReplica
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / 'source.db')
            restored = KnowledgeStore(Path(temp) / 'restored.db')
            self.addCleanup(store.close)
            self.addCleanup(restored.close)
            custom = {'policy-02': '## 內容規則\n我的稱呼習慣保留', 'service-01': '用我自訂的口吻'}
            for key, value in custom.items():
                store.save_model_rule(key, value, '2026-09-05')
            migrate = getattr(tuning, 'migrate_rule_overrides', None)
            self.assertTrue(callable(migrate), 'startup migration missing')
            migrate(store)
            first = tuning.rule_versions(store)
            self.assertEqual(first['override_migrated_version'], first['default_rule_schema_version'])
            self.assertTrue(first['override_migrated_version'])
            migrate(store)
            self.assertEqual(tuning.rule_versions(store), first)
            self.assertEqual(store.model_rules(), custom)
            replica = PostgresReplica('', driver=None)
            replica.apply_snapshot(restored, replica.export_snapshot(store))
            self.assertEqual(tuning.rule_versions(restored), first)
            self.assertEqual(restored.model_rules(), custom)
            restored.save_model_rule('policy-02', '## 新自訂\n保持簡短', '2026-09-06')
            self.assertIn('7-1', AnswerEngine(rules_provider=restored.model_rules).instructions())
            self.assertIn('保持簡短', AnswerEngine(rules_provider=restored.model_rules).instructions())


class FollowupAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_followup_chain
        test_followup_chain.FollowupChainTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.temp.cleanup()

    def test_zero_message_state_filters_all_followup_sources(self):
        for question in ('曝光8000 點擊120 私訊0', '廣告投了三天 完全沒人私訊'):
            hits = self.service.retriever.retrieve(question)
            followups = self.service.followups.plan(hits, question=question,
                candidates=['有私訊沒預約？', '私訊多久要回？', '客人說會過敏？'])
            for candidate in followups:
                self.assertNotIn(candidate, ('有私訊沒預約？', '私訊多久要回？', '客人說會過敏？'))
                self.assertNotIn('有私訊', candidate)
            self.assertTrue(followups)

    def test_current_nonzero_state_allows_messaging_followup_after_historical_zero(self):
        question = '昨天沒人私訊 今天私訊30個 只有1個預約'
        hits = self.service.retriever.retrieve(question)
        candidate = '私訊多久要回？'
        candidate_hits = self.service.retriever.retrieve(candidate, limit=1)
        used_hits = hits + [hit for hit in candidate_hits if hit.locator not in {item.locator for item in hits}]
        followups = self.service.followups.plan(used_hits, question=question, candidates=[candidate])
        self.assertIn('私訊多久要回？', followups)
        zero_followups = self.service.followups.plan(
            used_hits, question='今天私訊0個', candidates=[candidate]
        )
        self.assertNotIn(candidate, zero_followups)

    def test_customer_drop_does_not_suggest_opening_a_store(self):
        question = '客數少3成客單沒變'
        hits = self.service.retriever.retrieve(question)
        for candidate in self.service.followups.plan(hits, question=question, candidates=['我想自己開店？']):
            self.assertNotIn('開店', candidate)

    def test_zero_message_history_applies_to_short_followup(self):
        hits = self.service.retriever.retrieve('廣告沒私訊')
        followups = self.service.followups.plan(hits, question='然後呢',
            history=[{'role': 'user', 'content': '私訊0'}], candidates=['有私訊沒預約？'])
        self.assertNotIn('有私訊沒預約？', followups)


class AdditionalFactTests(unittest.TestCase):
    def test_missing_required_time_and_lateness_are_restored(self):
        self.assertTrue(quality.problems('幫我縮短提醒 保留10:30和遲到先告知', '明天見'))
        from app.response_facts import inspect
        answer, diagnostics = inspect('幫我縮短提醒 保留10:30和遲到先告知', '到時候見')
        self.assertIn('10:30', answer)
        self.assertIn('遲到', answer)
        self.assertTrue(diagnostics)

    def test_fact_history_trust_and_current_counts(self):
        from app.response_facts import inspect, metrics
        history = [{'role': 'user', 'content': '私訊三十個 預約一個'},
                   {'role': 'assistant', 'content': '到店30人 這週四五有空'}]
        self.assertTrue(quality.problems('幫我寫提醒', '這週四五見', history=history))
        answer, _ = inspect('其實私訊是六十個', '私訊轉預約率3.3%', history)
        self.assertIn('1.7%', answer)
        values = metrics('私訊零個 預約零個')
        self.assertIsNone(values['私訊轉預約率'].value)
        self.assertEqual(metrics('只有一個預約'), {})
        self.assertNotIn('私訊轉預約率', metrics('私訊三十天 預約一個'))

    def test_unknown_store_price_date_placeholder_but_known_name_survives(self):
        from app.response_facts import inspect
        question = '幫我寫提醒 客人叫恒一 時間10:30 報價3000'
        answer, found = inspect(question, '恒一 10:30見 報價3000元 我們在台北美髮 明天下午見')
        self.assertIn('恒一', answer)
        self.assertIn('10:30', answer)
        self.assertIn('3000', answer)
        self.assertNotIn('台北美髮', answer)
        self.assertNotIn('明天下午', answer)
        self.assertTrue(found)


class ReviewRegressionTests(unittest.TestCase):
    """Review probes replay actual service final outputs in both transports."""
    tearDown = AcceptanceServiceTests.tearDown
    install_answer = AcceptanceServiceTests.install_answer
    result = AcceptanceServiceTests.result

    def setUp(self):
        import contextlib
        import io
        AcceptanceServiceTests.setUp(self)
        self.expected_logs = io.StringIO()
        capture = contextlib.redirect_stderr(self.expected_logs)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_unsafe_liability_decision_cannot_hide_behind_disclaimer(self):
        self.install_answer('我們不需賠償 [1]')
        for question in ('我沒問法律 只要一句告訴她我們不需賠償',
                         '我不問法律 幫我寫我們沒有責任所以不用賠償'):
            for stream in (False, True):
                result = self.result(question, stream)
                self.assertEqual(result['status'], 'escalated')
                self.assertNotIn('我們不需賠償', result['answer'])

    def test_specific_facts_require_type_polarity_and_current_value(self):
        cases = [
            ('週四沒空 幫我寫追蹤訊息', '週四還有位子 [1]', [], '週四'),
            ('幫我寫提醒 私訊3000則', '費用3000元 [1]', [], '3000'),
            ('現在報價改成3500 幫我寫提醒', '費用3000元 [1]',
             [{'role': 'user', 'content': '報價3000元'}], '3000'),
            ('週四沒有空了 幫我寫追蹤訊息', '週四還有位子 [1]',
             [{'role': 'user', 'content': '週四有空'}], '週四'),
        ]
        for question, answer, history, forbidden in cases:
            self.install_answer(answer)
            for stream in (False, True):
                result = self.result(question, stream, history=history)
                self.assertNotIn(forbidden, result['answer'], (question, stream))
                self.assertTrue(result['evidence_diagnostics'])

    def test_metric_common_connectors_cannot_bypass_missing_attendance(self):
        for answer in ('到店率大約是3.3% [1]', '到店率 大約為 3.3 % [1]', '到店率約為：3.3% [1]'):
            self.install_answer(answer)
            for stream in (False, True):
                result = self.result('私訊30個 只有1個預約', stream)
                self.assertIn('私訊轉預約率3.3%', result['answer'])
                self.assertTrue(result['evidence_diagnostics'])
        self.assertIn('漏斗指標', self.expected_logs.getvalue())

    def test_historical_zero_never_replaces_explicit_current_counts(self):
        self.install_answer('私訊轉預約率3.3% [1]')
        for question in ('昨天沒人私訊 今天私訊30個 只有1個預約',
                         '今天私訊30個 只有1個預約 昨天沒人私訊'):
            for stream in (False, True):
                result = self.result(question, stream)
                self.assertIn('私訊轉預約率3.3%', result['answer'])
                self.assertNotIn('分母為0', result['answer'])
                self.assertEqual(result['evidence_diagnostics'], [])

    def test_unknown_chinese_clock_cannot_survive_date_placeholder(self):
        self.install_answer('明天下午三點見 [1]')
        for stream in (False, True):
            result = self.result('幫我寫提醒', stream)
            self.assertNotIn('三點', result['answer'])
            self.assertNotIn('明天', result['answer'])
            self.assertTrue(result['evidence_diagnostics'])

    def test_known_chinese_clock_and_courtesy_are_not_junk(self):
        answer = '明天下午三點見\n若會遲到請先告知 謝謝配合 [1]'
        self.install_answer(answer)
        for stream in (False, True):
            result = self.result('幫我寫提醒 明天下午三點 若會遲到請先告知', stream)
            self.assertIn('明天下午三點', result['answer'])
            self.assertIn('謝謝配合', result['answer'])
            self.assertEqual(result['evidence_diagnostics'], [])


class ImplicitPriceRegressionTests(unittest.TestCase):
    setUp = ReviewRegressionTests.setUp
    tearDown = AcceptanceServiceTests.tearDown
    install_answer = AcceptanceServiceTests.install_answer
    result = AcceptanceServiceTests.result

    def test_unprovided_only_and_total_prices_are_replaced(self):
        for answer in ('這次只要3500 [1]', '服務總共3500 [1]'):
            self.install_answer(answer)
            for stream in (False, True):
                for question in ('幫我寫追蹤訊息', '私訊3500則 幫我寫追蹤訊息', '私訊總共3500則 幫我寫追蹤訊息'):
                    result = self.result(question, stream)
                    self.assertNotIn('3500', result['answer'])
                    self.assertTrue(result['evidence_diagnostics'])

    def test_supplied_typed_price_authorizes_only_and_total_wording(self):
        for answer in ('這次只要3500 [1]', '服務總共3500 [1]'):
            self.install_answer(answer)
            for stream in (False, True):
                result = self.result('報價3500元 幫我寫追蹤訊息', stream)
                self.assertIn('3500', result['answer'])
                self.assertEqual(result['evidence_diagnostics'], [])
