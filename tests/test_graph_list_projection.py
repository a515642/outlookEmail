import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-graph-projection-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

web_outlook_app = importlib.import_module('web_outlook_app')


class FakeGraphListResponse:
    status_code = 200

    def json(self):
        return {'value': []}


class GraphListProjectionTests(unittest.TestCase):
    def _fetch(self, include_headers=False):
        captured = {}

        def fake_get(url, **kwargs):
            captured['params'] = kwargs.get('params') or {}
            return FakeGraphListResponse()

        with patch.object(web_outlook_app, 'get_access_token_graph_result',
                          return_value={'success': True, 'access_token': 'token'}), \
                patch.object(web_outlook_app, 'get_with_proxy_fallback', side_effect=fake_get):
            result = web_outlook_app.get_emails_graph(
                'client', 'refresh', 'inbox', 0, 20, 'direct', [],
                include_headers=include_headers,
            )

        self.assertTrue(result.get('success'))
        return captured['params']

    def test_list_projection_omits_raw_headers_by_default(self):
        params = self._fetch()
        self.assertNotIn('internetMessageHeaders', params['$select'])
        self.assertIn('bodyPreview', params['$select'])
        self.assertIn('subject', params['$select'])

    def test_list_projection_can_request_raw_headers(self):
        params = self._fetch(include_headers=True)
        self.assertIn('internetMessageHeaders', params['$select'])


if __name__ == '__main__':
    unittest.main()
