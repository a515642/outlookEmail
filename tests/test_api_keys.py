import importlib
import os
import pathlib
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _test_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.test_tmp')
    os.makedirs(_test_root, exist_ok=True)
    _temp_dir = os.path.join(_test_root, 'api_keys')
    os.makedirs(_temp_dir, exist_ok=True)
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

web_outlook_app = importlib.import_module('web_outlook_app')
ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]

GROUP_PREFIX = 'APIKey测试'


class ApiKeysTestBase(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM api_key_audit_logs')
            db.execute('DELETE FROM api_key_claims')
            db.execute('DELETE FROM api_key_groups')
            db.execute('DELETE FROM api_keys')
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM accounts')
            db.execute('DELETE FROM audit_logs')
            db.execute('DELETE FROM groups WHERE name LIKE ?', (GROUP_PREFIX + '%',))
            db.commit()
        # 每个用例使用独立 Key id，避免进程内限速桶互相影响。
        web_outlook_app._api_key_rate_buckets.clear()
        web_outlook_app._api_key_audit_insert_counters.clear()

    # ---------- fixtures ----------

    def _login(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True

    def _insert_group(self, suffix, parent_id=None, level=1):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            cursor = db.execute(
                'INSERT INTO groups (name, parent_id, level) VALUES (?, ?, ?)',
                (f'{GROUP_PREFIX}{suffix}', parent_id, level),
            )
            db.commit()
            return int(cursor.lastrowid)

    def _insert_account(self, email_addr, group_id, status='active'):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            cursor = db.execute(
                '''
                INSERT INTO accounts (email, password, client_id, refresh_token, group_id, status)
                VALUES (?, 'secret-password', 'client-id', 'refresh-token', ?, ?)
                ''',
                (email_addr, group_id, status),
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

    def _create_key(self, group_ids, scopes=None, **overrides):
        self._login()
        payload = {
            'name': overrides.pop('name', '测试 Key'),
            'scopes': scopes or ['mail:read', 'account:claim'],
            'group_ids': group_ids,
            'expires_at': overrides.pop('expires_at', None),
            'rate_limit_per_minute': overrides.pop('rate_limit_per_minute', 0),
            'ip_allowlist': overrides.pop('ip_allowlist', ''),
        }
        payload.update(overrides)
        response = self.client.post('/api/settings/api-keys', json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['secret'].startswith('okv1_'))
        return body['key'], body['secret']

    def _headers(self, secret):
        return {'X-API-Key': secret}

    def _insert_claim(self, key_id, account_id, email_addr, minutes_ago=0, released=False):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                '''
                INSERT INTO api_key_claims
                    (api_key_id, account_id, normalized_email, email_snapshot,
                     claimed_at, released_at, release_reason)
                VALUES (?, ?, ?, ?, datetime('now', ?), ?, ?)
                ''',
                (
                    key_id,
                    account_id,
                    email_addr.lower(),
                    email_addr,
                    f'-{int(minutes_ago)} minutes',
                    'explicit' if released else None,
                    'explicit' if released else '',
                ),
            )
            db.commit()

    # ---------- management ----------

    def test_create_returns_secret_once_and_stores_only_hash(self):
        group_id = self._insert_group('管理')
        key, secret = self._create_key([group_id])
        self.assertNotIn('secret', key)
        self.assertNotIn('key_hash', key)
        self.assertEqual(key['status'], 'active')
        with self.app.app_context():
            row = web_outlook_app.get_db().execute(
                'SELECT * FROM api_keys WHERE id = ?', (key['id'],)
            ).fetchone()
            self.assertEqual(row['key_hash'], web_outlook_app.hash_api_key_secret(secret))
            self.assertNotEqual(row['key_hash'], secret)
            self.assertEqual(row['key_prefix'], secret[:12])
            self.assertEqual(row['key_last4'], secret[-4:])

        listing = self.client.get('/api/settings/api-keys').get_json()
        self.assertTrue(listing['success'])
        self.assertEqual(len(listing['keys']), 1)
        listed = listing['keys'][0]
        self.assertNotIn('secret', listed)
        self.assertNotIn('key_hash', listed)

    def test_management_requires_login(self):
        response = self.client.get('/api/settings/api-keys')
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.get_json()['need_login'])

    def test_create_validation(self):
        group_id = self._insert_group('校验')
        self._login()
        base = {'name': 'x', 'scopes': ['mail:read'], 'group_ids': [group_id]}
        cases = [
            {**base, 'name': ''},
            {**base, 'scopes': []},
            {**base, 'scopes': ['mail:send']},
            {**base, 'group_ids': []},
            {**base, 'group_ids': [999999]},
            {**base, 'ip_allowlist': 'not-an-ip'},
            {**base, 'expires_at': '2000-01-01T00:00:00Z'},
        ]
        for payload in cases:
            response = self.client.post('/api/settings/api-keys', json=payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertFalse(response.get_json()['success'])

    def test_rotate_and_revoke(self):
        group_id = self._insert_group('轮换')
        self._insert_account('rotate@example.com', group_id)
        key, old_secret = self._create_key([group_id])

        rotate = self.client.post(f'/api/settings/api-keys/{key["id"]}/rotate')
        self.assertEqual(rotate.status_code, 200)
        rotate_body = rotate.get_json()
        new_secret = rotate_body['secret']
        self.assertNotEqual(old_secret, new_secret)

        old_response = self.client.get(
            '/api/v1/mail/folders?email=rotate@example.com',
            headers=self._headers(old_secret),
        )
        self.assertEqual(old_response.status_code, 401)
        new_response = self.client.get(
            '/api/v1/mail/folders?email=rotate@example.com',
            headers=self._headers(new_secret),
        )
        self.assertEqual(new_response.status_code, 200)

        revoke = self.client.post(f'/api/settings/api-keys/{key["id"]}/revoke')
        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(revoke.get_json()['key']['status'], 'revoked')
        rejected = self.client.get(
            '/api/v1/mail/folders?email=rotate@example.com',
            headers=self._headers(new_secret),
        )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.get_json()['error_code'], 'key_revoked')

        again = self.client.post(f'/api/settings/api-keys/{key["id"]}/revoke')
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.get_json()['success'])

    # ---------- authentication ----------

    def test_auth_errors_and_bearer(self):
        group_id = self._insert_group('认证')
        self._insert_account('auth@example.com', group_id)
        key, secret = self._create_key([group_id])

        missing = self.client.get('/api/v1/mail/folders?email=auth@example.com')
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.get_json()['error_code'], 'missing_api_key')

        invalid = self.client.get(
            '/api/v1/mail/folders?email=auth@example.com',
            headers=self._headers('okv1_' + 'x' * 40),
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.get_json()['error_code'], 'invalid_api_key')

        query_key = self.client.get(
            f'/api/v1/mail/folders?email=auth@example.com&api_key={secret}'
        )
        self.assertEqual(query_key.status_code, 401)
        self.assertEqual(query_key.get_json()['error_code'], 'missing_api_key')

        bearer = self.client.get(
            '/api/v1/mail/folders?email=auth@example.com',
            headers={'Authorization': f'Bearer {secret}'},
        )
        self.assertEqual(bearer.status_code, 200)

    def test_expired_key_rejected(self):
        group_id = self._insert_group('过期')
        self._insert_account('expired@example.com', group_id)
        key, secret = self._create_key([group_id])
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                "UPDATE api_keys SET expires_at = '2000-01-01 00:00:00' WHERE id = ?",
                (key['id'],),
            )
            db.commit()
        response = self.client.get(
            '/api/v1/mail/folders?email=expired@example.com',
            headers=self._headers(secret),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error_code'], 'key_expired')

    def test_legacy_and_v1_keys_are_disjoint(self):
        with self.app.app_context():
            web_outlook_app.set_setting('external_api_key', 'legacy-global-key')

        legacy_on_v1 = self.client.get(
            '/api/v1/mail/folders?email=any@example.com',
            headers=self._headers('legacy-global-key'),
        )
        self.assertEqual(legacy_on_v1.status_code, 401)
        self.assertEqual(legacy_on_v1.get_json()['error_code'], 'invalid_api_key')

        group_id = self._insert_group('隔离')
        _key, secret = self._create_key([group_id])
        v1_on_legacy = self.client.get(
            '/api/external/emails?email=any@example.com',
            headers=self._headers(secret),
        )
        self.assertEqual(v1_on_legacy.status_code, 401)

    def test_scope_denied(self):
        group_id = self._insert_group('权限')
        self._insert_account('scope@example.com', group_id)
        _key, mail_secret = self._create_key([group_id], scopes=['mail:read'])
        _key2, claim_secret = self._create_key([group_id], scopes=['account:claim'])

        claim_denied = self.client.post(
            '/api/v1/accounts/claim', headers=self._headers(mail_secret)
        )
        self.assertEqual(claim_denied.status_code, 403)
        self.assertEqual(claim_denied.get_json()['error_code'], 'scope_denied')

        mail_denied = self.client.get(
            '/api/v1/mail/folders?email=scope@example.com',
            headers=self._headers(claim_secret),
        )
        self.assertEqual(mail_denied.status_code, 403)
        self.assertEqual(mail_denied.get_json()['error_code'], 'scope_denied')

    # ---------- grouping ----------

    def test_group_scope_includes_descendants_and_rejects_siblings(self):
        parent = self._insert_group('父')
        child = self._insert_group('子', parent_id=parent, level=2)
        sibling = self._insert_group('兄弟')
        self._insert_account('child@example.com', child)
        self._insert_account('sibling@example.com', sibling)
        _key, secret = self._create_key([parent])

        child_response = self.client.get(
            '/api/v1/mail/folders?email=child@example.com',
            headers=self._headers(secret),
        )
        self.assertEqual(child_response.status_code, 200)

        sibling_response = self.client.get(
            '/api/v1/mail/folders?email=sibling@example.com',
            headers=self._headers(secret),
        )
        self.assertEqual(sibling_response.status_code, 403)
        self.assertEqual(sibling_response.get_json()['error_code'], 'group_not_allowed')

    def test_alias_outside_scope_is_rejected(self):
        allowed = self._insert_group('别名允许')
        other = self._insert_group('别名范围外')
        self._insert_account('owner@example.com', other)
        with self.app.app_context():
            db = web_outlook_app.get_db()
            account_id = db.execute(
                'SELECT id FROM accounts WHERE email = ?', ('owner@example.com',)
            ).fetchone()['id']
        self._add_alias(account_id, 'alias@example.com')
        _key, secret = self._create_key([allowed])

        response = self.client.get(
            '/api/v1/mail/folders?email=alias@example.com',
            headers=self._headers(secret),
        )
        self.assertEqual(response.status_code, 403)

    def test_empty_group_scope_fails_closed(self):
        group_id = self._insert_group('空范围')
        self._insert_account('closed@example.com', group_id)
        key, secret = self._create_key([group_id])
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM api_key_groups WHERE api_key_id = ?', (key['id'],))
            db.commit()

        claim_response = self.client.post(
            '/api/v1/accounts/claim', headers=self._headers(secret)
        )
        self.assertEqual(claim_response.status_code, 404)
        self.assertEqual(claim_response.get_json()['error_code'], 'no_account_available')

        mail_response = self.client.get(
            '/api/v1/mail/folders?email=closed@example.com',
            headers=self._headers(secret),
        )
        self.assertEqual(mail_response.status_code, 403)

    # ---------- claim ----------

    def test_claim_returns_only_email_and_group(self):
        group_id = self._insert_group('领取')
        self._insert_account('claim@example.com', group_id)
        _key, secret = self._create_key([group_id])

        response = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual(data['email'], 'claim@example.com')
        self.assertEqual(data['group_id'], group_id)
        self.assertEqual(data['group_name'], f'{GROUP_PREFIX}领取')
        self.assertEqual(set(data.keys()), {'email', 'group_id', 'group_name'})
        payload_text = response.get_data(as_text=True)
        self.assertNotIn('secret-password', payload_text)
        self.assertNotIn('refresh-token', payload_text)

    def test_claim_skips_accounts_claimed_within_ten_minutes(self):
        group_id = self._insert_group('冷却')
        first = self._insert_account('first@example.com', group_id)
        self._insert_account('second@example.com', group_id)
        key, secret = self._create_key([group_id])
        self._insert_claim(key['id'], first, 'first@example.com', minutes_ago=5)

        response = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data']['email'], 'second@example.com')

    def test_claim_returns_account_after_cooldown(self):
        group_id = self._insert_group('回归')
        account_id = self._insert_account('returned@example.com', group_id)
        key, secret = self._create_key([group_id])
        self._insert_claim(key['id'], account_id, 'returned@example.com', minutes_ago=11)

        response = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data']['email'], 'returned@example.com')

    def test_explicit_release_allows_immediate_reclaim(self):
        group_id = self._insert_group('释放')
        self._insert_account('release@example.com', group_id)
        _key, secret = self._create_key([group_id])

        first = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['data']['email'], 'release@example.com')

        blocked = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(blocked.status_code, 404)

        released = self.client.post(
            '/api/v1/accounts/release',
            headers=self._headers(secret),
            json={'email': 'release@example.com'},
        )
        self.assertEqual(released.status_code, 200)
        self.assertTrue(released.get_json()['data']['released'])

        again = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()['data']['email'], 'release@example.com')

    def test_claims_are_isolated_per_key(self):
        group_id = self._insert_group('跨Key')
        self._insert_account('shared@example.com', group_id)
        _key_a, secret_a = self._create_key([group_id], name='Key A')
        _key_b, secret_b = self._create_key([group_id], name='Key B')

        first = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret_a))
        self.assertEqual(first.status_code, 200)
        second = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret_b))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()['data']['email'], 'shared@example.com')

    def test_claim_and_release_errors(self):
        group_id = self._insert_group('空池')
        _key, secret = self._create_key([group_id])

        empty = self.client.post('/api/v1/accounts/claim', headers=self._headers(secret))
        self.assertEqual(empty.status_code, 404)
        self.assertEqual(empty.get_json()['error_code'], 'no_account_available')

        missing = self.client.post(
            '/api/v1/accounts/release', headers=self._headers(secret), json={}
        )
        self.assertEqual(missing.status_code, 400)

        unknown = self.client.post(
            '/api/v1/accounts/release',
            headers=self._headers(secret),
            json={'email': 'nobody@example.com'},
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.get_json()['error_code'], 'claim_not_found')

    # ---------- mail ----------

    def test_mail_list_whitelists_fields(self):
        group_id = self._insert_group('邮件')
        self._insert_account('mail@example.com', group_id)
        _key, secret = self._create_key([group_id])

        fake_item = {
            'id': 'msg-1',
            'subject': '你好',
            'from': 'sender@example.com',
            'to': 'mail@example.com',
            'date': '2026-01-01T00:00:00Z',
            'is_read': True,
            'has_attachments': True,
            'body_preview': '预览',
            'folder': 'inbox',
            'id_mode': 'uid',
            'password': 'leaked-password',
            'refresh_token': 'leaked-token',
        }
        with patch.object(web_outlook_app, 'fetch_account_emails', return_value={
            'success': True,
            'emails': [fake_item],
            'has_more': False,
        }) as mocked:
            response = self.client.get(
                '/api/v1/mail/messages?email=mail@example.com&folder=inbox&skip=0&top=10',
                headers=self._headers(secret),
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual(data['emails'][0]['id'], 'msg-1')
        self.assertEqual(
            set(data['emails'][0].keys()),
            {'id', 'subject', 'from', 'to', 'date', 'is_read',
             'has_attachments', 'body_preview', 'folder', 'id_mode'},
        )
        self.assertNotIn('leaked-password', response.get_data(as_text=True))
        mocked.assert_called_once()

    def test_mail_list_invalid_folder_and_failure(self):
        group_id = self._insert_group('邮件错误')
        self._insert_account('mail-error@example.com', group_id)
        _key, secret = self._create_key([group_id])

        invalid = self.client.get(
            '/api/v1/mail/messages?email=mail-error@example.com&folder=sentitems',
            headers=self._headers(secret),
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()['error_code'], 'invalid_folder')

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value={
            'success': False,
            'error': 'upstream boom',
        }):
            failed = self.client.get(
                '/api/v1/mail/messages?email=mail-error@example.com',
                headers=self._headers(secret),
            )
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed.get_json()['error_code'], 'mail_fetch_failed')
        self.assertNotIn('upstream boom', failed.get_data(as_text=True))

    def test_mail_detail_whitelists_fields(self):
        group_id = self._insert_group('正文')
        self._insert_account('detail@example.com', group_id)
        _key, secret = self._create_key([group_id])

        fake_detail = {
            'id': 'msg-1',
            'subject': '正文',
            'from': 'sender@example.com',
            'to': 'detail@example.com',
            'cc': 'cc@example.com',
            'date': '2026-01-01T00:00:00Z',
            'body': '<p>hi</p>',
            'body_type': 'html',
            'attachments': [{'name': 'a.pdf', 'contentBytes': 'xxx'}],
            'has_attachments': True,
            'internetMessageHeaders': [{'name': 'X-Secret', 'value': '1'}],
        }
        with patch.object(web_outlook_app, 'fetch_email_detail_for_account', return_value={
            'success': True,
            'email': fake_detail,
        }) as mocked:
            response = self.client.get(
                '/api/v1/mail/messages/msg-1?email=detail@example.com&folder=inbox&method=graph',
                headers=self._headers(secret),
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['data']
        self.assertEqual(data['subject'], '正文')
        self.assertEqual(data['body'], '<p>hi</p>')
        self.assertEqual(
            set(data.keys()),
            {'email', 'message_id', 'subject', 'from', 'to', 'date',
             'body', 'body_type', 'has_attachments'},
        )
        self.assertNotIn('contentBytes', response.get_data(as_text=True))
        self.assertNotIn('internetMessageHeaders', response.get_data(as_text=True))
        mocked.assert_called_once()

    # ---------- IP / rate / audit ----------

    def test_ip_allowlist(self):
        group_id = self._insert_group('IP')
        self._insert_account('ip@example.com', group_id)
        _key, secret = self._create_key(
            [group_id],
            ip_allowlist='203.0.113.0/24\n10.0.0.1',
        )

        allowed = self.client.get(
            '/api/v1/mail/folders?email=ip@example.com',
            headers=self._headers(secret),
            environ_base={'REMOTE_ADDR': '203.0.113.77'},
        )
        self.assertEqual(allowed.status_code, 200)

        exact = self.client.get(
            '/api/v1/mail/folders?email=ip@example.com',
            headers=self._headers(secret),
            environ_base={'REMOTE_ADDR': '10.0.0.1'},
        )
        self.assertEqual(exact.status_code, 200)

        denied = self.client.get(
            '/api/v1/mail/folders?email=ip@example.com',
            headers=self._headers(secret),
            environ_base={'REMOTE_ADDR': '192.0.2.9'},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()['error_code'], 'ip_not_allowed')

    def test_empty_ip_allowlist_disables_check(self):
        group_id = self._insert_group('IP空')
        self._insert_account('ip-open@example.com', group_id)
        _key, secret = self._create_key([group_id], ip_allowlist='')
        response = self.client.get(
            '/api/v1/mail/folders?email=ip-open@example.com',
            headers=self._headers(secret),
            environ_base={'REMOTE_ADDR': '192.0.2.9'},
        )
        self.assertEqual(response.status_code, 200)

    def test_rate_limit_returns_retry_after(self):
        group_id = self._insert_group('限速')
        self._insert_account('rate@example.com', group_id)
        _key, secret = self._create_key([group_id], rate_limit_per_minute=2)

        first = self.client.get(
            '/api/v1/mail/folders?email=rate@example.com', headers=self._headers(secret)
        )
        second = self.client.get(
            '/api/v1/mail/folders?email=rate@example.com', headers=self._headers(secret)
        )
        third = self.client.get(
            '/api/v1/mail/folders?email=rate@example.com', headers=self._headers(secret)
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.get_json()['error_code'], 'rate_limited')
        self.assertIn('Retry-After', third.headers)

    def test_audit_and_usage_counters(self):
        group_id = self._insert_group('审计')
        self._insert_account('audit@example.com', group_id)
        key, secret = self._create_key([group_id])

        for _ in range(2):
            response = self.client.get(
                '/api/v1/mail/folders?email=audit@example.com',
                headers=self._headers(secret),
            )
            self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute('SELECT * FROM api_keys WHERE id = ?', (key['id'],)).fetchone()
            self.assertEqual(int(row['request_count']), 2)
            self.assertEqual(row['last_used_ip'], '127.0.0.1')
            self.assertIsNotNone(row['last_used_at'])
            audit_count = db.execute(
                'SELECT COUNT(*) AS c FROM api_key_audit_logs WHERE api_key_id = ?',
                (key['id'],),
            ).fetchone()['c']
            self.assertEqual(int(audit_count), 2)

        logs = self.client.get(f'/api/settings/api-keys/{key["id"]}/audit').get_json()
        self.assertTrue(logs['success'])
        self.assertEqual(len(logs['logs']), 2)
        self.assertTrue(all(log['status_code'] == 200 for log in logs['logs']))

    def test_audit_prunes_to_limit(self):
        group_id = self._insert_group('审计上限')
        key, _secret = self._create_key([group_id])
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.executemany(
                '''
                INSERT INTO api_key_audit_logs
                    (api_key_id, key_prefix, method, path, status_code)
                VALUES (?, 'okv1_test', 'GET', '/api/v1/test', 200)
                ''',
                [(key['id'],) for _ in range(2100)],
            )
            db.commit()
            web_outlook_app._api_key_audit_insert_counters[key['id']] = 99
            web_outlook_app.prune_api_key_audit_logs(key['id'], db)
            db.commit()
            count = db.execute(
                'SELECT COUNT(*) AS c FROM api_key_audit_logs WHERE api_key_id = ?',
                (key['id'],),
            ).fetchone()['c']
            self.assertEqual(int(count), web_outlook_app.API_KEY_AUDIT_MAX_ROWS_PER_KEY)

    # ---------- route protection ----------

    def test_all_v1_routes_require_api_key(self):
        rules = [rule for rule in self.app.url_map.iter_rules()
                 if rule.rule.startswith('/api/v1/')]
        self.assertGreaterEqual(len(rules), 5)
        for rule in rules:
            view = self.app.view_functions[rule.endpoint]
            self.assertTrue(
                getattr(view, '_requires_api_key_v1', False),
                f'{rule.rule} 缺少 v1 API Key 保护',
            )


if __name__ == '__main__':
    unittest.main()
