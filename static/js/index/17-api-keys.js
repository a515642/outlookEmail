        // ==================== API Key 管理 ====================
        let apiKeyList = [];
        let apiKeyEditingId = null;
        let apiKeyAuditKeyId = null;

        function formatApiKeyScopes(scopes) {
            if (!Array.isArray(scopes) || !scopes.length) {
                return '<span class="api-key-muted">-</span>';
            }
            return scopes.map(scope => {
                const label = scope === 'mail:read'
                    ? '读取邮件'
                    : (scope === 'account:claim' ? '领取邮箱' : scope);
                return `<span class="api-key-scope-badge" title="${escapeHtml(scope)}">${escapeHtml(label)}</span>`;
            }).join(' ');
        }

        function formatApiKeyStatus(key) {
            const status = String((key && key.status) || 'active');
            const map = {
                active: { label: '启用', className: 'is-active' },
                expired: { label: '已过期', className: 'is-expired' },
                revoked: { label: '已撤销', className: 'is-revoked' },
            };
            const meta = map[status] || { label: status, className: '' };
            return `<span class="api-key-status ${meta.className}">${escapeHtml(meta.label)}</span>`;
        }

        function formatApiKeyGroups(key) {
            const groupItems = Array.isArray(key && key.groups)
                ? key.groups
                    .map(group => ({
                        id: Number(group.id),
                        name: String((group && group.name) || '').trim(),
                    }))
                    .filter(group => group.name)
                : [];
            if (!groupItems.length) {
                return '<span class="api-key-muted">未授权</span>';
            }
            return groupItems.map(group => {
                const idText = Number.isFinite(group.id)
                    ? `<span class="api-key-group-id" title="分组 ID">ID ${group.id}</span>`
                    : '';
                return `<span class="api-key-group-badge">${escapeHtml(group.name)}${idText}</span>`;
            }).join(' ');
        }

        function renderApiKeyTable() {
            const tbody = document.getElementById('apiKeyTableBody');
            if (!tbody) {
                return;
            }
            if (!apiKeyList.length) {
                tbody.innerHTML = '<tr><td colspan="9" class="api-keys-empty">还没有 API Key，点击「新建 API Key」开始。</td></tr>';
                return;
            }
            tbody.innerHTML = apiKeyList.map(key => {
                const keyId = Number(key.id);
                const isActive = key.status === 'active';
                const prefix = `${escapeHtml(key.key_prefix || '')}…${escapeHtml(key.key_last4 || '')}`;
                const rate = Number(key.rate_limit_per_minute || 0) > 0
                    ? `${Number(key.rate_limit_per_minute)} 次/分钟`
                    : '不限';
                const lastUsed = key.last_used_at
                    ? `${escapeHtml(formatAbsoluteDateTime(key.last_used_at))}<div class="api-key-muted">${escapeHtml(key.last_used_ip || '-')}</div>`
                    : '<span class="api-key-muted">从未调用</span>';
                const actions = [
                    `<button class="btn btn-secondary btn-sm" type="button" onclick="openEditApiKeyModal(${keyId})">编辑</button>`,
                    `<button class="btn btn-secondary btn-sm" type="button" onclick="openApiKeyAuditModal(${keyId})">记录</button>`,
                    isActive
                        ? `<button class="btn btn-secondary btn-sm" type="button" onclick="rotateApiKey(${keyId})">轮换</button>`
                        : '',
                    isActive
                        ? `<button class="btn btn-danger btn-sm" type="button" onclick="revokeApiKey(${keyId})">撤销</button>`
                        : '',
                    `<button class="btn btn-danger btn-sm" type="button" onclick="deleteApiKey(${keyId})">删除</button>`,
                ].filter(Boolean).join('');
                return `
                    <tr>
                        <td>
                            <strong>${escapeHtml(key.name || '')}</strong>
                            <div class="api-key-muted">创建于 ${escapeHtml(formatAbsoluteDateTime(key.created_at))}</div>
                        </td>
                        <td><code class="api-key-prefix">${prefix}</code></td>
                        <td>${formatApiKeyScopes(key.scopes)}</td>
                        <td class="api-key-group-cell">${formatApiKeyGroups(key)}</td>
                        <td>${key.expires_at ? escapeHtml(formatAbsoluteDateTime(key.expires_at)) : '<span class="api-key-muted">永不过期</span>'}</td>
                        <td>${escapeHtml(rate)}</td>
                        <td>${lastUsed}</td>
                        <td>${formatApiKeyStatus(key)}</td>
                        <td><div class="api-key-row-actions">${actions}</div></td>
                    </tr>
                `;
            }).join('');
        }

        async function loadApiKeyList() {
            const tbody = document.getElementById('apiKeyTableBody');
            if (tbody && !apiKeyList.length) {
                tbody.innerHTML = '<tr><td colspan="9" class="api-keys-empty">正在加载...</td></tr>';
            }
            try {
                const response = await fetch('/api/settings/api-keys');
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error((data && data.error) || '加载 API Key 失败');
                }
                apiKeyList = Array.isArray(data.keys) ? data.keys : [];
                renderApiKeyTable();
            } catch (error) {
                if (tbody) {
                    tbody.innerHTML = `<tr><td colspan="9" class="api-keys-empty">加载失败：${escapeHtml(error.message || '未知错误')}</td></tr>`;
                }
                showToast(error.message || '加载 API Key 失败', 'error');
            }
        }

        function renderApiKeyGroupCheckboxes(selectedIds) {
            const container = document.getElementById('apiKeyGroupCheckboxList');
            if (!container) {
                return;
            }
            const selected = new Set((selectedIds || []).map(id => Number(id)));
            let options = [];
            if (typeof flattenGroupTree === 'function' && typeof groupTree !== 'undefined' && groupTree) {
                options = flattenGroupTree(groupTree).map(group => ({
                    id: group.id,
                    label: typeof getGroupOptionLabel === 'function' ? getGroupOptionLabel(group) : group.name,
                }));
            } else if (Array.isArray(groups)) {
                options = groups.map(group => ({ id: group.id, label: group.name }));
            }
            if (!options.length) {
                container.innerHTML = '<span class="api-key-muted">分组尚未加载，请先刷新分组列表。</span>';
                return;
            }
            container.innerHTML = options.map(option => {
                const groupId = Number(option.id);
                const idText = Number.isFinite(groupId) ? `（ID: ${groupId}）` : '';
                return `
                <label class="api-key-checkbox-item">
                    <input type="checkbox" class="api-key-group-checkbox" value="${groupId}" ${selected.has(groupId) ? 'checked' : ''}>
                    <span>${escapeHtml(option.label)}${escapeHtml(idText)}</span>
                </label>
            `;
            }).join('');
        }

        async function ensureApiKeyGroupsLoaded() {
            if (Array.isArray(groups) && groups.length) {
                return;
            }
            if (typeof loadGroups === 'function') {
                try {
                    await loadGroups();
                } catch (error) {
                    // 分组加载失败时由弹窗内提示兜底。
                }
            }
        }

        function resetApiKeyForm() {
            apiKeyEditingId = null;
            const title = document.getElementById('apiKeyEditTitle');
            if (title) title.textContent = '新建 API Key';
            const nameInput = document.getElementById('apiKeyName');
            if (nameInput) nameInput.value = '';
            const mailRead = document.getElementById('apiKeyScopeMailRead');
            if (mailRead) mailRead.checked = true;
            const claim = document.getElementById('apiKeyScopeAccountClaim');
            if (claim) claim.checked = false;
            const expiresAt = document.getElementById('apiKeyExpiresAt');
            if (expiresAt) expiresAt.value = '';
            const rateLimit = document.getElementById('apiKeyRateLimit');
            if (rateLimit) rateLimit.value = '0';
            const allowlist = document.getElementById('apiKeyIpAllowlist');
            if (allowlist) allowlist.value = '';
            setApiKeyFormError('');
            renderApiKeyGroupCheckboxes([]);
        }

        function setApiKeyFormError(message) {
            const errorEl = document.getElementById('apiKeyEditError');
            if (!errorEl) {
                return;
            }
            errorEl.textContent = message || '';
            errorEl.style.display = message ? 'block' : 'none';
        }

        function toLocalDateTimeInput(utcText) {
            if (!utcText) {
                return '';
            }
            const date = typeof parseDateInput === 'function' ? parseDateInput(utcText) : new Date(utcText);
            if (!date || Number.isNaN(date.getTime())) {
                return '';
            }
            const pad = value => String(value).padStart(2, '0');
            return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
        }

        async function openCreateApiKeyModal() {
            await ensureApiKeyGroupsLoaded();
            resetApiKeyForm();
            showModal('apiKeyEditModal');
        }

        async function openEditApiKeyModal(keyId) {
            const key = apiKeyList.find(item => Number(item.id) === Number(keyId));
            if (!key) {
                showToast('API Key 不存在或已被删除', 'error');
                return;
            }
            await ensureApiKeyGroupsLoaded();
            resetApiKeyForm();
            apiKeyEditingId = Number(key.id);
            const title = document.getElementById('apiKeyEditTitle');
            if (title) title.textContent = `编辑 API Key：${key.name || ''}`;
            const nameInput = document.getElementById('apiKeyName');
            if (nameInput) nameInput.value = key.name || '';
            const mailRead = document.getElementById('apiKeyScopeMailRead');
            if (mailRead) mailRead.checked = Array.isArray(key.scopes) && key.scopes.includes('mail:read');
            const claim = document.getElementById('apiKeyScopeAccountClaim');
            if (claim) claim.checked = Array.isArray(key.scopes) && key.scopes.includes('account:claim');
            const expiresAt = document.getElementById('apiKeyExpiresAt');
            if (expiresAt) expiresAt.value = toLocalDateTimeInput(key.expires_at);
            const rateLimit = document.getElementById('apiKeyRateLimit');
            if (rateLimit) rateLimit.value = String(Number(key.rate_limit_per_minute || 0));
            const allowlist = document.getElementById('apiKeyIpAllowlist');
            if (allowlist) allowlist.value = key.ip_allowlist || '';
            renderApiKeyGroupCheckboxes(key.group_ids || []);
            showModal('apiKeyEditModal');
        }

        function hideApiKeyEditModal() {
            hideModal('apiKeyEditModal');
            setApiKeyFormError('');
        }

        function collectApiKeyFormPayload() {
            const name = String((document.getElementById('apiKeyName') || {}).value || '').trim();
            const scopes = [];
            if (document.getElementById('apiKeyScopeMailRead') && document.getElementById('apiKeyScopeMailRead').checked) {
                scopes.push('mail:read');
            }
            if (document.getElementById('apiKeyScopeAccountClaim') && document.getElementById('apiKeyScopeAccountClaim').checked) {
                scopes.push('account:claim');
            }
            const groupIds = Array.from(document.querySelectorAll('.api-key-group-checkbox'))
                .filter(checkbox => checkbox.checked)
                .map(checkbox => Number(checkbox.value));
            const expiryValue = String((document.getElementById('apiKeyExpiresAt') || {}).value || '');
            const rateRaw = String((document.getElementById('apiKeyRateLimit') || {}).value || '0').trim();
            const ipAllowlist = String((document.getElementById('apiKeyIpAllowlist') || {}).value || '').trim();

            if (!name) {
                throw new Error('请填写名称');
            }
            if (!scopes.length) {
                throw new Error('请至少选择一个权限范围');
            }
            if (!groupIds.length) {
                throw new Error('请至少选择一个作用分组');
            }
            let expiresAt = null;
            if (expiryValue) {
                const parsed = new Date(expiryValue);
                if (Number.isNaN(parsed.getTime())) {
                    throw new Error('过期时间格式无效');
                }
                expiresAt = parsed.toISOString();
            }
            return {
                name,
                scopes,
                group_ids: groupIds,
                expires_at: expiresAt,
                rate_limit_per_minute: rateRaw === '' ? 0 : Number(rateRaw),
                ip_allowlist: ipAllowlist,
            };
        }

        async function submitApiKeyForm() {
            const submitBtn = document.getElementById('apiKeyEditSubmitBtn');
            let payload;
            try {
                payload = collectApiKeyFormPayload();
            } catch (error) {
                setApiKeyFormError(error.message);
                return;
            }
            setApiKeyFormError('');
            const isEdit = apiKeyEditingId !== null;
            const url = isEdit
                ? `/api/settings/api-keys/${Number(apiKeyEditingId)}`
                : '/api/settings/api-keys';
            if (submitBtn) submitBtn.disabled = true;
            try {
                const response = await fetch(url, {
                    method: isEdit ? 'PUT' : 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error((data && data.error) || '保存 API Key 失败');
                }
                hideApiKeyEditModal();
                await loadApiKeyList();
                if (data.secret) {
                    showApiKeySecret(data.secret);
                } else {
                    showToast('API Key 已更新', 'success');
                }
            } catch (error) {
                setApiKeyFormError(error.message || '保存 API Key 失败');
            } finally {
                if (submitBtn) submitBtn.disabled = false;
            }
        }

        function showApiKeySecret(secret) {
            const input = document.getElementById('apiKeySecretValue');
            if (input) {
                input.value = secret || '';
            }
            showModal('apiKeySecretModal');
        }

        function hideApiKeySecretModal() {
            const input = document.getElementById('apiKeySecretValue');
            if (input) {
                input.value = '';
            }
            hideModal('apiKeySecretModal');
        }

        function copyApiKeySecret() {
            const input = document.getElementById('apiKeySecretValue');
            const secret = String((input && input.value) || '');
            if (!secret) {
                showToast('没有可复制的内容', 'error');
                return;
            }
            if (typeof copyTextToClipboard === 'function') {
                copyTextToClipboard(secret, 'API Key 已复制');
            } else {
                input.select();
                document.execCommand('copy');
                showToast('API Key 已复制', 'success');
            }
        }

        async function rotateApiKey(keyId) {
            const key = apiKeyList.find(item => Number(item.id) === Number(keyId));
            const name = key ? key.name : `#${keyId}`;
            const confirmed = await showConfirmModal(
                `确定要轮换 API Key「${name}」吗？\n旧密钥会立即失效，新密钥只会显示一次。`,
                { title: '轮换 API Key', confirmText: '确认轮换' }
            );
            if (!confirmed) {
                return;
            }
            try {
                const response = await fetch(`/api/settings/api-keys/${Number(keyId)}/rotate`, {method: 'POST'});
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error((data && data.error) || '轮换失败');
                }
                await loadApiKeyList();
                if (data.secret) {
                    showApiKeySecret(data.secret);
                }
            } catch (error) {
                showToast(error.message || '轮换失败', 'error');
            }
        }

        async function revokeApiKey(keyId) {
            const key = apiKeyList.find(item => Number(item.id) === Number(keyId));
            const name = key ? key.name : `#${keyId}`;
            const confirmed = await showConfirmModal(
                `确定要撤销 API Key「${name}」吗？\n撤销后不可恢复，使用该 Key 的调用会立即被拒绝。`,
                { title: '撤销 API Key', confirmText: '确认撤销' }
            );
            if (!confirmed) {
                return;
            }
            try {
                const response = await fetch(`/api/settings/api-keys/${Number(keyId)}/revoke`, {method: 'POST'});
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error((data && data.error) || '撤销失败');
                }
                await loadApiKeyList();
                showToast('API Key 已撤销', 'success');
            } catch (error) {
                showToast(error.message || '撤销失败', 'error');
            }
        }

        async function deleteApiKey(keyId) {
            const key = apiKeyList.find(item => Number(item.id) === Number(keyId));
            const name = key ? key.name : `#${keyId}`;
            const confirmed = await showConfirmModal(
                `确定要永久删除 API Key「${name}」吗？\n删除后无法恢复，分组绑定和调用记录也会一并删除。`,
                { title: '删除 API Key', confirmText: '永久删除' }
            );
            if (!confirmed) {
                return;
            }
            try {
                const response = await fetch(`/api/settings/api-keys/${Number(keyId)}`, {method: 'DELETE'});
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error((data && data.error) || '删除失败');
                }
                await loadApiKeyList();
                showToast('API Key 已删除', 'success');
            } catch (error) {
                showToast(error.message || '删除失败', 'error');
            }
        }

        function renderApiKeyAuditLogs(logs) {
            const tbody = document.getElementById('apiKeyAuditTableBody');
            if (!tbody) {
                return;
            }
            if (!logs || !logs.length) {
                tbody.innerHTML = '<tr><td colspan="7" class="api-keys-empty">暂无调用记录。</td></tr>';
                return;
            }
            tbody.innerHTML = logs.map(log => {
                const status = Number(log.status_code || 0);
                const statusClass = status >= 200 && status < 300 ? 'is-ok' : (status >= 500 ? 'is-error' : 'is-warn');
                return `
                    <tr>
                        <td>${escapeHtml(formatAbsoluteDateTime(log.created_at))}</td>
                        <td>${escapeHtml(log.method || '')}</td>
                        <td class="api-key-audit-path" title="${escapeHtml(log.path || '')}">${escapeHtml(log.path || '')}</td>
                        <td><span class="api-key-status ${statusClass}">${escapeHtml(String(status))}</span></td>
                        <td>${log.error_code ? `<code>${escapeHtml(log.error_code)}</code>` : '<span class="api-key-muted">-</span>'}</td>
                        <td>${escapeHtml(log.ip || '-')}</td>
                        <td>${Number(log.duration_ms || 0)} ms</td>
                    </tr>
                `;
            }).join('');
        }

        async function openApiKeyAuditModal(keyId) {
            const key = apiKeyList.find(item => Number(item.id) === Number(keyId));
            apiKeyAuditKeyId = Number(keyId);
            const title = document.getElementById('apiKeyAuditTitle');
            if (title) {
                title.textContent = `调用记录：${key ? key.name : `#${keyId}`}`;
            }
            const tbody = document.getElementById('apiKeyAuditTableBody');
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="7" class="api-keys-empty">正在加载...</td></tr>';
            }
            showModal('apiKeyAuditModal');
            try {
                const response = await fetch(`/api/settings/api-keys/${Number(keyId)}/audit?limit=100`);
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error((data && data.error) || '加载调用记录失败');
                }
                renderApiKeyAuditLogs(data.logs || []);
            } catch (error) {
                if (tbody) {
                    tbody.innerHTML = `<tr><td colspan="7" class="api-keys-empty">加载失败：${escapeHtml(error.message || '未知错误')}</td></tr>`;
                }
            }
        }

        function hideApiKeyAuditModal() {
            apiKeyAuditKeyId = null;
            hideModal('apiKeyAuditModal');
        }
