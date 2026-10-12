"""Privacy boundaries and local configuration migration use temporary files."""
import json
from pathlib import Path
from contextlib import ExitStack
import unittest
from tests.temp_support import temporary_directory

from monitor.local_settings import DEFAULTS, LocalSettingsError, mask_address, read_settings, save_routing


class LocalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.path = Path(self.stack.enter_context(temporary_directory())) / "config.local.json"

    def test_missing_config_uses_only_non_deliverable_examples(self):
        self.assertEqual(read_settings(self.path), DEFAULTS)
        self.assertFalse(self.path.exists())

    def test_mail_change_preserves_existing_vault_namespace(self):
        value = dict(DEFAULTS, project_binding="legacy-synthetic-binding")
        self.path.write_text(json.dumps(value), encoding="utf-8")
        save_routing("sender@example.com", "recipient@example.org", path=self.path)
        self.assertEqual(read_settings(self.path)["project_binding"], "legacy-synthetic-binding")
        self.assertEqual(set(read_settings(self.path)), set(DEFAULTS))

    def test_rejects_header_injection_without_replacing_config(self):
        save_routing("sender@example.com", "recipient@example.org", path=self.path)
        original = self.path.read_bytes()
        for address in ("target@example.com\nBcc: other@example.com", "a@example.com,b@example.com", ""):
            with self.subTest(address=address), self.assertRaises(LocalSettingsError):
                save_routing("sender@example.com", address, path=self.path)
            self.assertEqual(self.path.read_bytes(), original)

    def test_config_rejects_secret_fields_and_corrupt_data(self):
        for document in (dict(DEFAULTS, apify_token="SYNTHETIC_NOT_ALLOWED"), {"sender": "x@example.com"}):
            self.path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(LocalSettingsError):
                read_settings(self.path)

    def test_addresses_are_masked_by_default(self):
        self.assertEqual(mask_address("private@example.com"), "pr••••@example.com")
        self.assertEqual(mask_address(DEFAULTS["recipient"]), "未配置")
        self.assertNotIn("private", mask_address("private@example.com"))


if __name__ == "__main__":
    unittest.main()
