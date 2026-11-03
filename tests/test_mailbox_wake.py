import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-wake-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class MailboxWakeTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True

        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM accounts')
            db.commit()
            added = web_outlook_app.add_account(
                'wake@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            )
            self.assertTrue(added)
            self.account = web_outlook_app.get_account_by_email('wake@example.com')

    def _inbox_result(self, is_read=False):
        return {
            'success': True,
            'emails': [{
                'id': 'latest-message-1',
                'id_mode': 'graph',
                'subject': 'Latest',
                'is_read': is_read,
                'folder': 'inbox',
            }],
            'method': 'Graph API',
            'has_more': False,
            'request_method': 'graph',
        }

    def test_wake_toggles_latest_message_read_state_then_restores(self):
        calls = []

        def fake_set_read_state(account, items, is_read, proxy_url, fallback_proxy_urls):
            calls.append({'id': items[0]['id'], 'is_read': is_read})
            return {
                'success': True,
                'success_count': 1,
                'failed_count': 0,
                'updated_ids': [items[0]['id']],
                'errors': [],
            }

        with patch.object(web_outlook_app, 'fetch_account_folder_emails',
                          return_value=self._inbox_result(is_read=False)), \
                patch.object(web_outlook_app, 'set_account_email_read_state',
                             side_effect=fake_set_read_state):
            response = self.client.post(f'/api/accounts/{self.account["id"]}/wake', json={})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['restored'])
        self.assertEqual(calls, [
            {'id': 'latest-message-1', 'is_read': True},
            {'id': 'latest-message-1', 'is_read': False},
        ])

        with self.app.app_context():
            row = web_outlook_app.get_db().execute(
                'SELECT mailbox_last_wake_at FROM accounts WHERE id = ?',
                (self.account['id'],),
            ).fetchone()
        self.assertIsNotNone(row['mailbox_last_wake_at'])

    def test_empty_inbox_falls_back_to_temp_folder_write(self):
        empty_result = {
            'success': True,
            'emails': [],
            'method': 'Graph API',
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(web_outlook_app, 'fetch_account_folder_emails',
                          return_value=empty_result), \
                patch.object(web_outlook_app, 'wake_mailbox_with_temp_folder_graph',
                             return_value={'success': True, 'cleaned': True}) as graph_mock, \
                patch.object(web_outlook_app, 'set_account_email_read_state') as write_mock:
            response = self.client.post(f'/api/accounts/{self.account["id"]}/wake', json={})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['empty_inbox'])
        self.assertTrue(payload['cleaned'])
        self.assertIn('临时文件夹', payload['message'])
        graph_mock.assert_called_once()
        write_mock.assert_not_called()

        with self.app.app_context():
            row = web_outlook_app.get_db().execute(
                'SELECT mailbox_last_wake_at FROM accounts WHERE id = ?',
                (self.account['id'],),
            ).fetchone()
        self.assertIsNotNone(row['mailbox_last_wake_at'])

    def test_empty_inbox_reports_error_when_temp_folder_channels_fail(self):
        empty_result = {
            'success': True,
            'emails': [],
            'method': 'Graph API',
            'has_more': False,
            'request_method': 'graph',
        }

        with patch.object(web_outlook_app, 'fetch_account_folder_emails',
                          return_value=empty_result), \
                patch.object(web_outlook_app, 'wake_mailbox_with_temp_folder_graph',
                             return_value={'success': False, 'error': 'graph failed'}), \
                patch.object(web_outlook_app, 'wake_mailbox_with_temp_folder_imap',
                             return_value={'success': False, 'error': 'imap failed'}):
            response = self.client.post(f'/api/accounts/{self.account["id"]}/wake', json={})

        self.assertEqual(response.status_code, 502)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('收件箱为空', payload['error'])
        self.assertEqual(len(payload['details']), 2)

    def test_wake_rejects_imap_accounts(self):
        with self.app.app_context():
            web_outlook_app.add_account(
                'imap@example.com',
                'password',
                group_id=1,
                account_type='imap',
                provider='custom',
                imap_host='imap.example.com',
                imap_password='imap-password',
            )
            imap_account = web_outlook_app.get_account_by_email('imap@example.com')
        self.assertIsNotNone(imap_account)

        with patch.object(web_outlook_app, 'fetch_account_folder_emails') as fetch_mock, \
                patch.object(web_outlook_app, 'set_account_email_read_state') as write_mock:
            response = self.client.post(f'/api/accounts/{imap_account["id"]}/wake', json={})

        self.assertEqual(response.status_code, 502)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('仅支持 Outlook', payload['error'])
        fetch_mock.assert_not_called()
        write_mock.assert_not_called()

    def test_settings_expose_and_validate_wake_options(self):
        response = self.client.get('/api/settings')
        self.assertEqual(response.status_code, 200)
        settings = response.get_json()['settings']
        self.assertEqual(settings['mail_auto_sync_seconds'], '30')
        self.assertEqual(settings['mailbox_keepalive_enabled'], 'false')
        self.assertEqual(settings['mailbox_keepalive_interval_hours'], '24')

        response = self.client.put('/api/settings', json={'mail_auto_sync_seconds': 45})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])

        response = self.client.put('/api/settings', json={'mail_auto_sync_seconds': 9999})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('0-600', payload['error'])


if __name__ == '__main__':
    unittest.main()
