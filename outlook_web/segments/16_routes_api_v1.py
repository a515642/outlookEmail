# -*- coding: utf-8 -*-
"""对外 API v1：邮箱领取与邮件读取。

所有路由都必须同时具备：
- `@csrf_exempt`（对外接口无浏览器会话）
- `api_v1_scope_required(<scope>)`（新的多 Key 权限模型）
旧版 `/api/external/*` 不在本段处理。
"""

API_V1_FOLDER_NAMES = ('inbox', 'junkemail', 'deleteditems')


def api_v1_success(data: Any = None, **extra) -> Any:
    payload = {'success': True}
    if data is not None:
        payload['data'] = data
    payload.update(extra)
    return jsonify(payload)


def get_api_v1_json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def serialize_api_v1_mail_list_item(item: Dict[str, Any], fallback_folder: str) -> Dict[str, Any]:
    row = dict(item or {})
    return {
        'id': str(row.get('id') or ''),
        'subject': str(row.get('subject') or '无主题'),
        'from': str(row.get('from') or '未知'),
        'to': str(row.get('to') or ''),
        'date': str(row.get('date') or ''),
        'is_read': bool(row.get('is_read', False)),
        'has_attachments': bool(row.get('has_attachments', False)),
        'body_preview': str(row.get('body_preview') or ''),
        'folder': str(row.get('folder') or fallback_folder),
        'id_mode': str(row.get('id_mode') or ''),
    }


def resolve_api_v1_scoped_account(principal, email_addr: str):
    """解析邮箱并立即校验分组范围；失败时返回 (None, error_response)。"""
    if not email_addr:
        return None, api_v1_error('missing_email', '缺少 email 参数', 400)
    account = resolve_account_for_email_api(email_addr)
    if not account:
        return None, api_v1_error('account_not_found', '邮箱账号不存在', 404)
    if not api_key_account_allowed(principal, account):
        return None, api_v1_error('group_not_allowed', '该邮箱不在当前 API Key 的授权分组内', 403)
    return account, None


@app.route('/api/v1/accounts/claim', methods=['POST'])
@csrf_exempt
@api_v1_scope_required(API_KEY_SCOPE_ACCOUNT_CLAIM)
def api_v1_claim_account():
    principal = g.api_v1_principal
    key_id = principal['id']
    db = get_db()
    effective_groups = get_api_key_effective_group_ids(key_id, db=db)
    if not effective_groups:
        return api_v1_error('no_account_available', '当前 API Key 没有可领取的邮箱', 404)

    placeholders = ','.join('?' * len(effective_groups))
    params = list(effective_groups) + [key_id, key_id]
    candidate_query = f'''
        SELECT a.id, a.email, a.group_id, g.name AS group_name
        FROM accounts a
        LEFT JOIN groups g ON g.id = a.group_id
        WHERE a.status = 'active'
          AND a.group_id IN ({placeholders})
          AND a.id NOT IN (
              SELECT c.account_id FROM api_key_claims c
              WHERE c.api_key_id = ?
                AND c.account_id IS NOT NULL
                AND c.released_at IS NULL
                AND c.claimed_at > datetime('now', '-10 minutes')
          )
          AND LOWER(a.email) NOT IN (
              SELECT c.normalized_email FROM api_key_claims c
              WHERE c.api_key_id = ?
                AND c.released_at IS NULL
                AND c.claimed_at > datetime('now', '-10 minutes')
          )
        ORDER BY RANDOM()
        LIMIT 1
    '''

    try:
        db.execute('BEGIN IMMEDIATE')
        candidate = db.execute(candidate_query, params).fetchone()
        if not candidate:
            db.rollback()
            return api_v1_error('no_account_available', '暂无可领取的邮箱', 404)
        db.execute(
            '''
            INSERT INTO api_key_claims
                (api_key_id, account_id, normalized_email, email_snapshot)
            VALUES (?, ?, ?, ?)
            ''',
            (
                key_id,
                int(candidate['id']),
                normalize_email_address(candidate['email']),
                candidate['email'],
            ),
        )
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        app.logger.warning('API v1 领取邮箱失败: %s', exc)
        return api_v1_error('claim_failed', '领取邮箱失败，请稍后重试', 500)

    return api_v1_success({
        'email': candidate['email'],
        'group_id': int(candidate['group_id']),
        'group_name': candidate['group_name'] or '',
    })


