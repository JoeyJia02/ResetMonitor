"""Publication checks detect content without echoing sensitive matches."""
from contextlib import redirect_stdout
import io
from pathlib import Path
from tests.temp_support import temporary_directory
import unittest
from unittest.mock import patch

from scripts import check_publication as audit


class PublicationTests(unittest.TestCase):
    def test_private_paths_and_backups_cannot_be_published(self):
        for name in ("config.local.json", "config.local.json.tmp", "evidence/result.json", "runtime/copy.py",
                     ".env", ".env.production", ".claude/settings.local.json", "AGENTS.md", "keys.txt"):
            with self.subTest(name=name):
                self.assertEqual(audit.scan_blob(name, b"{}"), [audit.Finding(name, 0, "PRIVATE_FILE")])

    def test_templates_and_examples_are_allowed(self):
        for name in ("config.example.json", ".env.example", "monitor/settings_gui.py",
                     "monitor/AGENTS.md", "scripts/AGENTS.md"):
            self.assertFalse(audit.scan_blob(name, b"sender@example.com recipient@example.invalid"))

    def test_non_example_email_and_tokens_are_detected_without_echo(self):
        secret = "sk-" + "a" * 40
        address = "fictional" + "@" + "mail-provider.com"
        text = (secret + "\n" + address).encode()
        results = audit.scan_blob("file.py", text)
        self.assertEqual({item.rule for item in results}, {"POSSIBLE_API_SECRET", "NON_EXAMPLE_EMAIL"})
        self.assertNotIn(secret, repr(results))
        self.assertNotIn(address, repr(results))

    def test_known_local_binding_and_personal_path_detected(self):
        content = 'private-local-binding\nC:' + '/Users/Someone/Documents/report.json'
        results = audit.scan_blob("README.md", content.encode(), ("private-local-binding",))
        self.assertEqual({item.rule for item in results}, {"LOCAL_PRIVATE_VALUE", "PERSONAL_WINDOWS_PATH"})

    def test_reads_staged_object_not_redacted_working_copy(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            (root / "source.py").write_text("safe", encoding="utf-8")
            oid = "a" * 40
            entry = f"100644 {oid} 0\tsource.py\0".encode()
            with patch.object(audit, "git", side_effect=(entry, b"staged-private-data")) as mock:
                self.assertEqual(list(audit.staged_blobs(root)), [("source.py", b"staged-private-data")])
            self.assertEqual(mock.call_args.args, ("cat-file", "blob", oid))

    def test_report_contains_only_rules_and_positions(self):
        value = "sk-" + "b" * 40
        output = io.StringIO()
        with patch.object(audit, "staged_blobs", return_value=iter([("file.py", value.encode())])), \
                patch.object(audit, "local_private_values", return_value=()), redirect_stdout(output):
            self.assertEqual(audit.main(), 1)
        self.assertNotIn(value, output.getvalue())
        self.assertIn("file.py:1: POSSIBLE_API_SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
