import importlib
import os
import pathlib
import re
import sys
import tempfile
import unittest
import uuid
from urllib.parse import urlparse
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-group-share-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class GroupShareLinkTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM group_share_links')
            db.execute('DELETE FROM email_share_links')
            db.execute('DELETE FROM accounts')
            db.commit()

        web_outlook_app.group_share_attempts.clear()

    def _create_group(self, name=None):
        name = name or f"Group {uuid.uuid4().hex[:8]}"
        with self.app.app_context():
            group_id = web_outlook_app.add_group(name)
        self.assertIsNotNone(group_id)
        self.addCleanup(self._delete_group, group_id, name)
        return int(group_id)

    def _delete_group(self, group_id, name):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('UPDATE accounts SET group_id = 1 WHERE group_id = ?', (group_id,))
            db.execute('DELETE FROM group_share_links WHERE group_id = ?', (group_id,))
            db.execute('DELETE FROM groups WHERE id = ?', (group_id,))
            db.commit()

    def _insert_account(self, email_addr, group_id, status='active'):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            cursor = db.execute(
                '''
                INSERT INTO accounts (
                    email, password, client_id, refresh_token,
                    group_id, remark, status, account_type, provider,
                    imap_host, imap_port, imap_password, forward_enabled
                )
                VALUES (?, '', 'client-id', 'refresh-token', ?, '', ?, 'outlook',
                        'outlook', '', 993, '', 0)
                ''',
                (email_addr, group_id, status)
            )
            db.commit()
            return int(cursor.lastrowid)

    def _create_share(self, group_id, **payload):
        data = {'group_id': group_id, 'duration_minutes': 60}
        data.update(payload)
        response = self.client.post('/api/group-shares', json=data)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['success'])
        return body['share']

    def _token_from_share_url(self, share_url):
        return urlparse(share_url).path.rstrip('/').split('/')[-1]

    def _verify_access_code(self, client, token, code='Ab12'):
        return client.post(f'/api/share/group/{token}/verify', json={'access_code': code})

    def _verify_email(self, client, token, email_addr):
        return client.post(
            f'/api/share/group/{token}/verify-email',
            json={'email': email_addr},
        )

    def _granted_client(self, share, email_addr, group_id=None, access_code='Ab12'):
        token = self._token_from_share_url(share['share_url'])
        anonymous = self.app.test_client()
        if access_code:
            self.assertEqual(self._verify_access_code(anonymous, token, access_code).status_code, 200)
        self.assertEqual(self._verify_email(anonymous, token, email_addr).status_code, 200)
        return anonymous, token

    @staticmethod
    def _share_folder_result(emails, method='Graph API', request_method='graph', has_more=False):
        return {
            'success': True,
            'emails': [dict(email) for email in emails],
            'method': method,
            'request_method': request_method,
            'has_more': has_more,
        }

    def _make_share_folder_fetch(self, folder_emails, expected_top=web_outlook_app.EMAIL_SHARE_CANDIDATE_PAGE_SIZE):
        def fetch(_account, folder, skip, top):
            self.assertEqual(skip, 0)
            self.assertEqual(top, expected_top)
            return self._share_folder_result(folder_emails[folder])

        return fetch

    def test_create_validates_group_and_payload_and_hashes_access_code(self):
        group_id = self._create_group()
        self.assertEqual(self.client.post('/api/group-shares', json={
            'group_id': 999999, 'duration_minutes': 60,
        }).status_code, 404)

        anonymous = self.app.test_client()
        self.assertEqual(anonymous.post('/api/group-shares', json={
            'group_id': group_id, 'duration_minutes': 60,
        }).status_code, 401)

        for payload in (
            {'duration_minutes': 0},
            {'duration_minutes': 60, 'max_email_count': 0},
            {'duration_minutes': 60, 'max_email_count': 51},
            {'duration_minutes': 60, 'max_email_count': True},
            {'duration_minutes': 60, 'access_code': 'abc'},
            {'never_expires': 'maybe'},
        ):
            with self.subTest(payload=payload):
                data = {'group_id': group_id}
                data.update(payload)
                response = self.client.post('/api/group-shares', json=data)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()['success'])

        share = self._create_share(group_id, access_code='Ab12', max_email_count=2)
        self.assertTrue(share['requires_access_code'])
        self.assertEqual(share['access_code'], 'Ab12')
        self.assertTrue(share['access_code_available'])
        self.assertNotIn('access_code_hash', share)
        self.assertEqual(share['group_name'], self._group_name(group_id))
        self.assertEqual(share['max_email_count'], 2)
        self.assertTrue(share['share_url'].endswith('/share/group/' + self._token_from_share_url(share['share_url'])))

        with self.app.app_context():
            row = web_outlook_app.get_group_share_row_by_id(share['id'])
            self.assertNotEqual(row['access_code_hash'], 'Ab12')
            self.assertTrue(web_outlook_app.verify_password('Ab12', row['access_code_hash']))
            self.assertEqual(web_outlook_app.decrypt_data(row['access_code_encrypted']), 'Ab12')

        listed = self.client.get(f'/api/group-shares?group_id={group_id}').get_json()['shares']
        self.assertEqual([item['id'] for item in listed], [share['id']])
        self.assertEqual(listed[0]['access_code'], 'Ab12')
        self.assertNotIn('access_code_hash', listed[0])

    def test_management_marks_access_code_unavailable_when_ciphertext_is_missing(self):
        group_id = self._create_group()
        share = self._create_share(group_id, access_code='Ab12')
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'UPDATE group_share_links SET access_code_encrypted = NULL WHERE id = ?',
                (share['id'],)
            )
            db.commit()
        listed = self.client.get('/api/group-shares').get_json()['shares'][0]
        self.assertTrue(listed['requires_access_code'])
        self.assertFalse(listed['access_code_available'])
        self.assertEqual(listed['access_code'], '')

    def _group_name(self, group_id):
        with self.app.app_context():
            return web_outlook_app.get_group_by_id(group_id)['name']

    def test_access_code_gate_hides_group_name_and_blocks_read_routes(self):
        group_id = self._create_group()
        self._insert_account('shared@example.com', group_id)
        share = self._create_share(group_id, access_code='Ab12')
        token = self._token_from_share_url(share['share_url'])
        group_name = self._group_name(group_id)

        anonymous = self.app.test_client()
        page = anonymous.get(f'/share/group/{token}')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(group_name.encode(), page.data)
        self.assertIn('no-store', page.headers['Cache-Control'])

        with patch.object(web_outlook_app, 'fetch_account_emails') as list_mock, patch.object(
            web_outlook_app, 'fetch_email_detail_for_account'
        ) as detail_mock:
            for suffix in ('status', 'emails', 'email/msg-1'):
                response = anonymous.get(f'/api/share/group/{token}/{suffix}')
                self.assertEqual(response.status_code, 403)
                self.assertTrue(response.get_json()['requires_access_code'])
                self.assertNotIn('group_name', response.get_json())
                self.assertIn('no-store', response.headers['Cache-Control'])
            list_mock.assert_not_called()
            detail_mock.assert_not_called()

    def test_access_code_then_mailbox_verification_binds_session_and_grants_read_access(self):
        group_id = self._create_group()
        self._insert_account('shared@example.com', group_id)
        share = self._create_share(group_id, access_code='Ab12')
        token = self._token_from_share_url(share['share_url'])

        anonymous = self.app.test_client()
        self.assertEqual(self._verify_access_code(anonymous, token, 'wrong').status_code, 403)
        self.assertEqual(self._verify_access_code(anonymous, token).status_code, 200)

        pending = anonymous.get(f'/api/share/group/{token}/status')
        self.assertEqual(pending.status_code, 200)
        pending_body = pending.get_json()
        self.assertTrue(pending_body['requires_email'])
        self.assertEqual(pending_body['status'], 'mailbox_required')
        self.assertEqual(pending_body['group_name'], self._group_name(group_id))
        self.assertNotIn('email', pending_body)

        self.assertEqual(anonymous.get(f'/api/share/group/{token}/emails').status_code, 403)

        self.assertEqual(self._verify_email(anonymous, token, 'missing@example.com').status_code, 403)
        self.assertEqual(self._verify_email(anonymous, token, 'shared@example.com').status_code, 200)

        verified = anonymous.get(f'/api/share/group/{token}/status').get_json()
        self.assertEqual(verified['email'], 'shared@example.com')
        self.assertEqual(verified['group_name'], self._group_name(group_id))

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value={
            'success': True, 'emails': [],
        }) as list_mock, patch.object(web_outlook_app, 'fetch_email_detail_for_account', return_value={
            'success': True, 'email': {'id': 'msg-1'},
        }) as detail_mock:
            self.assertEqual(anonymous.get(f'/api/share/group/{token}/emails').status_code, 200)
            self.assertEqual(anonymous.get(f'/api/share/group/{token}/email/msg-1').status_code, 200)
            list_mock.assert_called_once()
            detail_mock.assert_called_once()

        other_client = self.app.test_client()
        self.assertEqual(other_client.get(f'/api/share/group/{token}/status').status_code, 403)
        self.assertEqual(other_client.get(f'/api/share/group/{token}/emails').status_code, 403)

    def test_mailbox_verification_accepts_only_primary_active_members(self):
        group_id = self._create_group()
        child_group_id = self._create_group()
        other_group_id = self._create_group()
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('UPDATE groups SET parent_id = ?, level = 2 WHERE id = ?', (group_id, child_group_id))
            db.commit()

        active_id = self._insert_account('member@example.com', group_id)
        self._insert_account('child@example.com', child_group_id)
        self._insert_account('inactive@example.com', group_id, status='inactive')
        self._insert_account('outsider@example.com', other_group_id)
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'INSERT INTO account_aliases (account_id, alias_email) VALUES (?, ?)',
                (active_id, 'alias@example.com')
            )
            db.commit()

        share = self._create_share(group_id)
        token = self._token_from_share_url(share['share_url'])
        anonymous = self.app.test_client()

        for email_addr in (
            'alias@example.com',
            'inactive@example.com',
            'outsider@example.com',
            'not-an-email',
        ):
            with self.subTest(email_addr=email_addr):
                response = self._verify_email(anonymous, token, email_addr)
                self.assertEqual(response.status_code, 403)
                self.assertGreaterEqual(response.get_json()['remaining_attempts'], 0)

        # Different case/whitespace is still the same primary mailbox.
        self.assertEqual(self._verify_email(anonymous, token, '  MEMBER@EXAMPLE.COM ').status_code, 200)
        self.assertEqual(self._verify_email(anonymous, token, 'child@example.com').status_code, 200)

    def test_five_consecutive_failures_block_for_five_minutes_and_success_resets(self):
        group_id = self._create_group()
        self._insert_account('member@example.com', group_id)
        share = self._create_share(group_id)
        token = self._token_from_share_url(share['share_url'])
        anonymous = self.app.test_client()

        for _ in range(4):
            self.assertEqual(self._verify_email(anonymous, token, 'wrong@example.com').status_code, 403)
        self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 200)

        for _ in range(4):
            self.assertEqual(self._verify_email(anonymous, token, 'wrong@example.com').status_code, 403)
        blocked = self._verify_email(anonymous, token, 'wrong@example.com')
        self.assertEqual(blocked.status_code, 429)
        retry_after = int(blocked.headers['Retry-After'])
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, web_outlook_app.GROUP_SHARE_LOCK_SECONDS)
        self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 429)

        later = web_outlook_app.time.time() + web_outlook_app.GROUP_SHARE_LOCK_SECONDS + 1
        with patch.object(web_outlook_app.time, 'time', return_value=later):
            self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 200)

    def test_failure_counter_resets_after_fifteen_minutes_without_failures(self):
        group_id = self._create_group()
        self._insert_account('member@example.com', group_id)
        share = self._create_share(group_id)
        token = self._token_from_share_url(share['share_url'])
        anonymous = self.app.test_client()

        for _ in range(4):
            self.assertEqual(self._verify_email(anonymous, token, 'wrong@example.com').status_code, 403)

        later = web_outlook_app.time.time() + web_outlook_app.GROUP_SHARE_FAILURE_RESET_SECONDS + 1
        with patch.object(web_outlook_app.time, 'time', return_value=later):
            response = self._verify_email(anonymous, token, 'wrong@example.com')
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()['remaining_attempts'], 4)
            self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 200)

    def test_limited_group_share_applies_per_folder_and_checks_detail_visibility(self):
        group_id = self._create_group()
        self._insert_account('member@example.com', group_id)
        share = self._create_share(group_id, max_email_count=1)
        anonymous = self.app.test_client()
        token = self._token_from_share_url(share['share_url'])
        self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 200)

        folder_emails = {
            'inbox': [
                {'id': 'inbox-visible', 'date': '2026-01-02T12:02:00Z', 'id_mode': 'graph'},
                {'id': 'inbox-hidden', 'date': '2026-01-02T12:01:00Z', 'id_mode': 'graph'},
            ],
            'junkemail': [
                {'id': 'junk-visible', 'date': '2026-01-02T12:03:00Z', 'id_mode': 'graph'},
            ],
        }
        with patch.object(
            web_outlook_app,
            'fetch_account_emails',
            side_effect=self._make_share_folder_fetch(folder_emails),
        ), patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            return_value={'success': True, 'email': {'id': 'inbox-visible'}},
        ) as detail_mock:
            inbox = anonymous.get(f'/api/share/group/{token}/emails?folder=inbox').get_json()
            junk = anonymous.get(f'/api/share/group/{token}/emails?folder=junkemail').get_json()
            self.assertEqual([email['id'] for email in inbox['emails']], ['inbox-visible'])
            self.assertEqual([email['id'] for email in junk['emails']], ['junk-visible'])

            allowed = anonymous.get(
                f'/api/share/group/{token}/email/inbox-visible?folder=inbox&id_mode=graph'
            )
            self.assertEqual(allowed.status_code, 200)
            detail_mock.assert_called_once()
            detail_mock.reset_mock()
            hidden = anonymous.get(
                f'/api/share/group/{token}/email/inbox-hidden?folder=inbox&id_mode=graph'
            )
            self.assertEqual(hidden.status_code, 404)
            detail_mock.assert_not_called()

    def test_membership_change_and_group_deletion_revoke_access(self):
        group_id = self._create_group()
        account_id = self._insert_account('member@example.com', group_id)
        share = self._create_share(group_id)
        anonymous, token = self._granted_client(share, 'member@example.com', access_code='')

        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('UPDATE accounts SET group_id = 1 WHERE id = ?', (account_id,))
            db.commit()

        self.assertEqual(anonymous.get(f'/api/share/group/{token}/status').status_code, 200)
        self.assertTrue(anonymous.get(f'/api/share/group/{token}/status').get_json()['requires_email'])
        self.assertEqual(anonymous.get(f'/api/share/group/{token}/emails').status_code, 403)

        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('UPDATE accounts SET group_id = ? WHERE id = ?', (group_id, account_id))
            db.commit()
        self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 200)

        with self.app.app_context():
            result = web_outlook_app.delete_group_tree(group_id)
            self.assertTrue(result['success'])
        self.assertEqual(anonymous.get(f'/api/share/group/{token}/status').status_code, 404)

    def test_management_cancel_delete_and_batch_operations(self):
        group_id = self._create_group()
        shares = [self._create_share(group_id) for _ in range(4)]
        ids = [share['id'] for share in shares]

        cancelled = self.client.post(f'/api/group-shares/{ids[0]}/cancel')
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()['share']['status'], 'revoked')

        self.assertEqual(self.client.post('/api/group-shares/batch-cancel', json={
            'share_ids': ids[1:3],
        }).status_code, 200)
        listed = {
            share['id']: share
            for share in self.client.get('/api/group-shares').get_json()['shares']
        }
        self.assertEqual(listed[ids[1]]['status'], 'revoked')
        self.assertEqual(listed[ids[2]]['status'], 'revoked')
        self.assertEqual(listed[ids[3]]['status'], 'active')

        self.assertEqual(self.client.delete(f'/api/group-shares/{ids[0]}').status_code, 200)
        self.assertEqual(self.client.post('/api/group-shares/batch-delete', json={
            'share_ids': [ids[1], ids[2]],
        }).status_code, 200)
        remaining = [share['id'] for share in self.client.get('/api/group-shares').get_json()['shares']]
        self.assertEqual(remaining, [ids[3]])

    def test_revoked_expired_and_deleted_shares_are_invalid_for_visitors(self):
        group_id = self._create_group()
        self._insert_account('member@example.com', group_id)
        for invalidation in ('expired', 'revoked', 'deleted'):
            with self.subTest(invalidation=invalidation):
                share = self._create_share(group_id)
                token = self._token_from_share_url(share['share_url'])
                anonymous = self.app.test_client()
                self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 200)
                if invalidation == 'expired':
                    with self.app.app_context():
                        db = web_outlook_app.get_db()
                        db.execute(
                            "UPDATE group_share_links SET expires_at = '2000-01-01 00:00:00' WHERE id = ?",
                            (share['id'],)
                        )
                        db.commit()
                elif invalidation == 'revoked':
                    self.client.post(f"/api/group-shares/{share['id']}/cancel")
                else:
                    self.client.delete(f"/api/group-shares/{share['id']}")
                for suffix in ('status', 'emails', 'email/msg-1'):
                    self.assertEqual(anonymous.get(f'/api/share/group/{token}/{suffix}').status_code, 404)
                self.assertEqual(self._verify_email(anonymous, token, 'member@example.com').status_code, 404)

    def test_group_access_code_verification_supports_csrf_protection(self):
        if not web_outlook_app.CSRF_AVAILABLE:
            self.skipTest('Flask-WTF is unavailable')
        group_id = self._create_group()
        self._insert_account('member@example.com', group_id)
        share = self._create_share(group_id, access_code='Ab12')
        token = self._token_from_share_url(share['share_url'])
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.addCleanup(self.app.config.update, WTF_CSRF_ENABLED=False)
        anonymous = self.app.test_client()
        page = anonymous.get(f'/share/group/{token}').get_data(as_text=True)
        csrf_token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
        url = f'/api/share/group/{token}/verify'
        self.assertEqual(anonymous.post(url, json={'access_code': 'Ab12'}).status_code, 400)
        response = anonymous.post(
            url,
            json={'access_code': 'Ab12'},
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
