"""Credential-management entrypoints use synthetic, injected storage only."""

from contextlib import ExitStack
import getpass
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from monitor.credentials import CredentialInputError, ProjectCredentials
from monitor.credential_store import CredentialStoreError
from scripts import manage_credentials as cli


class ManageCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for stream in ("stdin", "stderr"):
            self.stack.enter_context(patch.object(cli.sys, stream, SimpleNamespace(isatty=lambda: True)))
        for name in ("load_credentials", "save_credentials", "has_credentials", "delete_credentials"):
            self.stack.enter_context(patch.object(cli, name, side_effect=AssertionError("REAL_VAULT_FORBIDDEN")))
        self.output = io.StringIO()
        self.secrets = ("SYNTHETIC_APIFY_KEY", "SYNTHETIC_DEEPSEEK_KEY", "abcdefghijklmnop")

    def hidden(self):
        values = iter(self.secrets)
        return lambda _: next(values)

    def credentials(self):
        return ProjectCredentials(apify_token=self.secrets[0], deepseek_api_key=self.secrets[1],
                                  smtp_user=cli.SENDER, smtp_password=self.secrets[2])

    def test_help_and_invalid_args_never_touch_vault_or_input(self):
        for argv in ([], ["--help"]):
            self.assertEqual(cli.main(argv, output=self.output, hidden_input=lambda _: self.fail("NO_INPUT")), 0)
        self.assertEqual(cli.main([self.secrets[0]], output=self.output), 2)
        self.assertNotIn(self.secrets[0], self.output.getvalue())

    def test_save_updates_one_record_with_all_three_and_no_output_values(self):
        saved = []
        self.assertEqual(cli.main(["--save"], output=self.output, hidden_input=self.hidden(), saver=saved.append), 0)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].smtp_user, cli.SENDER)
        self.assertEqual(saved[0].apify_token, self.secrets[0])
        self.assertEqual(saved[0].deepseek_api_key, self.secrets[1])
        self.assertEqual(saved[0].smtp_password, self.secrets[2])
        for value in self.secrets:
            self.assertNotIn(value, self.output.getvalue())

    def test_status_only_metadata_delete_only_one_record_and_no_prompt(self):
        for present in (True, False):
            self.assertEqual(cli.main(["--status"], output=self.output, exists=lambda: present), 0)
            self.assertEqual(cli.main(["--delete"], output=self.output, deleter=lambda: present), 0)

    def test_cached_noninteractive_no_prompt_or_save(self):
        credentials = self.credentials()
        with patch.object(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False)):
            result = cli.get_saved_or_prompt_credentials(loader=lambda: credentials,
                saver=lambda _: self.fail("NO_SAVE"), hidden_input=lambda _: self.fail("NO_PROMPT"), output=self.output)
        self.assertIs(result, credentials)

    def test_missing_record_prompts_three_and_saves_before_return(self):
        saved = []
        result = cli.get_saved_or_prompt_credentials(loader=lambda: None, saver=saved.append,
                                                     hidden_input=self.hidden(), output=self.output)
        self.assertEqual(saved, [result])
        self.assertIn("Windows", self.output.getvalue())

    def test_noninteractive_and_echo_fallback_never_save(self):
        saved = []
        with patch.object(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False)):
            self.assertEqual(cli.main(["--save"], output=self.output,
                                      hidden_input=lambda _: self.fail("NO_PROMPT"), saver=saved.append), 2)
        def fallback(_):
            warnings.warn(self.secrets[0], getpass.GetPassWarning)
        self.assertEqual(cli.main(["--save"], output=self.output, hidden_input=fallback, saver=saved.append), 2)
        self.assertEqual(saved, [])
        self.assertNotIn(self.secrets[0], self.output.getvalue())

    def test_invalid_input_and_store_error_have_fixed_codes(self):
        saved = []
        self.assertEqual(cli.main(["--save"], output=self.output, hidden_input=lambda _: "bad", saver=saved.append), 2)
        self.assertEqual(saved, [])
        def fail(_):
            raise CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED")
        self.assertEqual(cli.main(["--save"], output=self.output, hidden_input=self.hidden(), saver=fail), 2)
        self.assertIn("CREDENTIAL_STORE_WRITE_FAILED", self.output.getvalue())
        for value in self.secrets:
            self.assertNotIn(value, self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
