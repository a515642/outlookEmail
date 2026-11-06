# -*- coding: utf-8 -*-
"""对外 API Key 管理（多 Key 权限模型）。

密钥只保存 SHA-256 哈希，明文仅在创建/轮换时返回一次。
本段新增的 /api/v1/* 认证与旧版 /api/external/* 的全局 Key 完全隔离。
"""

import ipaddress
import math
from collections import deque

API_KEY_SECRET_PREFIX = 'okv1_'
API_KEY_LOOKUP_PREFIX_LENGTH = 12
API_KEY_SCOPE_MAIL_READ = 'mail:read'
API_KEY_SCOPE_ACCOUNT_CLAIM = 'account:claim'
API_KEY_SCOPES = (API_KEY_SCOPE_MAIL_READ, API_KEY_SCOPE_ACCOUNT_CLAIM)
API_KEY_CLAIM_COOLDOWN_SECONDS = 600
API_KEY_RATE_WINDOW_SECONDS = 60
API_KEY_AUDIT_MAX_ROWS_PER_KEY = 2000
API_KEY_AUDIT_PRUNE_INTERVAL = 100
API_KEY_NAME_MAX_LENGTH = 100
API_KEY_RATE_LIMIT_MAX = 100000

# 进程内限速：单 worker 部署下有效；多 worker 会按进程各自计数。
_api_key_rate_buckets = {}
_api_key_rate_lock = threading.Lock()
_api_key_audit_insert_counters = {}
_api_key_audit_lock = threading.Lock()


def api_key_utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def generate_api_key_secret() -> str:
    return f"{API_KEY_SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key_secret(secret: str) -> str:
    return hashlib.sha256(str(secret or '').encode('utf-8')).hexdigest()


def split_api_key_secret(secret: str):
    value = str(secret or '')
    prefix = value[:API_KEY_LOOKUP_PREFIX_LENGTH]
    return prefix, value[-4:]


def normalize_api_key_scopes(raw: Any) -> List[str]:
    """把输入规范化为合法 scope 列表；不合法时抛 ValueError。"""
    if raw in (None, ''):
        raise ValueError('至少需要选择一个权限范围')
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        text = str(raw).strip()
        if text.startswith('['):
            try:
                values = json.loads(text)
            except (TypeError, ValueError):
                raise ValueError('权限范围格式无效')
        else:
            values = text.split(',')
    normalized = []
    for value in values:
        scope = str(value or '').strip()
        if not scope:
            continue
        if scope not in API_KEY_SCOPES:
            raise ValueError(f'不支持的权限范围: {scope}')
        if scope not in normalized:
            normalized.append(scope)
    if not normalized:
        raise ValueError('至少需要选择一个权限范围')
    return sorted(normalized)


def normalize_api_key_group_ids(raw: Any, db=None) -> List[int]:
    """校验分组 ID 列表非空且都存在。"""
    if raw in (None, ''):
        raise ValueError('至少需要选择一个作用分组')
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = str(raw).split(',')
    group_ids = []
    for value in values:
        try:
            group_id = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError('分组 ID 无效')
        if group_id <= 0:
            raise ValueError('分组 ID 无效')
        if group_id not in group_ids:
            group_ids.append(group_id)
    if not group_ids:
        raise ValueError('至少需要选择一个作用分组')

    database = db or get_db()
    placeholders = ','.join('?' * len(group_ids))
    rows = database.execute(
        f'SELECT id FROM groups WHERE id IN ({placeholders})',
        group_ids,
    ).fetchall()
    existing = {int(row['id']) for row in rows}
    missing = [group_id for group_id in group_ids if group_id not in existing]
    if missing:
        raise ValueError(f'分组不存在: {", ".join(str(item) for item in missing)}')
    return sorted(group_ids)


