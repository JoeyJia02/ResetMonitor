"""Synthetic settings tests; no network or actual credential vault access."""
import importlib
import unittest
from unittest.mock import Mock, patch


from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.credentials import ProjectCredentials
from monitor.credential_store import CredentialStoreError, SMTP_USER
from monitor.settings_controller import SettingsController


APIFY = "apify-SYNTHETIC-NOT-A-REAL-TOKEN"
DEEPSEEK = "SYNTHETIC-NOT-A-REAL-KEY"
QQ = "SYNTHETIC_PASS16"


class SettingsControllerTests(unittest.TestCase):
    def controller(self, *, present=False):
        saver, exists, deleter = Mock(), Mock(return_value=present), Mock(return_value=present)
        return SettingsController(saver=saver, exists=exists, deleter=deleter), saver, exists, deleter

    def assert_safe(self, result):
        self.assertEqual(set(result), {"success", "error", "stored", "message"})
        self.assertIs(type(result["success"]), bool)
        self.assertIs(result["error"], not result["success"])
        for secret in (APIFY, DEEPSEEK, QQ, "SYNTHETIC-SECRET-DETAIL"):
            self.assertNotIn(secret, repr(result))

    def test_import_and_construction_perform_no_store_operations(self):
        with patch("monitor.credential_store.has_credentials", side_effect=AssertionError) as exists, \
                patch("monitor.credential_store.load_credentials", side_effect=AssertionError) as loader, \
                patch("monitor.credential_store.save_credentials", side_effect=AssertionError) as saver, \
                patch("monitor.credential_store.delete_credentials", side_effect=AssertionError) as deleter:
            import monitor.settings_controller as module
            importlib.reload(module)
            module.SettingsController()
            for operation in (exists, loader, saver, deleter):
                operation.assert_not_called()
        # Restore the default imports after patching without accessing the vault.
        importlib.reload(module)

    def test_missing_and_saved_status_use_presence_only(self):
        for present in (False, True):
            controller, saver, exists, deleter = self.controller(present=present)
            with patch("monitor.credential_store.load_credentials", side_effect=AssertionError) as loader:
                result = controller.status()
            self.assertIs(result["stored"], present)
            self.assertTrue(result["success"])
            exists.assert_called_once_with()
            saver.assert_not_called()
            deleter.assert_not_called()
            loader.assert_not_called()
            self.assert_safe(result)

    def test_save_fixed_sender_without_loading_existing_values(self):
        controller, saver, exists, deleter = self.controller()
        result = controller.save(APIFY, DEEPSEEK, QQ)
        saver.assert_called_once()
        saved = saver.call_args.args[0]
        self.assertIsInstance(saved, ProjectCredentials)
        self.assertEqual(saved.apify_token, APIFY)
        self.assertEqual(saved.deepseek_api_key, DEEPSEEK)
        self.assertEqual(saved.smtp_user, SMTP_USER)
        self.assertEqual(saved.smtp_password, QQ)
        self.assertTrue(result["success"])
        self.assertIs(result["stored"], True)
        exists.assert_not_called()
        deleter.assert_not_called()
        self.assert_safe(result)
        self.assertNotIn(APIFY, repr(vars(controller)))

    def test_update_replaces_all_three_credentials_once(self):
        controller, saver, exists, _ = self.controller(present=True)
        controller.status()
        result = controller.save(APIFY, DEEPSEEK, QQ)
        self.assertTrue(result["success"])
        saver.assert_called_once()
        exists.assert_called_once_with()
        self.assertEqual(saver.call_args.args[0].smtp_user, LOCAL_SENDER)

    def test_paste_edge_whitespace_is_trimmed_without_truncation(self):
        controller, saver, _, _ = self.controller()
        result = controller.save(" \t" + APIFY + "\r\n", "\n" + DEEPSEEK + " ", QQ + "\r\n")
        self.assertTrue(result["success"])
        saved = saver.call_args.args[0]
        self.assertEqual(saved.apify_token, APIFY)
        self.assertEqual(saved.deepseek_api_key, DEEPSEEK)
        self.assertEqual(saved.smtp_password, QQ)

    def test_missing_and_nonstring_values_do_not_write(self):
        for values in (("", DEEPSEEK, QQ), (APIFY, " \r\n", QQ), (APIFY, DEEPSEEK, ""),
                       (None, DEEPSEEK, QQ), (APIFY, True, QQ), (APIFY, DEEPSEEK, 123)):
            with self.subTest(types=tuple(type(value).__name__ for value in values)):
                controller, saver, exists, deleter = self.controller()
                result = controller.save(*values)
                self.assertFalse(result["success"])
                self.assertIsNone(result["stored"])
                for operation in (saver, exists, deleter):
                    operation.assert_not_called()
                self.assert_safe(result)

    def test_qq_requires_exactly_16_printable_ascii_characters(self):
        for value in ("x" * 15, "x" * 17, "中" * 16, "x" * 8 + "\t" + "x" * 7,
                      "x" * 8 + " " + "x" * 7, "x" * 8 + "\x00" + "x" * 7):
            controller, saver, _, _ = self.controller()
            result = controller.save(APIFY, DEEPSEEK, value)
            self.assertFalse(result["success"])
            saver.assert_not_called()
            self.assert_safe(result)

    def test_invalid_internal_whitespace_nonascii_and_long_fields_are_rejected(self):
        for invalid in ("abc def", "abc\ndef", "中文", "x" * 4097, "abc\x7fdef"):
            for index in (0, 1):
                controller, saver, _, _ = self.controller()
                values = [APIFY, DEEPSEEK, QQ]
                values[index] = invalid
                result = controller.save(*values)
                self.assertFalse(result["success"])
                saver.assert_not_called()
                self.assert_safe(result)

    def test_clear_existing_and_missing_are_normal(self):
        for present in (True, False):
            controller, saver, exists, deleter = self.controller(present=present)
            result = controller.clear()
            self.assertTrue(result["success"])
            self.assertIs(result["stored"], False)
            deleter.assert_called_once_with()
            saver.assert_not_called()
            exists.assert_not_called()
            self.assert_safe(result)

    def test_all_store_errors_have_fixed_safe_friendly_messages(self):
        codes = ("CREDENTIAL_STORE_UNAVAILABLE", "CREDENTIAL_STORE_READ_FAILED",
                 "CREDENTIAL_STORE_WRITE_FAILED", "CREDENTIAL_STORE_DELETE_FAILED",
                 "CREDENTIAL_STORE_INVALID", "CREDENTIAL_STORE_TOO_LARGE")
        for code in codes:
            for action in ("status", "save", "clear"):
                controller, saver, exists, deleter = self.controller()
                operation = {"status": exists, "save": saver, "clear": deleter}[action]
                operation.side_effect = CredentialStoreError(code)
                result = controller.save(APIFY, DEEPSEEK, QQ) if action == "save" else getattr(controller, action)()
                self.assertFalse(result["success"])
                self.assertIsNone(result["stored"])
                self.assertNotIn(code, result["message"])
                self.assert_safe(result)

    def test_unknown_exception_and_interrupt_never_echo_input_or_error(self):
        for error in (RuntimeError("SYNTHETIC-SECRET-DETAIL"), KeyboardInterrupt()):
            for action in ("status", "save", "clear"):
                controller, saver, exists, deleter = self.controller(present=True)
                controller.status()
                {"status": exists, "save": saver, "clear": deleter}[action].side_effect = error
                result = controller.save(APIFY, DEEPSEEK, QQ) if action == "save" else getattr(controller, action)()
                self.assertFalse(result["success"])
                self.assertIsNone(result["stored"])
                self.assert_safe(result)

    def test_nonbool_backend_presence_or_delete_results_are_errors(self):
        for value in (None, 1, "yes", {}):
            controller, _, exists, deleter = self.controller()
            exists.return_value = value
            deleter.return_value = value
            self.assertFalse(controller.status()["success"])
            self.assertFalse(controller.clear()["success"])

    def test_validation_failure_preserves_confirmed_stored_status(self):
        controller, saver, _, _ = self.controller(present=True)
        controller.status()
        result = controller.save("", DEEPSEEK, QQ)
        self.assertFalse(result["success"])
        self.assertIs(result["stored"], True)
        saver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
