import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-token-cache-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

web_outlook_app = importlib.import_module('web_outlook_app')


class FakeTokenResponse:
    status_code = 200

    def __init__(self, token, expires_in=3600):
        self._payload = {'access_token': token, 'expires_in': expires_in}

    def json(self):
        return self._payload


class AccessTokenCacheTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        web_outlook_app.reset_access_token_cache()

    def tearDown(self):
        web_outlook_app.reset_access_token_cache()

    def _enable_cache(self):
        return patch.object(web_outlook_app, 'access_token_cache_active', return_value=True)

    def test_graph_token_is_cached_between_calls(self):
        with self._enable_cache(), patch.object(
            web_outlook_app,
            'request_graph_token_response',
            return_value=FakeTokenResponse('token-a'),
        ) as token_mock:
            first = web_outlook_app.get_access_token_graph_result('client', 'refresh')
            second = web_outlook_app.get_access_token_graph_result('client', 'refresh')

        self.assertEqual(token_mock.call_count, 1)
        self.assertEqual(first['access_token'], 'token-a')
        self.assertEqual(second['access_token'], 'token-a')

    def test_different_refresh_token_has_separate_entry(self):
        with self._enable_cache(), patch.object(
            web_outlook_app,
            'request_graph_token_response',
            side_effect=[
                FakeTokenResponse('token-one'),
                FakeTokenResponse('token-two'),
            ],
        ) as token_mock:
            first = web_outlook_app.get_access_token_graph_result('client', 'refresh-1')
            second = web_outlook_app.get_access_token_graph_result('client', 'refresh-2')

        self.assertEqual(token_mock.call_count, 2)
        self.assertEqual(first['access_token'], 'token-one')
        self.assertEqual(second['access_token'], 'token-two')

    def test_expired_entry_is_refreshed(self):
        with self._enable_cache(), patch.object(
            web_outlook_app,
            'request_graph_token_response',
            side_effect=[
                FakeTokenResponse('token-old'),
                FakeTokenResponse('token-new'),
            ],
        ) as token_mock:
            web_outlook_app.get_access_token_graph_result('client', 'refresh')
            scope_key = web_outlook_app.access_token_cache_scope_key('client', 'refresh')
            key = ('graph', 'client', 'refresh', scope_key)
            with web_outlook_app.access_token_cache_lock:
                web_outlook_app.access_token_cache[key] = ('token-old', time.monotonic() - 1)
            refreshed = web_outlook_app.get_access_token_graph_result('client', 'refresh')

        self.assertEqual(token_mock.call_count, 2)
        self.assertEqual(refreshed['access_token'], 'token-new')

    def test_cache_disabled_by_default_in_testing(self):
        with patch.object(
            web_outlook_app,
            'request_graph_token_response',
            side_effect=[
                FakeTokenResponse('token-a'),
                FakeTokenResponse('token-b'),
            ],
        ) as token_mock:
            web_outlook_app.get_access_token_graph_result('client', 'refresh')
            second = web_outlook_app.get_access_token_graph_result('client', 'refresh')

        self.assertEqual(token_mock.call_count, 2)
        self.assertEqual(second['access_token'], 'token-b')

    def test_imap_and_graph_caches_are_isolated(self):
        with self._enable_cache(), \
                patch.object(web_outlook_app, 'request_graph_token_response',
                             return_value=FakeTokenResponse('graph-token')), \
                patch.object(web_outlook_app, 'request_imap_token_response',
                             return_value=FakeTokenResponse('imap-token')):
            graph = web_outlook_app.get_access_token_graph_result('client', 'refresh')
            imap = web_outlook_app.get_access_token_imap_result('client', 'refresh')

        self.assertEqual(graph['access_token'], 'graph-token')
        self.assertEqual(imap['access_token'], 'imap-token')

    def test_scope_is_part_of_cache_key(self):
        send_scope = 'https://graph.microsoft.com/Mail.Send'
        with self._enable_cache(), patch.object(
            web_outlook_app,
            'request_graph_token_response',
            side_effect=[
                FakeTokenResponse('read-token'),
                FakeTokenResponse('send-token'),
                FakeTokenResponse('read-token-2'),
            ],
        ) as token_mock:
            read_first = web_outlook_app.get_access_token_graph_result('client', 'refresh')
            send_first = web_outlook_app.get_access_token_graph_result(
                'client', 'refresh', required_scope=send_scope,
            )
            read_second = web_outlook_app.get_access_token_graph_result('client', 'refresh')

        self.assertEqual(token_mock.call_count, 2)
        self.assertEqual(read_first['access_token'], 'read-token')
        self.assertEqual(send_first['access_token'], 'send-token')
        self.assertEqual(read_second['access_token'], 'read-token')

    def test_cache_respects_max_entries(self):
        with self._enable_cache(), patch.object(web_outlook_app, 'ACCESS_TOKEN_CACHE_MAX_ENTRIES', 2):
            for index in range(3):
                web_outlook_app.cache_access_token('graph', 'client', f'refresh-{index}', f'token-{index}', 3600)

        with web_outlook_app.access_token_cache_lock:
            self.assertEqual(len(web_outlook_app.access_token_cache), 2)


if __name__ == '__main__':
    unittest.main()