def normalize_api_key_ip_allowlist(raw: Any) -> str:
    """规范化 IP/CIDR 白名单；空值表示不启用。"""
    if raw in (None, ''):
        return ''
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = re.split(r'[\s,]+', str(raw).strip())
    normalized = []
    for value in values:
        entry = str(value or '').strip()
        if not entry:
            continue
        try:
            canonical = str(ipaddress.ip_network(entry, strict=False)) if '/' in entry \
                else str(ipaddress.ip_address(entry))
        except ValueError:
            raise ValueError(f'IP 或 CIDR 无效: {entry}')
        if canonical not in normalized:
            normalized.append(canonical)
    return ','.join(normalized)


def normalize_api_key_expiry(raw: Any) -> Optional[str]:
    """解析过期时间为 UTC 文本；空值返回 None。"""
    if raw in (None, ''):
        return None
    if isinstance(raw, datetime):
        value = raw
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith('Z') or text.endswith('z'):
        text = text[:-1] + '+00:00'
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('过期时间格式无效，请使用 ISO 8601 时间')
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def normalize_api_key_rate_limit(raw: Any) -> int:
    if raw in (None, ''):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError('每分钟限速必须是整数')
    if value < 0 or value > API_KEY_RATE_LIMIT_MAX:
        raise ValueError(f'每分钟限速必须在 0-{API_KEY_RATE_LIMIT_MAX} 之间')
    return value


