"""Credential-interface tests use synthetic mappings and hidden-input fakes."""

from dataclasses import asdict, is_dataclass
import getpass
import json
from types import ModuleType
import unittest
from unittest.mock import patch
import warnings

from monitor.credentials import CredentialInputError, ProjectCredentials, read_project_credentials


SECRET = "synthetic-project-only-secret"
VARIABLES = ("RESET_MONITOR_APIFY_TOKEN", "RESET_MONITOR_DEEPSEEK_API_KEY",
             "RESET_MONITOR_SMTP_USER", "RESET_MONITOR_SMTP_PASSWORD")


class CredentialsTests(unittest.TestCase):
    def test_stored_reads_project_store_only_without_prompt_or_environment(self):
        credentials = ProjectCredentials(apify_token=SECRET, deepseek_api_key=SECRET,
                                         smtp_user=SECRET, smtp_password=SECRET)
        module = ModuleType("monitor.credential_store")
        with patch.object(module, "load_credentials", return_value=credentials, create=True) as loader, patch.dict(
                "sys.modules", {"monitor.credential_store": module}), patch(
                "monitor.credentials.os.environ", new=object()), patch(
                "monitor.credentials.getpass.getpass", side_effect=AssertionError("No prompt allowed")) as prompt:
            result = read_project_credentials("stored", environment=object(), hidden_input=object())
        self.assertIs(result, credentials)
        loader.assert_called_once_with()
        prompt.assert_not_called()

    def test_stored_missing_malformed_and_errors_have_fixed_redacted_error_without_fallback(self):
        for result in (None, {"secret": SECRET}, SECRET):
            module = ModuleType("monitor.credential_store")
            with self.subTest(result_type=type(result).__name__), patch.object(
                    module, "load_credentials", return_value=result, create=True) as loader, patch.dict(
                    "sys.modules", {"monitor.credential_store": module}), self.assertRaises(CredentialInputError) as caught:
                read_project_credentials("stored", environment={name: SECRET for name in VARIABLES})
            loader.assert_called_once_with()
            self.assertEqual(str(caught.exception), "PROJECT_CREDENTIAL_INPUT_REQUIRED")
            self.assertNotIn(SECRET, repr(caught.exception))
        for problem in (RuntimeError(SECRET), KeyboardInterrupt()):
            module = ModuleType("monitor.credential_store")
            with self.subTest(error_type=type(problem).__name__), patch.object(
                    module, "load_credentials", side_effect=problem, create=True), patch.dict(
                    "sys.modules", {"monitor.credential_store": module}), self.assertRaises(CredentialInputError) as caught:
                read_project_credentials("stored")
            self.assertEqual(str(caught.exception), "PROJECT_CREDENTIAL_INPUT_REQUIRED")
            self.assertNotIn(SECRET, repr(caught.exception))

    def test_environment_and_prompt_never_access_stored_credentials(self):
        module = ModuleType("monitor.credential_store")
        with patch.object(module, "load_credentials", side_effect=AssertionError("Store access forbidden"), create=True) as loader, patch.dict(
                "sys.modules", {"monitor.credential_store": module}):
            read_project_credentials("env", environment={name: SECRET for name in VARIABLES})
            read_project_credentials("prompt", hidden_input=lambda _label: SECRET)
        loader.assert_not_called()

    def test_environment_reads_only_four_project_variables_without_enumeration(self):
        reads = []
        class Mapping:
            def get(self, key):
                reads.append(key)
                return SECRET
            def __iter__(self):
                raise AssertionError("Credentials may not enumerate environment")
        credentials = read_project_credentials("env", environment=Mapping())
        self.assertEqual(reads, list(VARIABLES))
        self.assertEqual(credentials.apify_token, SECRET)
        self.assertEqual(credentials.deepseek_api_key, SECRET)

    def test_global_or_other_app_keys_are_never_substituted(self):
        mapping = {"APIFY_TOKEN": SECRET, "DEEPSEEK_API_KEY": SECRET,
                   "SMTP_USER": SECRET, "SMTP_PASSWORD": SECRET, "OPENAI_API_KEY": SECRET}
        with self.assertRaises(CredentialInputError) as caught:
            read_project_credentials("env", environment=mapping)
        self.assertEqual(str(caught.exception), "PROJECT_CREDENTIAL_INPUT_REQUIRED")
        self.assertNotIn(SECRET, repr(caught.exception))

    def test_hidden_input_is_explicit_and_called_once_per_project_field(self):
        labels = []
        def hidden(label):
            labels.append(label)
            return SECRET
        credentials = read_project_credentials("prompt", hidden_input=hidden)
        self.assertEqual(len(labels), 4)
        self.assertTrue(all(label.startswith("ResetMonitor ") for label in labels))
        self.assertNotIn(SECRET, repr(labels))
        self.assertEqual(credentials.smtp_password, SECRET)

    def test_echo_fallback_warning_and_noninteractive_default_refuse_input(self):
        def unsafe(_label):
            warnings.warn("Synthetic fallback", getpass.GetPassWarning)
            return SECRET
        with self.assertRaises(CredentialInputError):
            read_project_credentials("prompt", hidden_input=unsafe)
        with patch("monitor.credentials.sys.stdin.isatty", return_value=False), patch(
                "monitor.credentials.getpass.getpass", side_effect=AssertionError("No interactive read allowed")) as reader:
            with self.assertRaises(CredentialInputError):
                read_project_credentials("prompt")
        reader.assert_not_called()

    def test_input_errors_and_keyboard_interrupt_have_fixed_redacted_exception(self):
        for problem in (RuntimeError(SECRET), KeyboardInterrupt()):
            def hidden(_label, problem=problem):
                raise problem
            with self.assertRaises(CredentialInputError) as caught:
                read_project_credentials("prompt", hidden_input=hidden)
            self.assertEqual(str(caught.exception), "PROJECT_CREDENTIAL_INPUT_REQUIRED")
            self.assertNotIn(SECRET, repr(caught.exception))

    def test_value_container_cannot_accidentally_dataclass_or_json_dump_credentials(self):
        credentials = ProjectCredentials(apify_token=SECRET, deepseek_api_key=SECRET,
                                         smtp_user=SECRET, smtp_password=SECRET)
        self.assertFalse(is_dataclass(credentials))
        self.assertEqual(repr(credentials), "ProjectCredentials(<redacted>)")
        with self.assertRaises(TypeError):
            asdict(credentials)
        with self.assertRaises(TypeError):
            json.dumps(credentials)
        self.assertFalse(hasattr(credentials, "__dict__"))

    def test_invalid_empty_controls_non_ascii_and_overlong_inputs_never_echo(self):
        for supplied in (None, "", " " + SECRET, SECRET + "\n", "含中文", "x" * 4097, True):
            with self.subTest(kind=type(supplied).__name__), self.assertRaises(CredentialInputError) as caught:
                ProjectCredentials(apify_token=supplied, deepseek_api_key=SECRET,
                                   smtp_user=SECRET, smtp_password=SECRET)
            self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(CredentialInputError):
            read_project_credentials("file", environment={name: SECRET for name in VARIABLES})


if __name__ == "__main__":
    unittest.main()