@app.route('/api/v1/accounts/release', methods=['POST'])
@csrf_exempt
@api_v1_scope_required(API_KEY_SCOPE_ACCOUNT_CLAIM)
def api_v1_release_account():
    principal = g.api_v1_principal
    body = get_api_v1_json_body()
    email_addr = str(body.get('email') or '').strip()
    if not email_addr:
        return api_v1_error('missing_email', '缺少 email 参数', 400)
    normalized_email = normalize_email_address(email_addr)

    db = get_db()
    row = db.execute(
        '''
        SELECT id FROM api_key_claims
        WHERE api_key_id = ?
          AND normalized_email = ?
          AND released_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        ''',
        (principal['id'], normalized_email),
    ).fetchone()
    if not row:
        return api_v1_error('claim_not_found', '未找到当前 Key 对该邮箱的有效领取记录', 404)

    db.execute(
        '''
        UPDATE api_key_claims
        SET released_at = CURRENT_TIMESTAMP, release_reason = 'explicit'
        WHERE id = ?
        ''',
        (int(row['id']),),
    )
    db.commit()
    return api_v1_success({'email': normalized_email, 'released': True})


@app.route('/api/v1/mail/folders', methods=['GET'])
@csrf_exempt
@api_v1_scope_required(API_KEY_SCOPE_MAIL_READ)
def api_v1_mail_folders():
    principal = g.api_v1_principal
    email_addr = get_query_arg_preserve_plus('email', '').strip()
    _account, error_response = resolve_api_v1_scoped_account(principal, email_addr)
    if error_response is not None:
        return error_response
    return api_v1_success({
        'folders': [
            {'name': 'inbox', 'label': '收件箱'},
            {'name': 'junkemail', 'label': '垃圾邮件'},
            {'name': 'deleteditems', 'label': '已删除'},
        ],
    })


@app.route('/api/v1/mail/messages', methods=['GET'])
@csrf_exempt
@api_v1_scope_required(API_KEY_SCOPE_MAIL_READ)
def api_v1_mail_messages():
    principal = g.api_v1_principal
    email_addr = get_query_arg_preserve_plus('email', '').strip()
    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    skip = parse_non_negative_int(request.args.get('skip', 0), 0)
    top = parse_non_negative_int(request.args.get('top', 20), 20, 50)

    if folder not in API_V1_FOLDER_NAMES:
        return api_v1_error(
            'invalid_folder',
            f'folder 参数无效，支持: {", ".join(API_V1_FOLDER_NAMES)}',
            400,
        )

    account, error_response = resolve_api_v1_scoped_account(principal, email_addr)
    if error_response is not None:
        return error_response

    try:
        result = fetch_account_emails(account, folder, skip, top)
    except Exception as exc:
        app.logger.warning('API v1 获取邮件列表失败: %s', exc)
        result = {'success': False, 'error': '获取邮件失败'}

    if not result.get('success'):
        return api_v1_error('mail_fetch_failed', '获取邮件失败，请稍后重试', 502)

    emails = [
        serialize_api_v1_mail_list_item(item, folder)
        for item in result.get('emails', [])
        if isinstance(item, dict)
    ]
    return api_v1_success({
        'email': account.get('email', ''),
        'folder': folder,
        'skip': skip,
        'top': top,
        'emails': emails,
        'has_more': bool(result.get('has_more')),
    })


@app.route('/api/v1/mail/messages/<path:message_id>', methods=['GET'])
@csrf_exempt
@api_v1_scope_required(API_KEY_SCOPE_MAIL_READ)
def api_v1_mail_message_detail(message_id):
    principal = g.api_v1_principal
    email_addr = get_query_arg_preserve_plus('email', '').strip()
    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    method = str(request.args.get('method') or 'graph').strip().lower()
    id_mode = str(request.args.get('id_mode') or '').strip().lower()

    if folder not in API_V1_FOLDER_NAMES:
        return api_v1_error(
            'invalid_folder',
            f'folder 参数无效，支持: {", ".join(API_V1_FOLDER_NAMES)}',
            400,
        )
    if method not in ('graph', 'imap'):
        return api_v1_error('invalid_method', 'method 参数仅支持 graph 或 imap', 400)
    if id_mode and id_mode not in ('uid', 'sequence'):
        return api_v1_error('invalid_id_mode', 'id_mode 参数仅支持 uid 或 sequence', 400)

    account, error_response = resolve_api_v1_scoped_account(principal, email_addr)
    if error_response is not None:
        return error_response

    try:
        result = fetch_email_detail_for_account(
            account,
            message_id,
            method,
            folder,
            id_mode,
            prefer_local=False,
        )
    except Exception as exc:
        app.logger.warning('API v1 获取邮件详情失败: %s', exc)
        result = {'success': False, 'error': '获取邮件详情失败'}

    if not result.get('success'):
        return api_v1_error('mail_fetch_failed', '获取邮件详情失败，请稍后重试', 502)

    detail = result.get('email') or {}
    return api_v1_success({
        'email': account.get('email', ''),
        'message_id': str(message_id),
        'subject': str(detail.get('subject') or '无主题'),
        'from': str(detail.get('from') or '未知'),
        'to': str(detail.get('to') or ''),
        'date': str(detail.get('date') or ''),
        'body': str(detail.get('body') or ''),
        'body_type': str(detail.get('body_type') or 'text'),
        'has_attachments': bool(detail.get('has_attachments', False)),
    })
