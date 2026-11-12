import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-mail-guard-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

web_outlook_app = importlib.import_module('web_outlook_app')


def _account(account_id=901, email='guard@example.com'):
    return {
        'id': account_id,
        'email': email,
        'account_type': 'outlook',
        'provider': 'outlook',
        'client_id': 'client',
        'refresh_token': 'token',
    }


class MailGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        web_outlook_app.reset_mail_guard_state()

    def tearDown(self):
        web_outlook_app.reset_mail_guard_state()

    def test_healthz_is_public_and_cheap(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertIn('version', body)

    def test_upstream_timeouts_are_tighter(self):
        self.assertEqual(web_outlook_app.IMAP_TIMEOUT, 15)
        self.assertEqual(web_outlook_app.HTTP_REQUEST_TIMEOUT, 12)
        self.assertEqual(web_outlook_app.MAIL_FETCH_TIMEOUT_DEFAULT_SECONDS, 30)

    def test_guard_disabled_under_testing_by_default(self):
        failure = {'success': False, 'error': 'boom'}
        with patch.object(web_outlook_app, '_fetch_account_folder_emails_unlocked',
                          return_value=failure) as impl:
            for _ in range(5):
                result = web_outlook_app.fetch_account_folder_emails(
                    _account(), 'inbox', 0, 20)
                self.assertFalse(result['success'])
        self.assertEqual(impl.call_count, 5)
        self.assertEqual(web_outlook_app.mail_guard_cooldown_left(_account()), 0)

    def test_repeated_failures_open_cooldown_and_skip_upstream(self):
        failure = {'success': False, 'error': 'boom'}
        with patch.object(web_outlook_app, 'mail_guard_active', return_value=True), \
                patch.object(web_outlook_app, '_fetch_account_folder_emails_unlocked',
                             return_value=failure) as impl:
            for _ in range(web_outlook_app.MAIL_GUARD_FAILURE_THRESHOLD):
                result = web_outlook_app.fetch_account_folder_emails(_account(), 'inbox', 0, 20)
                self.assertFalse(result['success'])
            self.assertEqual(impl.call_count, web_outlook_app.MAIL_GUARD_FAILURE_THRESHOLD)

            blocked = web_outlook_app.fetch_account_folder_emails(_account(), 'inbox', 0, 20)
            self.assertFalse(blocked['success'])
            self.assertEqual(blocked['error']['code'], 'EMAIL_FETCH_COOLDOWN')
            self.assertEqual(impl.call_count, web_outlook_app.MAIL_GUARD_FAILURE_THRESHOLD)

    def test_success_resets_failure_counter(self):
        failure = {'success': False, 'error': 'boom'}
        success = {'success': True, 'emails': [], 'has_more': False}
        account = _account(account_id=902, email='reset@example.com')
        with patch.object(web_outlook_app, 'mail_guard_active', return_value=True), \
                patch.object(web_outlook_app, '_fetch_account_folder_emails_unlocked') as impl:
            impl.side_effect = [failure, failure, success, failure, failure]
            for _ in range(5):
                web_outlook_app.fetch_account_folder_emails(account, 'inbox', 0, 20)
            self.assertEqual(web_outlook_app.mail_guard_cooldown_left(account), 0)

        with patch.object(web_outlook_app, 'mail_guard_active', return_value=True), \
                patch.object(web_outlook_app, '_fetch_account_folder_emails_unlocked',
                             return_value=failure) as impl:
            web_outlook_app.fetch_account_folder_emails(account, 'inbox', 0, 20)
            self.assertGreater(web_outlook_app.mail_guard_cooldown_left(account), 0)
            self.assertEqual(impl.call_count, 1)

    def test_concurrent_identical_requests_share_one_upstream_call(self):
        calls = []
        lock = threading.Lock()

        def slow_impl(*args, **kwargs):
            with lock:
                calls.append(args)
            time.sleep(0.4)
            return {'success': True, 'emails': [], 'has_more': False}

        results = []

        def worker():
            results.append(web_outlook_app.fetch_account_folder_emails(
                _account(account_id=903, email='single@example.com'), 'inbox', 0, 20))

        with patch.object(web_outlook_app, 'mail_guard_active', return_value=True), \
                patch.object(web_outlook_app, '_fetch_account_folder_emails_unlocked',
                             side_effect=slow_impl):
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.get('success') for result in results))
        self.assertEqual(len(calls), 1)

    def test_different_queries_are_not_merged(self):
        calls = []

        def impl(*args, **kwargs):
            calls.append((args[1], args[2], args[3]))

        with patch.object(web_outlook_app, 'mail_guard_active', return_value=True), \
                patch.object(web_outlook_app, '_fetch_account_folder_emails_unlocked',
                             side_effect=impl):
            web_outlook_app.fetch_account_folder_emails(_account(account_id=904), 'inbox', 0, 20)
            web_outlook_app.fetch_account_folder_emails(_account(account_id=904), 'inbox', 20, 20)
            web_outlook_app.fetch_account_folder_emails(_account(account_id=904), 'junkemail', 0, 20)

        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
