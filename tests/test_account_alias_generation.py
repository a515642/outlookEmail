import importlib
import os
import re
import sys
import tempfile
import unittest


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-alias-generate-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')

ALIAS_PATTERN = re.compile(r'^user\+[a-z0-9]{6}@outlook\.com$')


class AccountAliasGenerationTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_aliases WHERE alias_email LIKE ?', ('user+%',))
            db.execute('DELETE FROM accounts WHERE email = ?', ('user@outlook.com',))
            db.commit()
            cursor = db.execute(
                '''
                INSERT INTO accounts (email, group_id, account_type, provider)
                VALUES (?, 1, 'outlook', 'outlook')
                ''',
                ('user@outlook.com',),
            )
            db.commit()
            self.account_id = int(cursor.lastrowid)

    def _login(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True

    def test_generates_requested_number_of_plus_aliases(self):
        self._login()
        response = self.client.post(
            f'/api/accounts/{self.account_id}/aliases/generate',
            json={'count': 3},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(len(body['generated']), 3)
        self.assertEqual(len(body['aliases']), 3)
        for alias in body['generated']:
            self.assertRegex(alias, ALIAS_PATTERN)

        with self.app.app_context():
            rows = web_outlook_app.get_db().execute(
                'SELECT alias_email FROM account_aliases WHERE account_id = ?',
                (self.account_id,),
            ).fetchall()
        self.assertEqual(len(rows), 3)

    def test_second_call_keeps_existing_aliases(self):
        self._login()
        first = self.client.post(
            f'/api/accounts/{self.account_id}/aliases/generate',
            json={'count': 2},
        ).get_json()
        self.assertEqual(len(first['aliases']), 2)

        second = self.client.post(
            f'/api/accounts/{self.account_id}/aliases/generate',
            json={'count': 2, 'aliases': first['aliases']},
        ).get_json()
        self.assertEqual(len(second['aliases']), 4)
        for alias in first['aliases']:
            self.assertIn(alias, second['aliases'])

    def test_invalid_count_is_rejected(self):
        self._login()
        for count in (0, 51, 'abc'):
            with self.subTest(count=count):
                response = self.client.post(
                    f'/api/accounts/{self.account_id}/aliases/generate',
                    json={'count': count},
                )
                self.assertEqual(response.status_code, 400)

    def test_missing_account_returns_404(self):
        self._login()
        response = self.client.post(
            '/api/accounts/999999/aliases/generate',
            json={'count': 1},
        )
        self.assertEqual(response.status_code, 404)

    def test_requires_login(self):
        response = self.client.post(
            f'/api/accounts/{self.account_id}/aliases/generate',
            json={'count': 1},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
