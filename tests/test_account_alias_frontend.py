import os
import pathlib
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]


class AccountAliasFrontendTests(unittest.TestCase):
    def setUp(self):
        self.settings_js = (ROOT_DIR / 'static' / 'js' / 'index' / '07-settings.js').read_text(encoding='utf-8')
        self.dialog = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')

    def test_generate_control_exists(self):
        self.assertIn('id="editAliasGenerateCount"', self.dialog)
        self.assertIn('onclick="generateAccountAliases()"', self.dialog)
        self.assertIn('生成别名', self.dialog)

    def test_generate_function_calls_api(self):
        self.assertRegex(self.settings_js, r'async function generateAccountAliases\(\)\s*\{')
        self.assertIn('/aliases/generate', self.settings_js)
        self.assertIn("method: 'POST'", self.settings_js)
        self.assertIn('count, aliases: currentAliases', self.settings_js)
        self.assertIn("textarea.value = (data.aliases || []).join('\\n')", self.settings_js)


if __name__ == '__main__':
    unittest.main()
