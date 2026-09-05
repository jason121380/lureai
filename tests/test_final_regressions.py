"""Offline regressions for startup, capability boundaries and numeric evidence."""
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.auth import AuthManager
from app.replica import PostgresReplica
from app.server import AppContext
from app.storage import KnowledgeStore
from app.quality import grounding_diagnostics
from tests.test_ingest import approved_chunk
from tests.test_model_runtime import Response


class BootFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'knowledge.jsonl'
        self.source.write_text(json.dumps(approved_chunk()), encoding='utf-8')
        self.paths = {'knowledge': self.source, 'database': self.root / 'new.db'}

    def boot(self, replica, inspect, env=None):
        from run import main
        server = Mock(server_port=0)
        def create(_host, _port, context):
            inspect(context)
            return server
        with patch.dict(os.environ, env or {}, clear=True), patch('run.default_paths', return_value=self.paths), patch('run.PostgresReplica.from_env', return_value=replica), patch('run.create_server', side_effect=create) as serving:
            self.serving = serving
            return main([])

    def test_sqlite_only_boot_dispatches_and_settles_call(self):
        def inspect(context):
            request = urllib.request.Request('https://offline.test', data=b'{"input":"hi","max_output_tokens":10}')
            body = b'{"status":"completed","output_text":"ok","usage":{"input_tokens":2,"output_tokens":3}}'
            with patch('urllib.request.urlopen', return_value=Response(body)) as transport:
                context.service.answerer.runtime.complete(request, timeout=1)
            self.assertEqual(transport.call_count, 1)
            row = context.store.connection.execute('SELECT * FROM model_calls').fetchone()
            self.assertEqual(row['state'], 'completed')
            self.assertTrue(row['usage_known'])
        self.assertEqual(self.boot(PostgresReplica(''), inspect), 0)

    def test_configured_unowned_or_lost_writer_blocks_dispatch(self):
        for replica in (PostgresReplica('configured'), SimpleNamespace(configured=True, enabled=True, check_writer=lambda: False)):
            context = AppContext.create(self.paths['database'], self.source, self.root, 'token')
            try:
                context.replica = replica
                request = urllib.request.Request('https://offline.test', data=b'{"input":"hi","max_output_tokens":10}')
                with patch('urllib.request.urlopen') as transport:
                    with self.assertRaisesRegex(RuntimeError, 'writer unavailable'):
                        context.service.answerer.runtime.complete(request, timeout=1)
                transport.assert_not_called()
            finally:
                context.close()

    def snapshot_replica(self, password='1234', active=True):
        source = KnowledgeStore(self.root / 'source.db')
        auth = AuthManager(source)
        user = auth.create_or_reset_user('legacy', 'old-secret-very-long', role='admin')
        encoded = auth._hash_password(password)
        source.connection.execute('UPDATE users SET password_hash=?, active=? WHERE id=?', (encoded, int(active), user['id']))
        source.connection.commit()
        helper = PostgresReplica('')
        snapshot = helper.export_snapshot(source)
        source.close()
        replica = Mock(configured=True, enabled=True)
        replica.restore.side_effect = lambda store: (helper.apply_snapshot(store, snapshot), True)[1]
        return replica, encoded

    def test_fresh_restore_accepts_legacy_environment_and_password(self):
        replica, encoded = self.snapshot_replica()
        def inspect(context):
            self.assertTrue(context.restored_from_replica)
            _, user = context.auth.login('legacy', '1234')
            self.assertEqual(user['role'], 'admin')
            self.assertEqual(context.store.connection.execute('SELECT password_hash FROM users WHERE username="legacy"').fetchone()[0], encoded)
        self.assertEqual(self.boot(replica, inspect, {'USER_USERNAME': 'legacy', 'USER_PASSWORD': '1234', 'USER_ROLE': 'user'}), 0)
        replica.start.assert_called_once()

    def test_restored_existing_account_hash_role_and_inactive_state_are_preserved(self):
        replica, encoded = self.snapshot_replica(password='existing-secret-long', active=False)
        def inspect(context):
            row = context.store.connection.execute('SELECT * FROM users WHERE username="legacy"').fetchone()
            self.assertEqual((row['password_hash'], row['role'], row['active']), (encoded, 'admin', 0))
        self.assertEqual(self.boot(replica, inspect, {'USER_USERNAME': 'legacy', 'USER_PASSWORD': '1234', 'USER_ROLE': 'user'}), 0)

    def test_new_weak_bootstrap_fails_after_empty_restore_before_backup_or_serving(self):
        replica = Mock(configured=True, enabled=True)
        replica.restore.return_value = False
        with self.assertRaisesRegex(ValueError, '15'):
            self.boot(replica, lambda context: self.fail('served weak new account'), {'USER_USERNAME': 'new', 'USER_PASSWORD': '1234'})
        replica.restore.assert_called_once()
        replica.start.assert_not_called()
        self.serving.assert_not_called()
        replica.stop.assert_called_once_with()

    def test_restore_failure_does_not_create_bootstrap_or_start_backup(self):
        replica = Mock(configured=True, enabled=True)
        replica.restore.side_effect = RuntimeError('restore failed')
        with self.assertRaisesRegex(RuntimeError, 'restore failed'):
            self.boot(replica, lambda context: self.fail('served failed restore'), {'USER_USERNAME': 'new', 'USER_PASSWORD': '1234'})
        replica.start.assert_not_called()
        self.serving.assert_not_called()
        store = KnowledgeStore(self.paths['database'])
        self.addCleanup(store.close)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM users').fetchone()[0], 0)


class CitationOffsetTests(unittest.TestCase):
    def test_citations_before_comma_do_not_authorize_unrelated_percentage(self):
        hits = [SimpleNamespace(text='私訊轉預約率沒有標準答案。熟客折扣為10%。')]
        for mark in ('', '[1]', '[1][1]'):
            result = grounding_diagnostics(f'熟客折扣要調整{mark}，10%是私訊轉預約率目標。[1]', hits)
            self.assertIn('10 %', result['unsupported_numbers'])
            self.assertTrue(result['quality_failed'])

    def test_supported_local_numeric_clause_remains_valid(self):
        hits = [SimpleNamespace(text='私訊轉預約率沒有標準答案。熟客折扣為10%。')]
        result = grounding_diagnostics('私訊轉預約率沒有標準答案[1]，熟客折扣為10%。[1]', hits)
        self.assertEqual(result['unsupported_numbers'], [])
        self.assertFalse(result['quality_failed'])
