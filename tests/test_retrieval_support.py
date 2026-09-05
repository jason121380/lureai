"""Source evidence must survive generic templates, corpus edits and conversation history."""
import json
import tempfile
import unittest
from pathlib import Path

from app.answer import AnswerEngine
from app.ingest import ingest_jsonl, _search_text
from app.policy import PolicyEngine
from app.retrieval import Retriever, SearchHit, evidence_question
from app.service import CustomerService
from app.storage import KnowledgeStore
from tests.test_ingest import approved_chunk


class SourceSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root / 'test.db')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.rows = [approved_chunk(
            chunk_id=f'row-{i}', locator=f'row-{i}', title=subject,
            section_title=subject, source_file='knowledge/synthetic.md',
            text=f'{subject} 分析狀況 確認步驟 提供方法 檢查結果',
            aliases=['分析狀況有哪些步驟', '請提供方法檢查結果'],
        ) for i, subject in enumerate(('預約', '燙髮', '回流', '染髮', '薪資', '廣告'))]
        self.write_rows(self.rows)
        self.retriever = Retriever(self.store)
        self.policy = PolicyEngine()

    def write_rows(self, rows):
        source = self.root / 'synthetic.jsonl'
        source.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows))
        ingest_jsonl(self.store, source)

    def test_shared_boilerplate_and_alias_templates_cannot_authorize_unknown_subject(self):
        hits = self.retriever.retrieve('軌道週期分析狀況有哪些步驟請提供方法檢查結果')
        self.assertNotEqual(self.policy.evaluate(hits).action, 'answer')

    def test_rare_subject_beats_larger_shared_boilerplate_overlap(self):
        self.rows[1]['text'] = '燙髮 請使用冷風吹乾髮根'
        self.write_rows(self.rows)
        hits = self.retriever.retrieve('燙髮分析狀況有哪些步驟請提供方法檢查結果')
        self.assertEqual(hits[0].locator, 'row-1')
        self.assertEqual(self.policy.evaluate(hits).action, 'answer')

    def test_partial_aliases_cannot_erase_unsupported_original_subject(self):
        query = '軌道週期訊號頻譜量測燙髮'
        before = self.retriever.retrieve(query)
        self.assertNotEqual(self.policy.evaluate(before).action, 'answer')
        for row in self.rows:
            row['aliases'].append('軌道週期訊號頻譜量測')
        self.write_rows(self.rows)
        after = self.retriever.retrieve(query)
        self.assertNotEqual(self.policy.evaluate(after).action, 'answer')
        self.assertLessEqual(after[0].score, before[0].score)
        self.assertEqual(self.policy.evaluate(self.retriever.retrieve('燙髮')).action, 'answer')
        self.assertEqual(self.policy.evaluate(self.retriever.retrieve('分析狀況有哪些步驟')).action, 'answer')

    def test_nonexact_alias_frequency_cannot_promote_an_unrelated_cost_source(self):
        self.rows[1]['text'] = '燙髮 估算成本'
        self.write_rows(self.rows)
        query = '太陽系行星軌道週期長期變化估算成本'
        before = self.retriever.retrieve(query)
        self.assertNotEqual(self.policy.evaluate(before).action, 'answer')
        for row in self.rows:
            row['aliases'].append('太陽系行星軌道週期長期變化有哪些步驟')
        self.write_rows(self.rows)
        after = self.retriever.retrieve(query)
        self.assertNotEqual(self.policy.evaluate(after).action, 'answer')
        self.assertLessEqual(after[0].score, before[0].score)
        self.assertEqual(self.policy.evaluate(self.retriever.retrieve('燙髮估算成本')).action, 'answer')

    def test_rare_quantity_predicate_cannot_stand_in_for_unknown_subject(self):
        rows = self.rows + [
            approved_chunk(chunk_id='raise', locator='raise', title='一次升多少',
                           section_title='一次升多少', source_file='knowledge/synthetic.md',
                           text='育苗肥料採逐次稀釋'),
            approved_chunk(chunk_id='lower', locator='lower', title='一次降多少',
                           section_title='一次降多少', source_file='knowledge/synthetic.md',
                           text='水槽液位依刻度紀錄'),
        ]
        self.write_rows(rows)
        for query in ('玄武岩密度會升多少', '陀螺儀轉速會降多少'):
            with self.subTest(query=query):
                self.assertNotEqual(self.policy.evaluate(self.retriever.retrieve(query)).action, 'answer')
        for query in ('育苗肥料', '水槽液位', '一次升多少'):
            with self.subTest(query=query):
                self.assertEqual(self.policy.evaluate(self.retriever.retrieve(query)).action, 'answer')

    def test_quantity_object_keeps_directional_support_without_prior_action_span(self):
        self.assertIn('少錢', evidence_question('廣告一天要花多少錢'))
        self.assertNotIn('升多', evidence_question('玄武岩密度會升多少'))
        self.assertNotIn('降多', evidence_question('陀螺儀轉速會降多少'))

    def test_short_subject_and_exact_approved_alias_remain_answerable(self):
        for query in ('燙髮', '分析狀況有哪些步驟'):
            with self.subTest(query=query):
                self.assertEqual(self.policy.evaluate(self.retriever.retrieve(query)).action, 'answer')

    def test_approved_synonym_can_supply_source_subject(self):
        synonyms = self.root / 'synonyms.json'
        synonyms.write_text(json.dumps([['燙髮', '捲度重塑']]))
        hits = Retriever(self.store, synonyms).retrieve('捲度重塑要如何處理')
        self.assertEqual(hits[0].locator, 'row-1')
        self.assertEqual(self.policy.evaluate(hits).action, 'answer')

    def test_custom_changes_and_reindex_refresh_source_evidence(self):
        query = '軌道週期分析狀況有哪些步驟請提供方法檢查結果'
        self.retriever.retrieve(query)  # warm statistics before changing corpus
        added = approved_chunk(chunk_id='new', locator='new', title='軌道週期',
                               section_title='軌道週期', text='軌道週期依高度計算')
        added['search_text'] = _search_text(added)
        self.store.upsert_custom_chunk(added)
        self.assertEqual(self.retriever.retrieve(query)[0].locator, 'new')
        self.assertEqual(self.policy.evaluate(self.retriever.retrieve(query)).action, 'answer')
        self.store.delete_custom_chunk('new')
        self.assertNotEqual(self.policy.evaluate(self.retriever.retrieve(query)).action, 'answer')
        self.write_rows(self.rows + [added])
        self.assertEqual(self.retriever.retrieve(query)[0].locator, 'new')

    def test_warm_and_fresh_retrievers_agree_after_each_corpus_mutation(self):
        query = '燙髮分析狀況步驟'
        original = self.retriever.retrieve(query)[0].score
        added = approved_chunk(chunk_id='copy', locator='copy', title='燙髮',
                               section_title='燙髮', text='燙髮')
        added['search_text'] = _search_text(added)
        self.store.upsert_custom_chunk(added)
        changed = self.retriever.retrieve(query)[0].score
        self.assertNotEqual(original, changed)
        self.assertEqual(self.retriever.retrieve(query), Retriever(self.store).retrieve(query))
        self.store.clear_custom_chunks()
        self.assertEqual(self.retriever.retrieve(query), Retriever(self.store).retrieve(query))
        self.assertEqual(self.retriever.retrieve(query)[0].score, original)
        other = KnowledgeStore(self.store.db_path)
        try:
            other.upsert_custom_chunk(added)
            self.assertEqual(self.retriever.retrieve(query), Retriever(self.store).retrieve(query))
        finally:
            other.close()

    def test_standalone_subject_cannot_borrow_supported_history_in_any_mode(self):
        service = CustomerService(self.store, self.retriever, self.policy, AnswerEngine())
        history = [{'role': 'user', 'content': '燙髮'}]
        for tone in ('expert', 'service', 'line'):
            for query in ('軌道週期怎麼算', '軌道週期分析狀況有哪些步驟請提供方法檢查結果'):
                for streaming in (False, True):
                    with self.subTest(tone=tone, query=query, streaming=streaming):
                        if streaming:
                            result = list(service.chat_stream(query, tone=tone, history=history, allow_model=False))[-1]
                        else:
                            result = service.chat(query, tone=tone, history=history, allow_model=False)
                        self.assertEqual(result['status'], 'escalated')
                        self.assertFalse(result['citations'])

    def test_weak_supported_new_subject_is_not_replaced_by_a_different_old_source(self):
        booking = SearchHit('booking', '預約', 'knowledge/test.md', 'booking', '預約',
                            '預約時確認時段', '接待', .74)
        perm = SearchHit('perm', '燙髮', 'knowledge/test.md', 'perm', '燙髮',
                         '燙髮後依照方向整理', '技術', .95)
        class ContextBiasedRetriever:
            def retrieve(self, question, limit=6):
                return [perm] if '燙髮' in question else [booking]
        service = CustomerService(self.store, ContextBiasedRetriever(), self.policy, AnswerEngine())
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                kwargs = dict(history=[{'role': 'user', 'content': '燙髮'}], allow_model=False)
                result = (list(service.chat_stream('預約要確認什麼', **kwargs))[-1] if streaming
                          else service.chat('預約要確認什麼', **kwargs))
                self.assertEqual(result['citations'][0]['locator'], 'booking')

    def test_genuine_dependent_followup_keeps_history(self):
        service = CustomerService(self.store, self.retriever, self.policy, AnswerEngine())
        for query in ('為什麼', '那第一步呢', '再短一點'):
            with self.subTest(query=query):
                result = service.chat(query, history=[{'role': 'user', 'content': '燙髮'}], allow_model=False)
                self.assertEqual(result['status'], 'answered')
                self.assertEqual(result['citations'][0]['locator'], 'row-1')
