import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch
from app.answer import AnswerEngine, max_output_tokens, model_timeout
from app.storage import KnowledgeStore
from app.usage import UsagePricing

ENV = {'LLM_BASE_URL': 'https://provider.test', 'LLM_API_KEY': 'test', 'LLM_MODEL': 'test'}

class Response(io.BytesIO):
    pass

class LifecycleTests(unittest.TestCase):
    def test_default_output_is_finite_and_invalid_config_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(max_output_tokens(), int)
            self.assertGreater(max_output_tokens(), 0)
        for value in ['nan', 'inf', '-1']:
            with patch.dict(os.environ, {'LLM_TIMEOUT_SECONDS': value}):
                with self.assertRaises(ValueError):
                    model_timeout()
            with self.assertRaises(ValueError):
                UsagePricing(monthly_budget_twd=float(value))

    def test_direct_engine_incomplete_is_not_success(self):
        body = {'status': 'incomplete', 'output_text': 'partial', 'usage': {'input_tokens': 4, 'output_tokens': 3}}
        with patch.dict(os.environ, ENV), patch('urllib.request.urlopen', return_value=Response(json.dumps(body).encode())):
            with self.assertRaises(ValueError):
                AnswerEngine()._call_model('question', [])

    def test_stream_requires_terminal_and_distinguishes_incomplete(self):
        for terminal in [None, 'response.incomplete', 'response.completed']:
            data = [{'type': 'response.output_text.delta', 'delta': 'partial'}]
            if terminal:
                data.append({'type': terminal, 'response': {'usage': {'input_tokens': 4, 'output_tokens': 3}}})
            response = Response(b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in data))
            with patch.dict(os.environ, ENV), patch('urllib.request.urlopen', return_value=response):
                events = list(AnswerEngine().stream_answer('question', []))
            self.assertIn(('terminal', terminal.split('.')[-1] if terminal else 'eof'), events)
            self.assertTrue(response.closed)

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(self.temp.name + '/db.sqlite')
        import app.budget as budget
        self.ledger = budget.CallLedger(self.store, UsagePricing(monthly_budget_twd=1), system_budget=1)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_full_reservation_and_global_anonymous_budget(self):
        self.ledger.reserve('a', 1, .65, 100)
        with self.assertRaises(ValueError):
            self.ledger.reserve('b', 1, .65, 100)
        with self.assertRaises(ValueError):
            self.ledger.reserve('c', None, .65, 100)

    def test_settlement_and_reserve_race_never_reuses_spend(self):
        self.ledger.reserve('a', 1, .7, 100)
        barrier = threading.Barrier(2)
        outcomes = []
        def settle():
            barrier.wait()
            self.ledger.settle('a', 'completed', {'input_tokens': 1, 'output_tokens': 1}, cost=.7)
        def reserve():
            barrier.wait()
            try:
                self.ledger.reserve('b', 1, .4, 100)
                outcomes.append(True)
            except ValueError:
                outcomes.append(False)
        threads = [threading.Thread(target=f) for f in (settle, reserve)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(outcomes, [False])

    def test_unknown_charge_survives_reopen_and_reconcile_is_idempotent(self):
        self.ledger.reserve('a', None, .7, 100)
        self.ledger.settle('a', 'disconnect', None)
        self.assertEqual(self.ledger.get('a')['cost_twd'], .7)
        self.assertEqual(self.ledger.get('a')['usage_known'], 0)
        self.ledger.reconcile('a', .2, 'invoice:1')
        self.ledger.reconcile('a', .2, 'invoice:1')
        with self.assertRaises(ValueError):
            self.ledger.reconcile('a', .1, 'invoice:2')
        self.assertEqual(self.ledger.get('a')['cost_twd'], .2)

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(self.temp.name + '/db.sqlite')
        from app.model_runtime import ModelRuntime, Admission
        self.runtime = ModelRuntime(self.store, UsagePricing(monthly_budget_twd=0), admission=Admission())

    def wait_idle(self):
        for _ in range(400):
            if not self.runtime.admission.active:
                return
            threading.Event().wait(.005)
        self.fail("background accounting did not finish")

    def tearDown(self):
        self.wait_idle()
        self.store.close()
        self.temp.cleanup()

    def request(self):
        import urllib.request
        return urllib.request.Request('https://provider.test/responses', data=json.dumps({'input': 'hi', 'max_output_tokens': 10}).encode())

    def test_disconnect_closes_response_and_retains_unknown_reservation(self):
        response = Response(b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n')
        with patch('urllib.request.urlopen', return_value=response):
            events = self.runtime.stream(self.request(), timeout=1)
            self.assertEqual(next(events), ('delta', 'hi'))
            events.close()
        row = self.store.connection.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual(row['state'], 'disconnect')
        self.assertGreater(row['cost_twd'], 0)
        self.assertFalse(row['usage_known'])
        self.assertTrue(response.closed)

    def test_durable_reservation_confirmed_before_network_and_failure_blocks(self):
        def fail():
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 1)
            raise RuntimeError('snapshot unavailable')
        self.runtime.durable = fail
        with patch('urllib.request.urlopen') as network:
            with self.assertRaises(RuntimeError):
                self.runtime.complete(self.request(), timeout=1)
        network.assert_not_called()
        self.assertGreater(self.store.connection.execute('SELECT cost_twd FROM model_calls').fetchone()[0], 0)

    def test_429_cooldown_blocks_following_call(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('url', 429, 'busy', {'Retry-After': '60'}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                self.runtime.complete(self.request(), timeout=1)
        with patch('urllib.request.urlopen') as network:
            with self.assertRaises(ValueError):
                self.runtime.complete(self.request(), timeout=1)
            network.assert_not_called()

    def test_concurrency_rpm_and_tpm_are_global(self):
        from app.model_runtime import Admission
        for kwargs in [{'concurrency': 1}, {'rpm': 1}, {'tpm': 15}]:
            admission = Admission(**kwargs)
            token = admission.acquire(10)
            with self.assertRaises(ValueError):
                admission.acquire(10)
            admission.release(token)

    def test_boot_rejects_fractional_output_and_invalid_deadline(self):
        from app.model_runtime import ModelRuntime
        for env in [{'LLM_MAX_OUTPUT_TOKENS': '0.5'}, {'LLM_REQUEST_DEADLINE_SECONDS': 'nan'}]:
            with patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    ModelRuntime(self.store)

    def test_retry_after_is_bounded(self):
        import time
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('url', 429, 'busy', {'Retry-After': '1e100'}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                self.runtime.complete(self.request(), timeout=1)
        self.assertLessEqual(self.runtime.admission.cooldown - time.monotonic(), 3600)

    def test_crash_pending_requires_explicit_reconciliation(self):
        self.runtime.ledger.reserve('crashed', 1, .7, 10)
        with self.assertRaises(ValueError):
            self.runtime.reconcile('crashed', .2, 'invoice')
        self.runtime.reconcile('crashed', .2, 'invoice', allow_pending=True)
        self.assertEqual(self.runtime.ledger.get('crashed')['state'], 'reconciled')

    def test_completed_without_usage_remains_conservatively_charged(self):
        with patch('urllib.request.urlopen', return_value=Response(b'{"status":"completed","output_text":"ok"}')):
            self.runtime.complete(self.request(), timeout=1)
        row = self.store.connection.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['cost_twd'], row['reserved_twd'])
        self.assertGreater(row['cost_twd'], 0)
        self.assertFalse(row['usage_known'])

    def test_terminal_settles_before_consumer_resumes(self):
        body = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":3}}}\n\n'
        with patch('urllib.request.urlopen', return_value=Response(body)):
            stream = self.runtime.stream(self.request(), timeout=1)
            self.assertEqual(next(stream)[0], 'usage')
            row = self.store.connection.execute('SELECT * FROM model_calls').fetchone()
            self.assertEqual(row['state'], 'completed')
            self.assertEqual(row['input_tokens'], 2)
            stream.close()
        self.assertEqual(self.runtime.ledger.get(row['call_id'])['state'], 'completed')

    def test_slow_opener_times_out_and_retains_slot_until_transport_closes(self):
        import time
        gate = threading.Event()
        response = Response(b'{}')
        def open_late(*args, **kwargs):
            gate.wait(1)
            return response
        started = time.monotonic()
        try:
            with patch('urllib.request.urlopen', side_effect=open_late):
                with self.assertRaises(TimeoutError):
                    self.runtime.complete(self.request(), timeout=.03)
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(len(self.runtime.admission.active), 1)
        finally:
            gate.set()
        for _ in range(100):
            if response.closed:
                break
            threading.Event().wait(.005)
        self.assertTrue(response.closed)
        self.assertEqual(len(self.runtime.admission.active), 0)

    def test_slow_read_is_closed_at_absolute_deadline(self):
        class Slow(Response):
            def __init__(self):
                super().__init__(b'')
                self.released = threading.Event()
            def read(self):
                self.released.wait(1)
                return b'{"status":"completed"}'
            def close(self):
                self.released.set()
                super().close()
        import time
        response = Slow()
        started = time.monotonic()
        with patch('urllib.request.urlopen', return_value=response):
            with self.assertRaises(TimeoutError):
                self.runtime.complete(self.request(), timeout=.03)
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(response.closed)
        self.wait_idle()
        self.assertEqual(self.store.connection.execute('SELECT state FROM model_calls').fetchone()[0], 'timeout')

    def test_snapshot_restores_unknown_and_legacy_totals_without_double_charge(self):
        from app.replica import PostgresReplica
        from app.budget import month_bounds
        from datetime import datetime, timezone
        self.store.add_audit({'trace_id': 'old', 'created_at': datetime.now(timezone.utc).isoformat(),
            'question': 'old', 'status': 'answered', 'user_id': 1, 'cost_twd': .1})
        self.runtime.ledger.reserve('unknown', 1, .7, 10)
        self.runtime.ledger.settle('unknown', 'eof', None)
        replica = PostgresReplica('unused')
        snapshot = replica.export_snapshot(self.store)
        with tempfile.TemporaryDirectory() as root:
            other = KnowledgeStore(root + '/db')
            replica.apply_snapshot(other, snapshot)
            self.assertAlmostEqual(other.usage_totals(1, *month_bounds())['spend_twd'], .8)
            other.close()

class IntegrationTests(unittest.TestCase):
    def test_service_attaches_durable_runtime_and_counts_calls_once(self):
        from app.service import CustomerService
        from app.retrieval import Retriever
        from app.policy import PolicyEngine
        with tempfile.TemporaryDirectory() as root:
            store = KnowledgeStore(root + '/db')
            service = CustomerService(store, Retriever(store), PolicyEngine(), AnswerEngine())
            body = {'status': 'completed', 'output_text': '測試標題', 'usage': {'input_tokens': 40, 'output_tokens': 10}}
            with patch.dict(os.environ, ENV), patch('urllib.request.urlopen', side_effect=lambda *a, **k: Response(json.dumps(body).encode())):
                service.summarize_title('問題', '答案', user_id=7)
            from app.budget import month_bounds
            totals = store.usage_totals(7, *month_bounds())
            self.assertEqual(totals['input_tokens'], 40)
            self.assertEqual(store.connection.execute('SELECT user_id FROM model_calls').fetchone()[0], 7)
            self.assertEqual(store.connection.execute('SELECT ledger_backed FROM audits').fetchone()[0], 1)
            store.close()

    def test_auxiliary_and_retry_paths_share_accounting_and_output_bound(self):
        from app.model_runtime import ModelRuntime, Admission, request_scope
        from app.extract import propose_chunks
        from app.budget import month_bounds
        with tempfile.TemporaryDirectory() as root:
            store = KnowledgeStore(root + '/db')
            engine = AnswerEngine()
            engine.runtime = ModelRuntime(store, admission=Admission())
            requests = []
            def provider(request, **kwargs):
                requests.append(json.loads(request.data))
                return Response(json.dumps({'status': 'completed', 'output_text': '完整回答',
                    'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode())
            with patch.dict(os.environ, ENV), patch('urllib.request.urlopen', side_effect=provider):
                with request_scope(user_id=9):
                    engine.answer('問題', [], tone='service')
                    engine.smalltalk('你好')
                    engine.generate_title('問題', '回答')
                    propose_chunks(engine, '文件', '這是一份待整理的完整教材內容，請轉為有用的知識。' * 3, user_id=9)
                    engine.retry_with_citations('問題', [], tone='service')
                    engine.retry_for_quality('問題', [], ['需要改寫'], tone='service')
            rows = store.connection.execute('SELECT * FROM model_calls').fetchall()
            self.assertEqual(len(rows), 6)
            self.assertEqual({r['user_id'] for r in rows}, {9})
            self.assertTrue(all(0 < r['max_output_tokens'] <= 16384 for r in requests))
            self.assertEqual(store.usage_totals(9, *month_bounds())['input_tokens'], 60)
            store.close()

class ReconciliationCommandTests(unittest.TestCase):
    def test_operator_command_reconciles_sqlite_idempotently(self):
        import contextlib
        from app.budget import CallLedger
        with tempfile.TemporaryDirectory() as root:
            path = root + '/db'
            store = KnowledgeStore(path)
            ledger = CallLedger(store, UsagePricing())
            ledger.reserve('call-1', None, .7, 100)
            ledger.settle('call-1', 'eof', None)
            store.close()
            from app.reconcile_usage import main
            args = ['--db', path, 'settle', 'call-1', '--cost-twd', '.2', '--reference', 'invoice:1']
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 0)
            store = KnowledgeStore(path)
            self.assertEqual(store.connection.execute('SELECT cost_twd FROM model_calls').fetchone()[0], .2)
            store.close()

class PersistenceRevisionTests(unittest.TestCase):
    setUp = RuntimeTests.setUp
    tearDown = RuntimeTests.tearDown
    request = RuntimeTests.request
    wait_idle = RuntimeTests.wait_idle

    def test_already_current_snapshot_allows_call(self):
        self.runtime.durable = lambda: False
        with patch('urllib.request.urlopen', return_value=Response(b'{"status":"completed"}')):
            self.assertEqual(self.runtime.complete(self.request(), timeout=1)['status'], 'completed')

    def test_slow_reservation_persistence_never_dispatches_after_deadline(self):
        import time
        started = threading.Event()
        gate = threading.Event()
        calls = []
        def persist():
            calls.append(1)
            started.set()
            gate.wait(1)
        self.runtime.durable = persist
        try:
            begin = time.monotonic()
            with patch('urllib.request.urlopen') as provider:
                with self.assertRaises(TimeoutError):
                    self.runtime.complete(self.request(), timeout=.02)
                self.assertLess(time.monotonic() - begin, .2)
                self.assertEqual(len(self.runtime.admission.active), 1)
                provider.assert_not_called()
                gate.set()
                for _ in range(100):
                    if not self.runtime.admission.active:
                        break
                    threading.Event().wait(.005)
                provider.assert_not_called()
            self.assertEqual(len(calls), 1)
            self.assertGreater(self.store.connection.execute('SELECT cost_twd FROM model_calls').fetchone()[0], 0)
        finally:
            gate.set()

    def test_slow_settlement_persistence_returns_at_deadline(self):
        import time
        gate = threading.Event()
        calls = []
        def persist():
            calls.append(1)
            if len(calls) == 2:
                gate.wait(1)
        self.runtime.durable = persist
        try:
            begin = time.monotonic()
            with patch('urllib.request.urlopen', return_value=Response(b'{"status":"completed","usage":{"input_tokens":2,"output_tokens":3}}')):
                with self.assertRaises(TimeoutError):
                    self.runtime.complete(self.request(), timeout=.03)
            self.assertLess(time.monotonic() - begin, .2)
            self.assertEqual(len(self.runtime.admission.active), 1)
            self.assertEqual(self.store.connection.execute('SELECT state FROM model_calls').fetchone()[0], 'completed')
        finally:
            gate.set()
            for _ in range(100):
                if not self.runtime.admission.active:
                    break
                threading.Event().wait(.005)
        self.assertEqual(len(calls), 2)

    def test_postgres_reconciliation_noop_is_idempotent(self):
        import contextlib
        from app.reconcile_usage import main
        self.runtime.ledger.reserve('cli', None, .7, 10)
        self.runtime.ledger.settle('cli', 'eof', None)
        args = ['--db', str(self.store.db_path), 'settle', 'cli', '--cost-twd', '.2', '--reference', 'invoice']
        with patch('app.reconcile_usage.connection_string', return_value='fake'), patch('app.reconcile_usage.PostgresReplica') as replica, contextlib.redirect_stdout(io.StringIO()):
            replica.return_value.backup.return_value = False
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)

    def test_shutdown_drain_includes_pending_accounting_and_protects_sqlite(self):
        import time
        from types import SimpleNamespace
        from app.server import create_server, AppContext
        gate = threading.Event()
        self.runtime.durable = lambda: gate.wait(1)
        context = SimpleNamespace(service=SimpleNamespace(answerer=SimpleNamespace(runtime=self.runtime)), store=self.store)
        server = create_server('127.0.0.1', 0, context)
        server.drain_timeout = .03
        try:
            with self.assertRaises(TimeoutError):
                self.runtime.complete(self.request(), timeout=.01)
            begin = time.monotonic()
            with self.assertRaises(TimeoutError):
                server.server_close()
            self.assertLess(time.monotonic() - begin, .2)
            with self.assertRaises(TimeoutError):
                AppContext.close(context)
            self.assertEqual(self.store.connection.execute('SELECT 1').fetchone()[0], 1)
        finally:
            gate.set()
            self.wait_idle()
            server.server_close()
