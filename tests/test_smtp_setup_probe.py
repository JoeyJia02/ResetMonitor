"""Offline checks of the manual setup boundary; no real credentials or SMTP."""

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import getpass
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid
import warnings

from scripts import verify_smtp as probe
from tests.test_smtp_transport import FakeFactory, FakeSession


class SMTPSetupProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("REAL_NETWORK_FORBIDDEN")))
        temporary_root = Path(__file__).resolve().parents[1] / "runtime" / "smtp-probe-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        # Python 3.14's Windows mkdtemp(mode=0700) excludes this sandbox token.
        # A normal inherited-ACL directory keeps test IO within the workspace.
        temporary_case = (temporary_root / ("case-" + uuid.uuid4().hex)).resolve()
        temporary_case.relative_to(temporary_root.resolve())
        temporary_case.mkdir()
        self.stack.callback(shutil.rmtree, temporary_case)
        self.directory = temporary_case / "probe"
        self.output = io.StringIO()
        self.factory = FakeFactory()
        self.now = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
        self.secret = "abcdefghijklmnop"  # Synthetic sentinel, never a real code.

    def run_probe(self, args=None, **overrides):
        options = dict(output=self.output, confirm=lambda prompt: probe.CONFIRMATION,
                       hidden_input=lambda prompt: self.secret, clock=lambda: self.now,
                       directory=self.directory, smtp_factory=self.factory)
        options.update(overrides)
        return probe.main(["--send-one-test"] if args is None else args, **options)

    def test_preview_reads_no_credentials_or_state_and_never_connects(self):
        with patch.object(probe, "_default_directory", side_effect=AssertionError("NO_ENV")), \
             patch.object(probe, "_consumed", side_effect=AssertionError("NO_STATE_READ")):
            self.assertEqual(self.run_probe([], hidden_input=lambda _: self.fail("NO_SECRET")), 0)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.factory.calls, [])
        self.assertIn("PREVIEW_ONLY", self.output.getvalue())

    def test_unknown_argument_is_not_echoed(self):
        self.assertEqual(self.run_probe([self.secret]), 2)
        self.assertNotIn(self.secret, self.output.getvalue())
        self.assertEqual(self.factory.calls, [])

    def test_real_cli_noninteractive_refuses_before_secret_or_network(self):
        with patch.object(probe.sys.stdin, "isatty", return_value=False), \
             patch.object(probe.getpass, "getpass", side_effect=AssertionError("NO_SECRET")):
            self.assertEqual(self.run_probe(confirm=None, hidden_input=None), 2)
        self.assertIn("SMTP_TEST_INTERACTIVE_TERMINAL_REQUIRED", self.output.getvalue())
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.factory.calls, [])

    def test_confirmation_rejection_and_expiry_read_no_secret(self):
        self.assertEqual(self.run_probe(confirm=lambda _: "NO", hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.run_probe(clock=lambda: probe.EXPIRES_AT, hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.factory.calls, [])

    def test_getpass_echo_fallback_refuses_before_any_attempt(self):
        def fallback(_):
            warnings.warn("fake secret must not be echoed", getpass.GetPassWarning)
            self.fail("MUST_NOT_CONTINUE")
        self.assertEqual(self.run_probe(hidden_input=fallback), 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.factory.calls, [])

    def test_reservation_exists_before_wire_fixed_message_and_repeat_refusal(self):
        original = self.factory
        def factory(*args, **kwargs):
            record = json.loads((self.directory / (probe.PLAN_ID + ".attempt.json")).read_text())
            self.assertEqual(record["reserved_attempts"], 1)
            return original(*args, **kwargs)
        self.assertEqual(self.run_probe(smtp_factory=factory), 0)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(original.calls[0][:2], ("smtp.qq.com", 465))
        receipt = json.loads((self.directory / (probe.PLAN_ID + ".receipt.json")).read_text())
        self.assertEqual(receipt["status"], "SENT")
        self.assertFalse(receipt["inbox_delivery_confirmed"])
        self.assertEqual(original.session.auth_responses, [probe.SENDER, self.secret])
        self.assertIn(("To: " + LOCAL_RECIPIENT).encode("ascii"), original.session.payloads[0])
        self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(len(original.calls), 1)
        for path in self.directory.iterdir():
            self.assertNotIn(self.secret.encode(), path.read_bytes())
        self.assertNotIn(self.secret, self.output.getvalue())

    def test_reservation_failure_never_opens_smtp(self):
        with patch.object(probe, "_reserve", side_effect=OSError(self.secret)):
            self.assertEqual(self.run_probe(), 2)
        self.assertEqual(self.factory.calls, [])
        self.assertNotIn(self.secret, self.output.getvalue())

    def test_data_uncertainty_consumes_attempt_without_retry(self):
        self.factory = FakeFactory(FakeSession(send=OSError(self.secret)))
        self.assertEqual(self.run_probe(), 1)
        self.assertIn("UNKNOWN SMTP_DATA_UNCERTAIN", self.output.getvalue())
        self.assertEqual(self.run_probe(), 2)
        self.assertEqual(len(self.factory.calls), 1)
        self.assertNotIn(self.secret, self.output.getvalue())

    def test_auth_failure_is_known_failed_and_does_not_retry(self):
        self.factory = FakeFactory(FakeSession(auth=(535, self.secret.encode())))
        self.assertEqual(self.run_probe(), 1)
        self.assertIn("FAILED SMTP_AUTH_REJECTED", self.output.getvalue())
        self.assertEqual(self.run_probe(), 2)
        self.assertEqual(len(self.factory.calls), 1)
        self.assertNotIn(self.secret, self.output.getvalue())

    def test_receipt_failure_keeps_reservation_and_blocks_repeat(self):
        with patch.object(probe, "_write_receipt", side_effect=OSError(self.secret)):
            self.assertEqual(self.run_probe(), 1)
        self.assertIn("SMTP_TEST_RECEIPT_WRITE_FAILED", self.output.getvalue())
        self.assertEqual(self.run_probe(), 2)
        self.assertEqual(len(self.factory.calls), 1)

    def test_post_reservation_expiry_keeps_hold_and_opens_no_connection(self):
        ticks = iter([self.now, self.now, probe.EXPIRES_AT + timedelta(seconds=1)])
        self.assertEqual(self.run_probe(clock=lambda: next(ticks)), 2)
        self.assertEqual(self.factory.calls, [])
        self.assertTrue((self.directory / (probe.PLAN_ID + ".attempt.json")).exists())
        self.assertEqual(self.run_probe(), 2)


if __name__ == "__main__":
    unittest.main()
