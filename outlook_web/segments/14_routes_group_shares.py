from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    # These segmented files are executed into the shared `web_outlook_app`
    # globals at runtime. Importing from the assembled module keeps IDE
    # inspections from flagging the shared names as unresolved.
    from web_outlook_app import *  # noqa: F403


GROUP_SHARE_STATUS_MAILBOX_REQUIRED = 'mailbox_required'
GROUP_SHARE_MAX_VERIFIED_LINKS = 20
GROUP_SHARE_FAILURE_LIMIT = 5
GROUP_SHARE_LOCK_SECONDS = 300
GROUP_SHARE_FAILURE_RESET_SECONDS = 15 * 60

# 邮箱验证失败记录，仅保存在进程内存中（与登录限流一致）：
# {key: {'count': int, 'last_attempt': timestamp, 'locked_until': timestamp}}
group_share_attempts: Dict[str, Dict[str, float]] = {}

GROUP_SHARE_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def build_group_share_url(token: str) -> str:
    if not token:
        return ''
    return url_for('view_group_share', token=token, _external=True)


def get_group_share_status(share: Optional[Dict[str, Any]]) -> str:
    if not share or not share.get('group_id'):
        return EMAIL_SHARE_STATUS_INVALID
    _, max_email_count_error = normalize_persisted_email_share_max_email_count(
        share.get('max_email_count')
    )
    if max_email_count_error:
        return EMAIL_SHARE_STATUS_INVALID
    if share.get('revoked_at'):
        return EMAIL_SHARE_STATUS_REVOKED
    if int(share.get('never_expires') or 0):
        return EMAIL_SHARE_STATUS_ACTIVE
    expires_at = parse_share_timestamp(share.get('expires_at'))
    if not expires_at or expires_at <= utc_now_naive():
        return EMAIL_SHARE_STATUS_EXPIRED
    return EMAIL_SHARE_STATUS_ACTIVE


def get_group_share_access_code(share: Dict[str, Any]) -> str:
    encrypted = str(share.get('access_code_encrypted') or '')
    if not encrypted:
        return ''
    try:
        return decrypt_data(encrypted)
    except Exception:
        app.logger.warning('Failed to decrypt group share access code for share id=%s', share.get('id'))
        return ''


def serialize_group_share_row(row: Any) -> Dict[str, Any]:
    share = dict(row)
    status = get_group_share_status(share)
    token = ''
    share_url = ''
    try:
        token = decrypt_data(str(share.get('token_encrypted') or ''))
    except Exception:
        app.logger.warning('Failed to decrypt group share token for share id=%s', share.get('id'))
    if token:
        try:
            share_url = build_group_share_url(token)
        except Exception:
            app.logger.warning('Failed to build group share URL for share id=%s', share.get('id'))
    access_code = get_group_share_access_code(share)

    return {
        'id': share.get('id'),
        'group_id': share.get('group_id'),
        'group_name': share.get('group_name') or '',
        'expires_at': share.get('expires_at'),
        'never_expires': bool(share.get('never_expires')),
        'max_email_count': get_email_share_max_email_count(share),
        'revoked_at': share.get('revoked_at'),
        'last_accessed_at': share.get('last_accessed_at'),
        'created_at': share.get('created_at'),
        'updated_at': share.get('updated_at'),
        'status': status,
        'share_url': share_url,
        'requires_access_code': bool(share.get('access_code_hash')),
        'access_code': access_code,
        'access_code_available': bool(access_code),
    }


def query_group_share_rows(group_id: Optional[int] = None) -> list[Dict[str, Any]]:
    db = get_db()
    params = []
    where_sql = ''
    if group_id is not None:
        where_sql = 'WHERE s.group_id = ?'
        params.append(int(group_id))
    rows = db.execute(
        f'''
        SELECT s.*, g.name AS group_name
        FROM group_share_links s
        JOIN groups g ON g.id = s.group_id
        {where_sql}
        ORDER BY s.created_at DESC, s.id DESC
        ''',
        tuple(params)
    ).fetchall()
    return [serialize_group_share_row(row) for row in rows]


