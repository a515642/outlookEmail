import pathlib
import re
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT_DIR / 'templates'
STATIC_DIR = ROOT_DIR / 'static'


class ApiKeysFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_template = (TEMPLATES_DIR / 'partials' / 'index' / 'dialogs-management.html').read_text(encoding='utf-8')
        cls.index_template = (TEMPLATES_DIR / 'index.html').read_text(encoding='utf-8')
        cls.js = (STATIC_DIR / 'js' / 'index' / '17-api-keys.js').read_text(encoding='utf-8')
        cls.css = (STATIC_DIR / 'css' / 'index' / '11-api-keys.css').read_text(encoding='utf-8')
        cls.settings_js = (STATIC_DIR / 'js' / 'index' / '07-settings.js').read_text(encoding='utf-8')
        cls.bootstrap = (ROOT_DIR / 'outlook_web' / 'segments' / '01_bootstrap.py').read_text(encoding='utf-8')

    def test_settings_template_has_api_key_section_and_modal_ids(self):
        for marker in [
            'data-target="settingsApiKeysSection"',
            'id="settingsApiKeysSection"',
            'id="apiKeyTableBody"',
            'id="apiKeyEditModal"',
            'id="apiKeyName"',
            'id="apiKeyScopeMailRead"',
            'id="apiKeyScopeAccountClaim"',
            'id="apiKeyGroupCheckboxList"',
            'id="apiKeyExpiresAt"',
            'id="apiKeyRateLimit"',
            'id="apiKeyIpAllowlist"',
            'id="apiKeySecretModal"',
            'id="apiKeySecretValue"',
            'id="apiKeyAuditModal"',
            'id="apiKeyAuditTableBody"',
        ]:
            self.assertIn(marker, self.settings_template)

    def test_settings_template_wires_js_handlers(self):
        for handler in [
            'loadApiKeyList()',
            'openCreateApiKeyModal()',
            'submitApiKeyForm()',
            'copyApiKeySecret()',
            'hideApiKeyEditModal()',
            'hideApiKeySecretModal()',
            'hideApiKeyAuditModal()',
        ]:
            self.assertIn(handler, self.settings_template)

    def test_asset_loading_registered(self):
        self.assertIn(
            "js/index/17-api-keys.js",
            self.index_template,
        )
        self.assertIn("'js/index/17-api-keys.js'", self.bootstrap)
        self.assertIn("'css/index/11-api-keys.css'", self.bootstrap)

    def test_group_ids_visible_in_api_key_ui(self):
        # 分组列表徽标与选择清单都需要展示分组 ID，便于与 /api/v1 返回的 group_id 对应。
        self.assertIn('class="api-key-group-id"', self.js)
        self.assertIn('（ID: ${groupId}）', self.js)

    def test_expected_functions_exist(self):
        for function_name in [
            'loadApiKeyList',
            'renderApiKeyTable',
            'openCreateApiKeyModal',
            'openEditApiKeyModal',
            'submitApiKeyForm',
            'showApiKeySecret',
            'hideApiKeySecretModal',
            'copyApiKeySecret',
            'rotateApiKey',
            'revokeApiKey',
            'openApiKeyAuditModal',
            'renderApiKeyAuditLogs',
        ]:
            self.assertRegex(
                self.js,
                rf'function\s+{function_name}\s*\(',
                f'缺少函数 {function_name}',
            )

    def test_management_endpoints_are_used(self):
        self.assertIn("fetch('/api/settings/api-keys')", self.js)
        self.assertIn('/api/settings/api-keys/${Number(apiKeyEditingId)}', self.js)
        self.assertIn('/rotate', self.js)
        self.assertIn('/revoke', self.js)
        self.assertIn('/audit?limit=100', self.js)

    def test_delete_action_is_available(self):
        self.assertIn("onclick=\"deleteApiKey(", self.js)
        self.assertRegex(
            self.js,
            r'function\s+deleteApiKey\s*\(keyId\)\s*\{',
        )
        self.assertIn("/api/settings/api-keys/${Number(keyId)}`, {method: 'DELETE'}", self.js)
        self.assertIn('永久删除', self.js)

    def test_secret_is_never_written_to_browser_storage(self):
        self.assertNotIn('localStorage', self.js)
        self.assertNotIn('sessionStorage', self.js)
        self.assertNotIn('document.cookie', self.js)

    def test_secret_modal_clears_value_on_close(self):
        self.assertRegex(
            self.js,
            r'function\s+hideApiKeySecretModal\s*\(\)\s*\{[^}]*input\.value\s*=\s*\'\'',
        )

    def test_rendered_fields_are_escaped(self):
        # 表格与审计渲染必须经过 escapeHtml，避免名称/路径注入。
        for field in ['key.name', 'key.key_prefix', 'key.key_last4', 'log.path', 'log.ip']:
            self.assertIn(f'escapeHtml({field}', self.js)
        self.assertIn('escapeHtml(option.label)', self.js)

    def test_settings_modal_loads_and_cleans_up_api_key_ui(self):
        self.assertIn('await loadApiKeyList();', self.settings_js)
        self.assertIn('hideApiKeyEditModal();', self.settings_js)
        self.assertIn('hideApiKeySecretModal();', self.settings_js)
        self.assertIn('hideApiKeyAuditModal();', self.settings_js)

    def test_css_defines_layout(self):
        for selector in ['.api-keys-table', '.api-key-modal-content', '.api-key-status', '.api-key-secret-value']:
            self.assertIn(selector, self.css)


if __name__ == '__main__':
    unittest.main()
