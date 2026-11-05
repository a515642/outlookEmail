import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-multiline-search-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

web_outlook_app = importlib.import_module('web_outlook_app')


class AccountMultilineSearchTests(unittest.TestCase):
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

    def _insert_account(self, email_addr, remark=''):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            cursor = db.execute(
                '''
                INSERT INTO accounts (email, group_id, remark, account_type, provider)
                VALUES (?, 1, ?, 'outlook', 'outlook')
                ''',
                (email_addr, remark),
            )
            db.commit()
            return int(cursor.lastrowid)

    def _add_alias(self, account_id, alias_email):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'INSERT INTO account_aliases (account_id, alias_email) VALUES (?, ?)',
                (account_id, alias_email),
            )
            db.commit()

    def _search(self, query):
        response = self.client.get('/api/accounts/search?' + urlencode({'q': query, 'limit': 50}))
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['success'])
        return body

    def _emails(self, query):
        return sorted(account['email'] for account in self._search(query)['accounts'])

    def test_normalize_terms_splits_lines_and_strips_export_suffix(self):
        terms = web_outlook_app.normalize_account_search_terms(
            'A@example.com----token---pass\n\n  B@example.com  \nA@EXAMPLE.COM\n备注关键词'
        )
        self.assertEqual(terms, ['A@example.com', 'B@example.com', '备注关键词'])

        # 单行多个关键词的旧用法保持兼容。
        self.assertEqual(
            web_outlook_app.normalize_account_search_terms('foo bar  foo'),
            ['foo', 'bar'],
        )

    def test_search_matches_each_line_against_email_alias_and_remark(self):
        self._insert_account('team-a@example.com', remark='VIP 客户')
        self._insert_account('team-b@example.com')
        other_id = self._insert_account('other@example.com')
        self._add_alias(other_id, 'alias-c@example.com')

        query = '\n'.join([
            'team-a@example.com----refresh-token---password',
            'team-b@example.com',
            'alias-c@example.com',
            'VIP',
        ])
        self.assertEqual(
            self._emails(query),
            ['other@example.com', 'team-a@example.com', 'team-b@example.com'],
        )

    def test_export_line_alone_only_matches_its_mailbox(self):
        self._insert_account('target@example.com')
        self._insert_account('target-other@example.com')

        self.assertEqual(
            self._emails('target@example.com----token---pass'),
            ['target@example.com'],
        )

    def test_more_than_two_hundred_lines_is_rejected(self):
        response = self.client.get('/api/accounts/search?' + urlencode({
            'q': '\n'.join(f'user{index}@example.com' for index in range(201)),
        }))
        self.assertEqual(response.status_code, 400)
        self.assertIn('200', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