def api_key_ip_allowed(ip_allowlist: str, client_ip: str) -> bool:
    """白名单为空时视为不启用；解析失败一律拒绝。"""
    entries = [item for item in re.split(r'[\s,]+', str(ip_allowlist or '').strip()) if item]
    if not entries:
        return True
    raw_client_ip = str(client_ip or '').strip()
    if raw_client_ip.lower().startswith('::ffff:'):
        raw_client_ip = raw_client_ip[7:]
    try:
        client_address = ipaddress.ip_address(raw_client_ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if '/' in entry:
                if client_address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif client_address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def check_api_key_rate_limit(key_id: int, limit_per_minute: int) -> tuple:
    """返回 (allowed, retry_after_seconds)。limit<=0 表示不限速。"""
    if limit_per_minute <= 0:
        with _api_key_rate_lock:
            _api_key_rate_buckets.pop(key_id, None)
        return True, 0

    now = time.monotonic()
    cutoff = now - API_KEY_RATE_WINDOW_SECONDS
    with _api_key_rate_lock:
        bucket = _api_key_rate_buckets.get(key_id)
        if bucket is None:
            bucket = deque()
            _api_key_rate_buckets[key_id] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit_per_minute:
            retry_after = max(1, int(math.ceil(API_KEY_RATE_WINDOW_SECONDS - (now - bucket[0]))))
            return False, retry_after
        bucket.append(now)
        return True, 0


def clear_api_key_rate_bucket(key_id: int) -> None:
    with _api_key_rate_lock:
        _api_key_rate_buckets.pop(key_id, None)


def api_v1_error(error_code: str, message: str, status: int = 400,
                 retry_after: Optional[int] = None):
    g.api_v1_error_code = error_code
    payload = {'success': False, 'error_code': error_code, 'error': message}
    response = jsonify(payload)
    response.status_code = status
    if retry_after is not None:
        response.headers['Retry-After'] = str(max(1, int(retry_after)))
    return response


def get_api_v1_request_key() -> str:
    header_key = request.headers.get('X-API-Key')
    if header_key:
        return str(header_key).strip()
    authorization = request.headers.get('Authorization', '')
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip()
    return ''


def resolve_api_v1_principal():
    """解析 v1 API Key；失败时返回 (None, error_response)。"""
    raw_key = get_api_v1_request_key()
    if not raw_key:
        return None, api_v1_error(
            'missing_api_key',
            '缺少 API Key，请通过 Header X-API-Key 或 Authorization: Bearer 提供',
            401,
        )
    if len(raw_key) > 256 or re.search(r'\s', raw_key):
        return None, api_v1_error('invalid_api_key', 'API Key 格式无效', 401)
    if not raw_key.startswith(API_KEY_SECRET_PREFIX) or len(raw_key) < 24:
        return None, api_v1_error('invalid_api_key', 'API Key 无效', 401)

    db = get_db()
    prefix = raw_key[:API_KEY_LOOKUP_PREFIX_LENGTH]
    row = db.execute('SELECT * FROM api_keys WHERE key_prefix = ? LIMIT 1', (prefix,)).fetchone()
    if not row:
        return None, api_v1_error('invalid_api_key', 'API Key 无效', 401)

    # 记录已知前缀，便于对失败请求做审计。
    g.api_v1_key_id = int(row['id'])
    g.api_v1_key_prefix = str(row['key_prefix'] or '')

    if not secrets.compare_digest(str(row['key_hash'] or ''), hash_api_key_secret(raw_key)):
        return None, api_v1_error('invalid_api_key', 'API Key 无效', 401)

    if str(row['status'] or '') != 'active' or row['revoked_at']:
        return None, api_v1_error('key_revoked', 'API Key 已被撤销', 401)

    expires_at = str(row['expires_at'] or '').strip()
    if expires_at and expires_at <= api_key_utc_now_text():
        return None, api_v1_error('key_expired', 'API Key 已过期', 401)

    client_ip = str(request.remote_addr or '')
    if not api_key_ip_allowed(str(row['ip_allowlist'] or ''), client_ip):
        return None, api_v1_error('ip_not_allowed', '当前 IP 不在该 API Key 的白名单内', 403)

    allowed, retry_after = check_api_key_rate_limit(
        int(row['id']),
        int(row['rate_limit_per_minute'] or 0),
    )
    if not allowed:
        return None, api_v1_error(
            'rate_limited',
            '请求过于频繁，请稍后重试',
            429,
            retry_after=retry_after,
        )

    try:
        scopes = json.loads(row['scopes'] or '[]')
    except (TypeError, ValueError):
        scopes = []
    principal = {
        'id': int(row['id']),
        'name': str(row['name'] or ''),
        'key_prefix': str(row['key_prefix'] or ''),
        'scopes': set(scopes if isinstance(scopes, list) else []),
        'rate_limit_per_minute': int(row['rate_limit_per_minute'] or 0),
    }
    g.api_v1_authenticated = True
    return principal, None


def api_v1_key_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        principal, error_response = resolve_api_v1_principal()
        if error_response is not None:
            return error_response
        g.api_v1_principal = principal
        return f(*args, **kwargs)

    decorated_function._requires_api_key_v1 = True
    return decorated_function


def api_v1_scope_required(scope: str):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            principal, error_response = resolve_api_v1_principal()
            if error_response is not None:
                return error_response
            if scope not in principal['scopes']:
                return api_v1_error(
                    'scope_denied',
                    f'当前 API Key 缺少权限: {scope}',
                    403,
                )
            g.api_v1_principal = principal
            return f(*args, **kwargs)

        decorated_function._requires_api_key_v1 = True
        decorated_function._requires_api_key_v1_scope = scope
        return decorated_function

    return decorator


def get_api_key_configured_group_ids(key_id: int, db=None) -> List[int]:
    database = db or get_db()
    rows = database.execute(
        'SELECT group_id FROM api_key_groups WHERE api_key_id = ?',
        (key_id,),
    ).fetchall()
    return sorted({int(row['group_id']) for row in rows})


def get_api_key_effective_group_ids(key_id: int, db=None) -> List[int]:
    """作用分组自身 + 所有子分组；实时计算，分组调整后立即生效。"""
    database = db or get_db()
    effective = set()
    for group_id in get_api_key_configured_group_ids(key_id, db=database):
        effective.update(get_descendant_group_ids(group_id, db=database))
    return sorted(effective)


def api_key_account_allowed(principal, account, db=None) -> bool:
    if not principal or not account:
        return False
    effective = set(get_api_key_effective_group_ids(principal['id'], db=db))
    if not effective:
        return False
    try:
        group_id = int(account.get('group_id'))
    except (TypeError, ValueError):
        return False
    return group_id in effective


@app.before_request
def api_v1_start_request_timer():
    if request.path.startswith('/api/v1/'):
        g.api_v1_started_at = time.monotonic()


def prune_api_key_audit_logs(key_id: int, db) -> None:
    with _api_key_audit_lock:
        count = int(_api_key_audit_insert_counters.get(key_id, 0)) + 1
        _api_key_audit_insert_counters[key_id] = count
        should_prune = count % API_KEY_AUDIT_PRUNE_INTERVAL == 0
    if not should_prune:
        return
    db.execute(
        '''
        DELETE FROM api_key_audit_logs
        WHERE api_key_id = ?
          AND id NOT IN (
              SELECT id FROM api_key_audit_logs
              WHERE api_key_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        ''',
        (key_id, key_id, API_KEY_AUDIT_MAX_ROWS_PER_KEY),
    )


@app.after_request
def api_v1_record_request_audit(response):
    if not request.path.startswith('/api/v1/') or request.method == 'OPTIONS':
        return response
    key_id = getattr(g, 'api_v1_key_id', None)
    key_prefix = str(getattr(g, 'api_v1_key_prefix', '') or '')
    if not key_id and not key_prefix:
        return response
    try:
        started_at = getattr(g, 'api_v1_started_at', None)
        duration_ms = int((time.monotonic() - started_at) * 1000) if started_at else 0
        db = get_db()
        db.execute(
            '''
            INSERT INTO api_key_audit_logs
                (api_key_id, key_prefix, method, path, status_code, error_code,
                 ip, user_agent, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                key_id,
                key_prefix,
                request.method,
                request.path[:500],
                int(response.status_code or 0),
                str(getattr(g, 'api_v1_error_code', '') or '')[:100],
                str(request.remote_addr or '')[:100],
                str(request.headers.get('User-Agent') or '')[:500],
                duration_ms,
            ),
        )
        if key_id and getattr(g, 'api_v1_authenticated', False):
            db.execute(
                '''
                UPDATE api_keys
                SET last_used_at = CURRENT_TIMESTAMP,
                    last_used_ip = ?,
                    request_count = request_count + 1
                WHERE id = ?
                ''',
                (str(request.remote_addr or '')[:100], key_id),
            )
        if key_id:
            prune_api_key_audit_logs(int(key_id), db)
        db.commit()
    except Exception as exc:
        try:
            app.logger.warning('记录 API Key 审计失败: %s', exc)
        except Exception:
            pass
    return response


# ==================== API Key 管理 ====================

def get_api_key_row(key_id: int):
    return get_db().execute('SELECT * FROM api_keys WHERE id = ?', (key_id,)).fetchone()


def api_key_computed_status(row) -> str:
    if str(row['status'] or '') != 'active' or row['revoked_at']:
        return 'revoked'
    expires_at = str(row['expires_at'] or '').strip()
    if expires_at and expires_at <= api_key_utc_now_text():
        return 'expired'
    return 'active'


def serialize_api_key_row(row, db=None) -> Dict[str, Any]:
    database = db or get_db()
    group_ids = get_api_key_configured_group_ids(int(row['id']), db=database)
    groups = []
    if group_ids:
        placeholders = ','.join('?' * len(group_ids))
        group_rows = database.execute(
            f'SELECT id, name FROM groups WHERE id IN ({placeholders}) ORDER BY id',
            group_ids,
        ).fetchall()
        groups = [{'id': int(item['id']), 'name': item['name']} for item in group_rows]
    try:
        scopes = json.loads(row['scopes'] or '[]')
    except (TypeError, ValueError):
        scopes = []
    return {
        'id': int(row['id']),
        'name': row['name'],
        'key_prefix': row['key_prefix'],
        'key_last4': row['key_last4'],
        'scopes': scopes if isinstance(scopes, list) else [],
        'group_ids': group_ids,
        'groups': groups,
        'status': api_key_computed_status(row),
        'expires_at': row['expires_at'],
        'rate_limit_per_minute': int(row['rate_limit_per_minute'] or 0),
        'ip_allowlist': row['ip_allowlist'] or '',
        'last_used_at': row['last_used_at'],
        'last_used_ip': row['last_used_ip'] or '',
        'request_count': int(row['request_count'] or 0),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'revoked_at': row['revoked_at'],
        'rotated_at': row['rotated_at'],
    }


def parse_api_key_payload(data: Dict[str, Any], is_create: bool = False) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象')
    name = str(data.get('name') or '').strip()
    if not name or len(name) > API_KEY_NAME_MAX_LENGTH:
        raise ValueError(f'名称长度必须在 1-{API_KEY_NAME_MAX_LENGTH} 个字符之间')

    result = {
        'name': name,
        'scopes': normalize_api_key_scopes(data.get('scopes')),
        'group_ids': normalize_api_key_group_ids(data.get('group_ids')),
        'expires_at': normalize_api_key_expiry(data.get('expires_at')),
        'rate_limit_per_minute': normalize_api_key_rate_limit(data.get('rate_limit_per_minute')),
        'ip_allowlist': normalize_api_key_ip_allowlist(data.get('ip_allowlist')),
    }
    if is_create and result['expires_at'] and result['expires_at'] <= api_key_utc_now_text():
        raise ValueError('过期时间必须晚于当前时间')
    return result


def replace_api_key_groups(key_id: int, group_ids: List[int], db) -> None:
    db.execute('DELETE FROM api_key_groups WHERE api_key_id = ?', (key_id,))
    db.executemany(
        'INSERT INTO api_key_groups (api_key_id, group_id) VALUES (?, ?)',
        [(key_id, group_id) for group_id in group_ids],
    )


@app.route('/api/settings/api-keys', methods=['GET'])
@login_required
def api_list_api_keys():
    db = get_db()
    rows = db.execute('SELECT * FROM api_keys ORDER BY id DESC').fetchall()
    return jsonify({
        'success': True,
        'keys': [serialize_api_key_row(row, db=db) for row in rows],
    })


@app.route('/api/settings/api-keys', methods=['POST'])
@login_required
def api_create_api_key():
    data = request.get_json(silent=True) or {}
    try:
        payload = parse_api_key_payload(data, is_create=True)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    db = get_db()
    created_row = None
    secret = ''
    for _attempt in range(5):
        secret = generate_api_key_secret()
        prefix, last4 = split_api_key_secret(secret)
        try:
            cursor = db.execute(
                '''
                INSERT INTO api_keys
                    (name, key_prefix, key_last4, key_hash, scopes,
                     rate_limit_per_minute, ip_allowlist, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    payload['name'],
                    prefix,
                    last4,
                    hash_api_key_secret(secret),
                    json.dumps(payload['scopes'], ensure_ascii=False),
                    payload['rate_limit_per_minute'],
                    payload['ip_allowlist'],
                    payload['expires_at'],
                ),
            )
            key_id = int(cursor.lastrowid)
            replace_api_key_groups(key_id, payload['group_ids'], db)
            db.commit()
            created_row = get_api_key_row(key_id)
            break
        except sqlite3.IntegrityError:
            db.rollback()
            created_row = None
            continue
    if created_row is None:
        return jsonify({'success': False, 'error': '生成 API Key 失败，请重试'}), 500

    log_audit('create', 'api_key', str(created_row['id']), f'创建 API Key：{payload["name"]}')
    response = jsonify({
        'success': True,
        'key': serialize_api_key_row(created_row),
        'secret': secret,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/settings/api-keys/<int:key_id>', methods=['PUT'])
@login_required
def api_update_api_key(key_id):
    db = get_db()
    row = get_api_key_row(key_id)
    if not row:
        return jsonify({'success': False, 'error': 'API Key 不存在'}), 404
    data = request.get_json(silent=True) or {}
    try:
        payload = parse_api_key_payload(data)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    db.execute(
        '''
        UPDATE api_keys
        SET name = ?, scopes = ?, rate_limit_per_minute = ?, ip_allowlist = ?,
            expires_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (
            payload['name'],
            json.dumps(payload['scopes'], ensure_ascii=False),
            payload['rate_limit_per_minute'],
            payload['ip_allowlist'],
            payload['expires_at'],
            key_id,
        ),
    )
    replace_api_key_groups(key_id, payload['group_ids'], db)
    db.commit()
    clear_api_key_rate_bucket(key_id)
    log_audit('update', 'api_key', str(key_id), f'更新 API Key：{payload["name"]}')
    return jsonify({'success': True, 'key': serialize_api_key_row(get_api_key_row(key_id))})


@app.route('/api/settings/api-keys/<int:key_id>/rotate', methods=['POST'])
@login_required
def api_rotate_api_key(key_id):
    db = get_db()
    row = get_api_key_row(key_id)
    if not row:
        return jsonify({'success': False, 'error': 'API Key 不存在'}), 404
    if str(row['status'] or '') != 'active' or row['revoked_at']:
        return jsonify({'success': False, 'error': '已撤销的 API Key 不能轮换'}), 400

    secret = generate_api_key_secret()
    prefix, last4 = split_api_key_secret(secret)
    db.execute(
        '''
        UPDATE api_keys
        SET key_prefix = ?, key_last4 = ?, key_hash = ?,
            rotated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (prefix, last4, hash_api_key_secret(secret), key_id),
    )
    db.commit()
    clear_api_key_rate_bucket(key_id)
    log_audit('rotate', 'api_key', str(key_id), f'轮换 API Key：{row["name"]}')
    response = jsonify({
        'success': True,
        'key': serialize_api_key_row(get_api_key_row(key_id)),
        'secret': secret,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/settings/api-keys/<int:key_id>/revoke', methods=['POST'])
@login_required
def api_revoke_api_key(key_id):
    db = get_db()
    row = get_api_key_row(key_id)
    if not row:
        return jsonify({'success': False, 'error': 'API Key 不存在'}), 404
    if str(row['status'] or '') == 'revoked' or row['revoked_at']:
        return jsonify({
            'success': True,
            'key': serialize_api_key_row(row),
            'message': 'API Key 已处于撤销状态',
        })
    db.execute(
        '''
        UPDATE api_keys
        SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (key_id,),
    )
    db.commit()
    clear_api_key_rate_bucket(key_id)
    log_audit('revoke', 'api_key', str(key_id), f'撤销 API Key：{row["name"]}')
    return jsonify({
        'success': True,
        'key': serialize_api_key_row(get_api_key_row(key_id)),
        'message': 'API Key 已撤销',
    })


@app.route('/api/settings/api-keys/<int:key_id>/audit', methods=['GET'])
@login_required
def api_get_api_key_audit(key_id):
    if not get_api_key_row(key_id):
        return jsonify({'success': False, 'error': 'API Key 不存在'}), 404
    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    rows = get_db().execute(
        '''
        SELECT id, method, path, status_code, error_code, ip, user_agent,
               duration_ms, created_at
        FROM api_key_audit_logs
        WHERE api_key_id = ?
        ORDER BY id DESC
        LIMIT ?
        ''',
        (key_id, limit),
    ).fetchall()
    return jsonify({
        'success': True,
        'logs': [dict(row) for row in rows],
        'limit': limit,
    })
