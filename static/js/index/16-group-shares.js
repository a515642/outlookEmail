        /* global copyTextToClipboard, escapeHtml, formatAbsoluteDateTime, formatEmailShareExpiry, formatEmailShareStatus, formatEmailShareVisibility, groups, handleApiError, hideModal, showConfirmModal, showModal, showToast */

        // ==================== 分组分享 ====================

        function hideCreateGroupShareModal() {
            hideModal('createGroupShareModal');
        }

        function getGroupShareGroupOptions(selectedGroupId) {
            const selectableGroups = (Array.isArray(groups) ? groups : [])
                .filter(group => group && group.name !== '临时邮箱' && Number(group.is_system) !== 1);
            if (!selectableGroups.length) {
                return '<option value="">暂无可分享的分组</option>';
            }
            return selectableGroups.map(group => {
                const selected = Number(group.id) === Number(selectedGroupId) ? ' selected' : '';
                return `<option value="${Number(group.id)}"${selected}>${escapeHtml(group.name || '')}</option>`;
            }).join('');
        }

        function syncGroupShareDurationPreset() {
            const preset = document.getElementById('groupShareDurationPreset');
            const customGroup = document.getElementById('groupShareCustomDurationGroup');
            if (!preset || !customGroup) return;
            customGroup.style.display = preset.value === 'custom' ? 'block' : 'none';
        }

        function toggleGroupShareNeverExpires() {
            const never = document.getElementById('groupShareNeverExpires')?.checked;
            const preset = document.getElementById('groupShareDurationPreset');
            const custom = document.getElementById('groupShareCustomDuration');
            if (preset) preset.disabled = !!never;
            if (custom) custom.disabled = !!never;
            syncGroupShareDurationPreset();
            updateGroupShareRollingWarning();
        }

        function isGroupShareVisibilityLimited() {
            return document.getElementById('groupShareVisibilityMode')?.value === 'latest';
        }

        function toggleGroupShareVisibilityMode() {
            const maxCountGroup = document.getElementById('groupShareMaxEmailCountGroup');
            if (maxCountGroup) {
                maxCountGroup.style.display = isGroupShareVisibilityLimited() ? 'block' : 'none';
            }
            updateGroupShareRollingWarning();
        }

        function getGroupShareMaxEmailCount() {
            if (!isGroupShareVisibilityLimited()) return null;
            const rawValue = document.getElementById('groupShareMaxEmailCount')?.value;
            const value = Number(rawValue);
            return Number.isInteger(value) ? value : 0;
        }

        function getGroupShareDurationMinutes() {
            const preset = document.getElementById('groupShareDurationPreset')?.value || '1440';
            const rawValue = preset === 'custom'
                ? document.getElementById('groupShareCustomDuration')?.value
                : preset;
            const value = parseInt(rawValue || '0', 10);
            return Number.isFinite(value) ? value : 0;
        }

        function updateGroupShareRollingWarning() {
            const warning = document.getElementById('groupShareRollingWarning');
            if (!warning) return;
            const neverExpires = !!document.getElementById('groupShareNeverExpires')?.checked;
            const isLimited = isGroupShareVisibilityLimited();
            warning.hidden = !(neverExpires && isLimited);
            if (neverExpires && isLimited) {
                const maxEmailCount = getGroupShareMaxEmailCount();
                warning.textContent = `永不过期的链接会持续显示每个邮箱未来最新的 ${maxEmailCount || 'N'} 封邮件。`;
            } else {
                warning.textContent = '';
            }
        }

        function resetGroupShareCreateForm() {
            const result = document.getElementById('groupShareCreateResult');
            if (result) {
                result.hidden = true;
                result.replaceChildren();
            }
            const accessCode = document.getElementById('groupShareAccessCode');
            if (accessCode) accessCode.value = '';
            const preset = document.getElementById('groupShareDurationPreset');
            if (preset) preset.value = '1440';
            const custom = document.getElementById('groupShareCustomDuration');
            if (custom) custom.value = '1440';
            const visibilityMode = document.getElementById('groupShareVisibilityMode');
            if (visibilityMode) visibilityMode.value = 'unlimited';
            const maxEmailCount = document.getElementById('groupShareMaxEmailCount');
            if (maxEmailCount) maxEmailCount.value = '1';
            const never = document.getElementById('groupShareNeverExpires');
            if (never) never.checked = false;
            syncGroupShareDurationPreset();
            toggleGroupShareNeverExpires();
            toggleGroupShareVisibilityMode();
        }

        function showCreateGroupShareModal(groupId) {
            resetGroupShareCreateForm();
            const select = document.getElementById('groupShareGroupId');
            if (select) {
                select.innerHTML = getGroupShareGroupOptions(groupId);
                if (groupId) select.value = String(groupId);
            }
            showModal('createGroupShareModal');
        }

        function renderGroupShareCreateResult(share, accessCode = '') {
            const result = document.getElementById('groupShareCreateResult');
            if (!result) return;
            result.hidden = false;
            result.innerHTML = `
                <div class="share-result__label">分组分享链接</div>
                <div class="share-result__row">
                    <input type="text" class="form-input" readonly value="${escapeHtml(share.share_url || '')}">
                    <button class="btn btn-secondary" type="button" onclick="copyGroupShareUrl('${escapeHtml(share.share_url || '')}', '${escapeHtml(accessCode)}')">${accessCode ? '复制链接和访问码' : '复制'}</button>
                </div>
                ${accessCode ? `<div class="form-hint">访问码：${escapeHtml(accessCode)}（请保存，关闭后无法再次查看）</div>` : ''}
                <div class="form-hint">有效期：${escapeHtml(formatEmailShareExpiry(share))}</div>
                <div class="form-hint">可见范围：每个邮箱 ${escapeHtml(formatEmailShareVisibility(share))}</div>
                <div class="form-hint">访客需输入分组内邮箱的完整地址验证后才能查看。</div>
            `;
        }

        async function createGroupShare() {
            const groupId = parseInt(document.getElementById('groupShareGroupId')?.value || '0', 10);
            const neverExpires = !!document.getElementById('groupShareNeverExpires')?.checked;
            const durationMinutes = getGroupShareDurationMinutes();
            const maxEmailCount = getGroupShareMaxEmailCount();
            const accessCode = document.getElementById('groupShareAccessCode')?.value || '';
            if (!groupId) {
                showToast('请选择分组', 'error');
                return;
            }
            if (!neverExpires && durationMinutes <= 0) {
                showToast('分享时长无效', 'error');
                return;
            }
            if (isGroupShareVisibilityLimited() && (maxEmailCount < 1 || maxEmailCount > 50)) {
                showToast('邮件数量限制应为 1 到 50 的整数', 'error');
                return;
            }
            if (accessCode && !/^[A-Za-z0-9]{4,32}$/.test(accessCode)) {
                showToast('访问码须为 4–32 位英文字母或数字', 'error');
                return;
            }

            const button = document.getElementById('createGroupShareBtn');
            if (button) button.disabled = true;
            try {
                const response = await fetch('/api/group-shares', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        group_id: groupId,
                        duration_minutes: durationMinutes,
                        never_expires: neverExpires,
                        max_email_count: maxEmailCount,
                        access_code: accessCode
                    })
                });
                const data = await response.json();
                if (data.success) {
                    renderGroupShareCreateResult(data.share, accessCode);
                    showToast('分组分享链接已创建', 'success');
                    if (typeof loadGroupShares === 'function') loadGroupShares();
                } else {
                    handleApiError(data, '创建分组分享失败');
                }
            } catch (error) {
                showToast('创建分组分享失败: ' + error.message, 'error');
            } finally {
                if (button) button.disabled = false;
            }
        }

        function copyGroupShareUrl(url, accessCode = '') {
            if (!url) {
                showToast('分享链接不可用', 'error');
                return;
            }
            const text = accessCode ? `分享链接：${url}\n访问码：${accessCode}` : url;
            copyTextToClipboard(text, accessCode ? '分享链接和访问码已复制' : '分享链接已复制');
        }

        let allGroupShares = [];
        let groupShareSortCol = 'created_at';
        let groupShareSortDir = 'desc';

        function switchShareManagementTab(tab) {
            const showGroup = tab === 'group';
            const emailPanel = document.getElementById('emailShareManagementPanel');
            const groupPanel = document.getElementById('groupShareManagementPanel');
            const emailTab = document.getElementById('shareTabEmailBtn');
            const groupTab = document.getElementById('shareTabGroupBtn');
            if (emailPanel) emailPanel.hidden = showGroup;
            if (groupPanel) groupPanel.hidden = !showGroup;
            if (emailTab) emailTab.classList.toggle('active', !showGroup);
            if (groupTab) groupTab.classList.toggle('active', showGroup);
            if (showGroup) {
                loadGroupShares();
            } else {
                loadEmailShares();
            }
        }

        function updateGroupShareSortHeadersUI() {
            document.querySelectorAll('#groupShareManagementPanel .sortable-header').forEach(th => {
                const col = th.getAttribute('data-sort-col');
                const indicator = th.querySelector('.sort-indicator');
                if (!indicator) return;
                if (col === groupShareSortCol) {
                    indicator.textContent = groupShareSortDir === 'asc' ? ' ▲' : ' ▼';
                    indicator.style.color = 'var(--text-primary, #111827)';
                } else {
                    indicator.textContent = ' ↕';
                    indicator.style.color = 'var(--text-muted, #9ca3af)';
                }
            });
        }

        function handleGroupShareHeaderSort(colName) {
            if (groupShareSortCol === colName) {
                groupShareSortDir = groupShareSortDir === 'asc' ? 'desc' : 'asc';
            } else {
                groupShareSortCol = colName;
                groupShareSortDir = colName === 'group_name' ? 'asc' : 'desc';
            }
            applyGroupShareFilters();
        }

        function applyGroupShareFilters() {
            const searchName = document.getElementById('groupShareFilterName')?.value.toLowerCase().trim() || '';
            const filterStatus = document.getElementById('groupShareFilterStatus')?.value || 'all';

            let filtered = [...allGroupShares];
            if (searchName) {
                filtered = filtered.filter(share => (share.group_name || '').toLowerCase().includes(searchName));
            }
            if (filterStatus !== 'all') {
                filtered = filtered.filter(share => share.status === filterStatus);
            }

            filtered.sort((a, b) => {
                let valA;
                let valB;
                if (groupShareSortCol === 'group_name') {
                    valA = (a.group_name || '').toLowerCase();
                    valB = (b.group_name || '').toLowerCase();
                } else if (groupShareSortCol === 'status') {
                    valA = a.status || '';
                    valB = b.status || '';
                } else if (groupShareSortCol === 'expires_at') {
                    const getExpiryTime = (share) => {
                        if (share.never_expires) {
                            return groupShareSortDir === 'asc' ? Infinity : -Infinity;
                        }
                        return share.expires_at ? new Date(share.expires_at).getTime() : 0;
                    };
                    valA = getExpiryTime(a);
                    valB = getExpiryTime(b);
                    if (valA === valB) return 0;
                    if (valA === Infinity || valB === -Infinity) return groupShareSortDir === 'asc' ? 1 : -1;
                    if (valB === Infinity || valA === -Infinity) return groupShareSortDir === 'asc' ? -1 : 1;
                    return groupShareSortDir === 'asc' ? valA - valB : valB - valA;
                } else {
                    valA = new Date(a.created_at || 0).getTime();
                    valB = new Date(b.created_at || 0).getTime();
                }

                if (valA < valB) return groupShareSortDir === 'asc' ? -1 : 1;
                if (valA > valB) return groupShareSortDir === 'asc' ? 1 : -1;
                return 0;
            });

            updateGroupShareSortHeadersUI();
            renderGroupShareTable(filtered);
        }

        function renderGroupShareTable(shares) {
            const tbody = document.getElementById('groupShareTableBody');
            if (!tbody) return;

            const allCheckbox = document.getElementById('groupShareSelectAllCheckbox');
            if (allCheckbox) allCheckbox.checked = false;

            if (!Array.isArray(shares) || shares.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="8" class="share-empty">暂无分组分享记录</td>
                    </tr>
                `;
                updateGroupShareBatchButtons();
                return;
            }

            tbody.innerHTML = shares.map(share => {
                const statusStr = formatEmailShareStatus(share.status);
                let badgeClass = 'share-badge--invalid';
                if (share.status === 'active') badgeClass = 'share-badge--active';
                else if (share.status === 'expired') badgeClass = 'share-badge--expired';
                else if (share.status === 'revoked') badgeClass = 'share-badge--revoked';

                return `
                    <tr data-group-share-id="${share.id}">
                        <td>
                            <div class="share-select-all">
                                <input type="checkbox" class="share-checkbox group-share-item-checkbox" data-share-id="${share.id}" onchange="updateGroupShareBatchButtons()">
                            </div>
                        </td>
                        <td style="font-weight: 500; color: var(--text-primary, #111827);">${escapeHtml(share.group_name || '')}</td>
                        <td>
                            <span class="share-badge ${badgeClass}">${escapeHtml(statusStr)}</span>
                        </td>
                        <td class="share-cell-mono">${escapeHtml(formatEmailShareExpiry(share))}</td>
                        <td>${escapeHtml(formatEmailShareVisibility(share))}</td>
                        <td class="share-cell-mono" title="${escapeHtml(share.created_at || '')}">${escapeHtml(formatAbsoluteDateTime(share.created_at))}</td>
                        <td>
                            <div class="share-url-container">
                                <input type="text" class="form-input share-url-input" readonly value="${escapeHtml(share.share_url || '')}">
                                <button class="btn btn-secondary btn-sm-action" type="button" onclick="copyGroupShareUrl('${escapeHtml(share.share_url || '')}')">复制</button>
                            </div>
                            <div class="form-hint">${share.requires_access_code ? '需访问码' : '无需访问码'}</div>
                        </td>
                        <td style="text-align: right;">
                            <div style="display: flex; gap: 6px; justify-content: flex-end;">
                                <button class="btn btn-secondary btn-sm-action" type="button" onclick="cancelGroupShare(${Number(share.id)})" ${share.status === 'revoked' ? 'disabled' : ''}>取消</button>
                                <button class="btn btn-danger btn-sm-action" type="button" onclick="deleteGroupShare(${Number(share.id)})">删除</button>
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');

            updateGroupShareBatchButtons();
        }

        async function loadGroupShares() {
            const tbody = document.getElementById('groupShareTableBody');
            if (tbody) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="8" class="share-empty">正在加载分组分享记录...</td>
                    </tr>
                `;
            }
            const allCheckbox = document.getElementById('groupShareSelectAllCheckbox');
            if (allCheckbox) allCheckbox.checked = false;
            updateGroupShareBatchButtons();

            try {
                const response = await fetch('/api/group-shares');
                const data = await response.json();
                if (data.success) {
                    allGroupShares = data.shares || [];
                    applyGroupShareFilters();
                } else {
                    handleApiError(data, '加载分组分享记录失败');
                }
            } catch (error) {
                showToast('加载分组分享记录失败: ' + error.message, 'error');
            }
        }

        function toggleSelectAllGroupShares(allCheckbox) {
            document.querySelectorAll('.group-share-item-checkbox').forEach(cb => {
                cb.checked = allCheckbox.checked;
            });
            updateGroupShareBatchButtons();
        }

        function updateGroupShareBatchButtons() {
            const checkboxes = document.querySelectorAll('.group-share-item-checkbox');
            const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
            const cancelBtn = document.getElementById('batchCancelGroupShareBtn');
            const deleteBtn = document.getElementById('batchDeleteGroupShareBtn');
            const allCheckbox = document.getElementById('groupShareSelectAllCheckbox');
            if (cancelBtn) cancelBtn.disabled = checkedCount === 0;
            if (deleteBtn) deleteBtn.disabled = checkedCount === 0;
            if (allCheckbox) {
                allCheckbox.checked = checkboxes.length > 0 && checkedCount === checkboxes.length;
            }
        }

        async function cancelGroupShare(shareId) {
            if (!(await showConfirmModal('确定要取消这个分组分享链接吗？', { title: '取消分组分享', confirmText: '确认取消' }))) {
                return;
            }
            try {
                const response = await fetch(`/api/group-shares/${encodeURIComponent(shareId)}/cancel`, { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    showToast('分组分享已取消', 'success');
                    loadGroupShares();
                } else {
                    handleApiError(data, '取消分组分享失败');
                }
            } catch (error) {
                showToast('取消分组分享失败: ' + error.message, 'error');
            }
        }

        async function deleteGroupShare(shareId) {
            if (!(await showConfirmModal('确定要删除这个分组分享链接吗？删除后链接将永久失效且不可恢复。', { title: '删除分组分享', confirmText: '确认删除', danger: true }))) {
                return;
            }
            try {
                const response = await fetch(`/api/group-shares/${encodeURIComponent(shareId)}`, { method: 'DELETE' });
                const data = await response.json();
                if (data.success) {
                    showToast('分组分享已删除', 'success');
                    loadGroupShares();
                } else {
                    handleApiError(data, '删除分组分享失败');
                }
            } catch (error) {
                showToast('删除分组分享失败: ' + error.message, 'error');
            }
        }

        async function batchCancelGroupShares() {
            const checkboxes = document.querySelectorAll('.group-share-item-checkbox:checked');
            const ids = Array.from(checkboxes).map(cb => parseInt(cb.getAttribute('data-share-id'), 10));
            if (ids.length === 0) return;
            if (!(await showConfirmModal(`确定要取消选中的 ${ids.length} 个分组分享链接吗？`, { title: '批量取消分组分享', confirmText: '确认取消' }))) {
                return;
            }
            try {
                const response = await fetch('/api/group-shares/batch-cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ share_ids: ids })
                });
                const data = await response.json();
                if (data.success) {
                    showToast('所选分组分享已取消', 'success');
                    loadGroupShares();
                } else {
                    handleApiError(data, '批量取消分组分享失败');
                }
            } catch (error) {
                showToast('批量取消分组分享失败: ' + error.message, 'error');
            }
        }

        async function batchDeleteGroupShares() {
            const checkboxes = document.querySelectorAll('.group-share-item-checkbox:checked');
            const ids = Array.from(checkboxes).map(cb => parseInt(cb.getAttribute('data-share-id'), 10));
            if (ids.length === 0) return;
            if (!(await showConfirmModal(`确定要删除选中的 ${ids.length} 个分组分享链接吗？删除后链接将永久失效且不可恢复。`, { title: '批量删除分组分享', confirmText: '确认删除', danger: true }))) {
                return;
            }
            try {
                const response = await fetch('/api/group-shares/batch-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ share_ids: ids })
                });
                const data = await response.json();
                if (data.success) {
                    showToast('所选分组分享已删除', 'success');
                    loadGroupShares();
                } else {
                    handleApiError(data, '批量删除分组分享失败');
                }
            } catch (error) {
                showToast('批量删除分组分享失败: ' + error.message, 'error');
            }
        }