def get_group_share_row_by_id(share_id: int) -> Optional[Dict[str, Any]]:
    row = get_db().execute(
        '''
        SELECT s.*, g.name AS group_name
        FROM group_share_links s
        JOIN groups g ON g.id = s.group_id
        WHERE s.id = ?
        LIMIT 1
        ''',
        (int(share_id),)
    ).fetchone()
    return dict(row) if row else None


def get_group_share_row_by_token(token: str) -> Optional[Dict[str, Any]]:
    normalized_token = str(token or '').strip()
    if not normalized_token:
        return None
    row = get_db().execute(
        '''
        SELECT s.*, g.name AS group_name
        FROM group_share_links s
        JOIN groups g ON g.id = s.group_id
        WHERE s.token_hash = ?
        LIMIT 1
        ''',
        (hash_email_share_token(normalized_token),)
    ).fetchone()
    return dict(row) if row else None


def touch_group_share_access(share_id: int) -> None:
    db = get_db()
    db.execute(
        '''
        UPDATE group_share_links
        SET last_accessed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (int(share_id),)
    )
    db.commit()


def get_group_share_member_group_ids(share: Dict[str, Any]) -> list[int]:
    """分组分享的成员范围与分组列表一致：包含所有后代分组。"""
    group_id = int(share.get('group_id') or 0)
    if group_id <= 0:
        return []
    return get_descendant_group_ids(group_id) or [group_id]


def is_valid_group_share_email(email_addr: Any) -> bool:
    return bool(GROUP_SHARE_EMAIL_PATTERN.fullmatch(str(email_addr or '').strip()))


def find_group_share_account(share: Dict[str, Any], email_addr: Any) -> Optional[Dict[str, Any]]:
    """按主邮箱查找分组内处于启用状态的账号；别名不算匹配。"""
    if not is_valid_group_share_email(email_addr):
        return None
    normalized = normalize_email_address(email_addr)
    group_ids = get_group_share_member_group_ids(share)
    if not normalized or not group_ids:
        return None
    placeholders = ','.join('?' * len(group_ids))
    row = get_db().execute(
        f'''
        SELECT *
        FROM accounts
        WHERE LOWER(email) = ?
          AND LOWER(COALESCE(status, 'active')) = 'active'
          AND group_id IN ({placeholders})
        LIMIT 1
        ''',
        [normalized] + group_ids
    ).fetchone()
    return resolve_account_record(row) if row else None


def group_share_grant_prefix(share: Dict[str, Any]) -> str:
    return f"{share.get('token_hash') or ''}:"


def get_group_share_granted_account_id(share: Dict[str, Any]) -> Optional[int]:
    prefix = group_share_grant_prefix(share)
    grants = session.get('group_share_access', [])
    if not isinstance(grants, list):
        return None
    for value in reversed(grants):
        if not isinstance(value, str) or not value.startswith(prefix):
            continue
        try:
            return int(value[len(prefix):])
        except (TypeError, ValueError):
            continue
    return None


def remove_group_share_mailbox_grant(share: Dict[str, Any]) -> None:
    prefix = group_share_grant_prefix(share)
    grants = session.get('group_share_access', [])
    if not isinstance(grants, list):
        session['group_share_access'] = []
        return
    session['group_share_access'] = [
        value for value in grants
        if not (isinstance(value, str) and value.startswith(prefix))
    ]


def grant_group_share_mailbox(share: Dict[str, Any], account_id: int) -> None:
    prefix = group_share_grant_prefix(share)
    grants = session.get('group_share_access', [])
    if not isinstance(grants, list):
        grants = []
    grants = [
        value for value in grants
        if not (isinstance(value, str) and value.startswith(prefix))
    ]
    # 限制已验证链接数，避免 Flask 的 Cookie Session 随访问次数无限增长。
    session['group_share_access'] = (
        grants + [f"{prefix}{int(account_id)}"]
    )[-GROUP_SHARE_MAX_VERIFIED_LINKS:]


def get_group_share_granted_account(share: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    account_id = get_group_share_granted_account_id(share)
    if not account_id:
        return None
    account = get_account_by_id(account_id)
    if not account:
        remove_group_share_mailbox_grant(share)
        return None
    if str(account.get('status') or 'active').strip().lower() != 'active':
        remove_group_share_mailbox_grant(share)
        return None
    group_ids = get_group_share_member_group_ids(share)
    if int(account.get('group_id') or 0) not in group_ids:
        remove_group_share_mailbox_grant(share)
        return None
    return account


def resolve_active_group_share(
    token: str
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    share = get_group_share_row_by_token(token)
    if not share:
        return None, None, EMAIL_SHARE_STATUS_INVALID

    status = get_group_share_status(share)
    if status != EMAIL_SHARE_STATUS_ACTIVE:
        return share, None, status

    if not has_email_share_access(share):
        return share, None, EMAIL_SHARE_STATUS_ACCESS_CODE_REQUIRED

    account = get_group_share_granted_account(share)
    if not account:
        return share, None, GROUP_SHARE_STATUS_MAILBOX_REQUIRED

    if should_touch_email_share_access(share):
        touch_group_share_access(int(share['id']))
    return share, account, status


def group_share_error_payload(status: str) -> Dict[str, Any]:
    if status == GROUP_SHARE_STATUS_MAILBOX_REQUIRED:
        return {
            'success': False,
            'status': status,
            'requires_email': True,
            'error': '请先验证分组内的完整邮箱地址',
        }
    return email_share_error_payload(status)


def group_share_error_response(status: str):
    payload = group_share_error_payload(status)
    if status in (EMAIL_SHARE_STATUS_ACCESS_CODE_REQUIRED, GROUP_SHARE_STATUS_MAILBOX_REQUIRED):
        return jsonify(payload), 403
    return jsonify(payload), 404


def group_share_attempt_key(share: Dict[str, Any]) -> str:
    return f"group-share:{share.get('token_hash') or ''}:{request.remote_addr or ''}"


def prune_group_share_attempt(key: str) -> Optional[Dict[str, float]]:
    state = group_share_attempts.get(key)
    if not state:
        return None
    now = time.time()
    if now - float(state.get('last_attempt') or 0) > GROUP_SHARE_FAILURE_RESET_SECONDS:
        group_share_attempts.pop(key, None)
        return None
    return state


def get_group_share_block_remaining(key: str) -> int:
    state = prune_group_share_attempt(key)
    if not state:
        return 0
    now = time.time()
    locked_until = float(state.get('locked_until') or 0)
    if locked_until > now:
        return max(1, int(math.ceil(locked_until - now)))
    return 0


def record_group_share_failure(key: str) -> int:
    now = time.time()
    state = prune_group_share_attempt(key) or {
        'count': 0,
        'last_attempt': now,
        'locked_until': 0,
    }
    state['count'] = int(state.get('count') or 0) + 1
    state['last_attempt'] = now
    if state['count'] >= GROUP_SHARE_FAILURE_LIMIT:
        state['locked_until'] = now + GROUP_SHARE_LOCK_SECONDS
        group_share_attempts[key] = state
        return GROUP_SHARE_LOCK_SECONDS
    group_share_attempts[key] = state
    return 0


def reset_group_share_attempts(key: str) -> None:
    group_share_attempts.pop(key, None)


def group_share_locked_response(retry_after: int):
    response = jsonify({
        'success': False,
        'error': f'邮箱验证失败次数过多，请 {max(1, int(math.ceil(retry_after / 60)))} 分钟后再试',
        'retry_after': retry_after,
    })
    response.headers['Retry-After'] = str(max(1, retry_after))
    return response, 429


@app.route('/api/group-shares', methods=['POST'])
@login_required
def api_create_group_share():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}

    try:
        group_id = int(data.get('group_id'))
    except (TypeError, ValueError):
        group_id = 0
    group = get_group_by_id(group_id) if group_id > 0 else None
    if not group:
        return jsonify({'success': False, 'error': '分组不存在'}), 404

    duration_minutes, never_expires, max_email_count, payload_error = parse_email_share_create_payload(data)
    if payload_error:
        return jsonify({'success': False, 'error': payload_error}), 400

    access_code, access_code_error = normalize_email_share_access_code(data.get('access_code'))
    if access_code_error:
        return jsonify({'success': False, 'error': access_code_error}), 400

    token = generate_email_share_token()
    expires_at = None if never_expires else utc_timestamp(duration_minutes)
    db = get_db()
    cursor = db.execute(
        '''
        INSERT INTO group_share_links (
            group_id, token_hash, token_encrypted, expires_at, never_expires, max_email_count,
            access_code_hash, access_code_encrypted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            int(group['id']),
            hash_email_share_token(token),
            encrypt_data(token),
            expires_at,
            1 if never_expires else 0,
            max_email_count,
            hash_password(access_code) if access_code else None,
            encrypt_data(access_code) if access_code else None,
        )
    )
    db.commit()

    share = get_group_share_row_by_id(int(cursor.lastrowid))
    return jsonify({
        'success': True,
        'share': serialize_group_share_row(share),
    })


@app.route('/api/group-shares', methods=['GET'])
@login_required
def api_list_group_shares():
    group_id = request.args.get('group_id')
    normalized_group_id = None
    if group_id is not None and str(group_id).strip() != '':
        try:
            normalized_group_id = int(group_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'group_id 参数无效'}), 400
        if normalized_group_id <= 0:
            return jsonify({'success': False, 'error': 'group_id 参数无效'}), 400
    return jsonify({'success': True, 'shares': query_group_share_rows(normalized_group_id)})


@app.route('/api/group-shares/<int:share_id>/cancel', methods=['POST'])
@login_required
def api_cancel_group_share(share_id):
    share = get_group_share_row_by_id(share_id)
    if not share:
        return jsonify({'success': False, 'error': '分享记录不存在'}), 404

    db = get_db()
    db.execute(
        '''
        UPDATE group_share_links
        SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (int(share_id),)
    )
    db.commit()
    updated = get_group_share_row_by_id(share_id)
    return jsonify({'success': True, 'share': serialize_group_share_row(updated)})


@app.route('/api/group-shares/<int:share_id>', methods=['DELETE'])
@login_required
def api_delete_group_share(share_id):
    share = get_group_share_row_by_id(share_id)
    if not share:
        return jsonify({'success': False, 'error': '分享记录不存在'}), 404

    db = get_db()
    try:
        db.execute('DELETE FROM group_share_links WHERE id = ?', (int(share_id),))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'删除失败: {str(e)}'})


@app.route('/api/group-shares/batch-cancel', methods=['POST'])
@login_required
def api_batch_cancel_group_shares():
    data = request.get_json(silent=True) or {}
    share_ids = data.get('share_ids') or []
    if not isinstance(share_ids, list):
        return jsonify({'success': False, 'error': '参数无效'}), 400
    if not share_ids:
        return jsonify({'success': True, 'message': '未选择任何记录'})

    try:
        ids = [int(x) for x in share_ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '包含无效的 ID'}), 400

    db = get_db()
    try:
        placeholders = ','.join(['?'] * len(ids))
        db.execute(
            f'''
            UPDATE group_share_links
            SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            ''',
            tuple(ids)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'批量取消失败: {str(e)}'})


@app.route('/api/group-shares/batch-delete', methods=['POST'])
@login_required
def api_batch_delete_group_shares():
    data = request.get_json(silent=True) or {}
    share_ids = data.get('share_ids') or []
    if not isinstance(share_ids, list):
        return jsonify({'success': False, 'error': '参数无效'}), 400
    if not share_ids:
        return jsonify({'success': True, 'message': '未选择任何记录'})

    try:
        ids = [int(x) for x in share_ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '包含无效的 ID'}), 400

    db = get_db()
    try:
        placeholders = ','.join(['?'] * len(ids))
        db.execute(
            f'DELETE FROM group_share_links WHERE id IN ({placeholders})',
            tuple(ids)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'批量删除失败: {str(e)}'})


@app.route('/share/group/<token>')
def view_group_share(token):
    share = get_group_share_row_by_token(token)
    status = get_group_share_status(share or {})
    group_name = ''
    if status == EMAIL_SHARE_STATUS_ACTIVE:
        if not has_email_share_access(share):
            status = EMAIL_SHARE_STATUS_ACCESS_CODE_REQUIRED
        else:
            group_name = share.get('group_name', '')
            if not get_group_share_granted_account(share):
                status = GROUP_SHARE_STATUS_MAILBOX_REQUIRED
    return render_template(
        'email_share.html',
        share_kind='group',
        share_token=token,
        share_status=status,
        group_name=group_name,
        account_email='',
    )


@app.route('/api/share/group/<token>/verify', methods=['POST'])
def api_verify_group_share_access_code(token):
    share = get_group_share_row_by_token(token)
    status = get_group_share_status(share or {})
    if status != EMAIL_SHARE_STATUS_ACTIVE:
        return group_share_error_response(status)
    if not share.get('access_code_hash'):
        return jsonify({'success': True})

    rate_limit_key = f"group-share-code:{share['token_hash']}:{request.remote_addr}"
    locked_until = login_attempts.get(rate_limit_key, {}).get('locked_until')
    if locked_until is not None and time.time() >= locked_until:
        reset_login_attempts(rate_limit_key)
    allowed, retry_after = check_rate_limit(rate_limit_key)
    if not allowed:
        response = jsonify({'success': False, 'error': '访问码尝试次数过多，请稍后重试'})
        response.headers['Retry-After'] = str(max(1, retry_after))
        return response, 429

    data = request.get_json(silent=True)
    access_code, error = normalize_email_share_access_code(
        data.get('access_code') if isinstance(data, dict) else None
    )
    if error or not access_code or not verify_password(access_code, share['access_code_hash']):
        record_login_failure(rate_limit_key)
        payload = email_share_error_payload(EMAIL_SHARE_STATUS_ACCESS_CODE_REQUIRED)
        payload['error'] = '访问码错误，请重试'
        return jsonify(payload), 403

    reset_login_attempts(rate_limit_key)
    grant = email_share_access_grant(share)
    grants = [value for value in session.get('email_share_access', []) if value != grant]
    session['email_share_access'] = (grants + [grant])[-EMAIL_SHARE_MAX_VERIFIED_LINKS:]
    return jsonify({'success': True})


@app.route('/api/share/group/<token>/status', methods=['GET'])
def api_group_share_status(token):
    share, account, status = resolve_active_group_share(token)
    if status == GROUP_SHARE_STATUS_MAILBOX_REQUIRED:
        return jsonify({
            'success': True,
            'status': status,
            'requires_email': True,
            'group_name': share.get('group_name', ''),
            'never_expires': bool(share.get('never_expires')),
            'expires_at': share.get('expires_at'),
            'max_email_count': get_email_share_max_email_count(share),
        })
    if status != EMAIL_SHARE_STATUS_ACTIVE:
        return group_share_error_response(status)

    return jsonify({
        'success': True,
        'status': status,
        'group_name': share.get('group_name', ''),
        'email': account.get('email', ''),
        'never_expires': bool(share.get('never_expires')),
        'expires_at': share.get('expires_at'),
        'max_email_count': get_email_share_max_email_count(share),
    })


@app.route('/api/share/group/<token>/verify-email', methods=['POST'])
def api_group_share_verify_email(token):
    share, _, status = resolve_active_group_share(token)
    if status not in (EMAIL_SHARE_STATUS_ACTIVE, GROUP_SHARE_STATUS_MAILBOX_REQUIRED):
        return group_share_error_response(status)

    attempt_key = group_share_attempt_key(share)
    blocked_for = get_group_share_block_remaining(attempt_key)
    if blocked_for:
        return group_share_locked_response(blocked_for)

    data = request.get_json(silent=True)
    email_addr = data.get('email') if isinstance(data, dict) else None
    account = find_group_share_account(share, email_addr)
    if not account:
        locked_for = record_group_share_failure(attempt_key)
        if locked_for:
            return group_share_locked_response(locked_for)
        state = group_share_attempts.get(attempt_key) or {}
        attempts = int(state.get('count') or 0)
        return jsonify({
            'success': False,
            'error': '邮箱不存在或不属于该分组',
            'remaining_attempts': max(0, GROUP_SHARE_FAILURE_LIMIT - attempts),
        }), 403

    reset_group_share_attempts(attempt_key)
    grant_group_share_mailbox(share, int(account['id']))
    if should_touch_email_share_access(share):
        touch_group_share_access(int(share['id']))
    return jsonify({
        'success': True,
        'email': account.get('email', ''),
        'group_name': share.get('group_name', ''),
    })


@app.route('/api/share/group/<token>/switch-mailbox', methods=['POST'])
def api_group_share_switch_mailbox(token):
    share = get_group_share_row_by_token(token)
    status = get_group_share_status(share or {})
    if status != EMAIL_SHARE_STATUS_ACTIVE:
        return group_share_error_response(status)
    if not has_email_share_access(share):
        return group_share_error_response(EMAIL_SHARE_STATUS_ACCESS_CODE_REQUIRED)

    # 只清除已验证邮箱，保留同一链接的访问码授权，避免切换时重复输入访问码。
    remove_group_share_mailbox_grant(share)
    return jsonify({
        'success': True,
        'status': GROUP_SHARE_STATUS_MAILBOX_REQUIRED,
        'requires_email': True,
        'group_name': share.get('group_name', ''),
    })


@app.route('/api/share/group/<token>/emails', methods=['GET'])
def api_group_share_get_emails(token):
    folder, folder_error = normalize_email_share_folder_response(request.args.get('folder', 'inbox'))
    if folder_error:
        return folder_error

    share, account, status = resolve_active_group_share(token)
    if status != EMAIL_SHARE_STATUS_ACTIVE:
        return group_share_error_response(status)

    skip = parse_non_negative_int(request.args.get('skip', 0), 0)
    top = parse_non_negative_int(request.args.get('top', 20), 20, 50)
    max_email_count = get_email_share_max_email_count(share)
    if max_email_count is not None:
        return jsonify(fetch_limited_email_share_emails(
            account, folder, skip, top, max_email_count
        ))
    return jsonify(fetch_account_emails(account, folder, skip, top))


@app.route('/api/share/group/<token>/email/<path:message_id>', methods=['GET'])
def api_group_share_get_email_detail(token, message_id):
    folder, folder_error = normalize_email_share_folder_response(request.args.get('folder', 'inbox'))
    if folder_error:
        return folder_error

    share, account, status = resolve_active_group_share(token)
    if status != EMAIL_SHARE_STATUS_ACTIVE:
        return group_share_error_response(status)

    method = request.args.get('method', 'graph')
    id_mode = normalize_email_share_message_id_mode(request.args.get('id_mode'))
    max_email_count = get_email_share_max_email_count(share)
    if max_email_count is not None:
        visible_result = resolve_email_share_visible_messages(account, max_email_count, folder)
        if not visible_result.get('success'):
            return jsonify(visible_result)
        visible_email = find_email_share_visible_message(
            visible_result['emails'], message_id, folder, id_mode
        )
        if not visible_email:
            return jsonify({'success': False, 'error': '邮件不存在或无权访问'}), 404
        method = get_email_share_visible_message_method(visible_email)

    return jsonify(fetch_email_detail_for_account(
        account,
        message_id,
        method,
        folder,
        id_mode,
        strict_id_mode=max_email_count is not None,
        force_method=max_email_count is not None,
    ))
