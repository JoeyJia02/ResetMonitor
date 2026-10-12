"""Regression checks against the actual scheduled entry point, with no live IO."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4


from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.credentials import ProjectCredentials
from scripts import background_monitor as monitor
from scripts.one_shot_source import OneShotSourceError
from tests.test_background_monitor import model_response
from tests.test_one_shot_source import Response, Transport, official_embed
from tests.test_smtp_transport import FakeFactory

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT", str(PROJECT / "runtime" / "test-tmp"))).resolve()
NOW = datetime(2026, 10, 12, 0, 0, 7, tzinfo=timezone.utc)

class BackgroundProductionTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ("production-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL", "monitor.credential_store.load_credentials"):
            guard = patch(target, side_effect=AssertionError("LIVE_IO_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)
        self.credentials = ProjectCredentials(apify_token="SYNTHETIC_APIFY_TOKEN",
            deepseek_api_key="SYNTHETIC_MODEL_KEY", smtp_user=LOCAL_SENDER,
            smtp_password="abcdefghijklmnop")
        self.factory = FakeFactory()

    def cleanup(self):
        target = self.root.resolve()
        if target.parent != ROOT or target == ROOT:
            raise AssertionError("UNSAFE_TEST_CLEANUP")
        shutil.rmtree(target)

    def run_at(self, transport, **changes):
        args = dict(root=self.root, credential_loader=lambda: self.credentials,
                    transport=transport, smtp_factory=self.factory,
                    chain_lock=self.root / "chain.lock", manual_results_root=self.root / "manual")
        args.update(changes)
        return monitor.run_slot(NOW, **args)

    def test_long_note_reaches_model_with_complete_text_and_no_duplicate_calls(self):
        rows = json.loads((PROJECT / "tests/fixtures/source-items.json").read_text("utf-8"))
        curated = json.loads((PROJECT / "tests/fixtures/source-context.json").read_text("utf-8"))
        embeds = [official_embed(item) for item in curated["items"]]
        preview = "Codex &amp; Work: " + "x" * (280 - len("Codex &amp; Work: "))
        rows[0]["text"] = preview.replace("&amp;", "&") + " EXTRA RESET COMPLETED."
        embeds[0].update(text=preview, note_tweet={"id": "NoteTweet:synthetic"}, display_text_range=[0, 280])
        transport = Transport([Response(rows, status=201)] + [Response(item) for item in embeds] +
                              [model_response(("IRRELEVANT", "PRE_RESET", "POST_RESET"))])
        result = self.run_at(transport)
        self.assertEqual((result["status"], result["captured_items"], result["analyzed_items"]), ("SENT", 3, 3))
        model_request = json.loads(transport.calls[-1][0].data)
        self.assertIn(rows[0]["text"], json.dumps(model_request, ensure_ascii=False).replace("\\n", "\n"))
        self.assertEqual(len(self.factory.calls), 1)
        self.assertEqual(self.run_at(transport), result)
        self.assertEqual((len(transport.calls), len(self.factory.calls)), (5, 1))

    def test_source_failure_keeps_specific_code_and_stage_in_status(self):
        def fail(*args):
            raise OneShotSourceError("ONE_SHOT_EMBED_TEXT_MISMATCH")
        transport = Transport([])
        result = self.run_at(transport, source_fetch=fail)
        self.assertEqual((result["status"], result["stage"], result["error_code"]),
                         ("FAILED", "SOURCE_FETCH", "ONE_SHOT_EMBED_TEXT_MISMATCH"))
        self.assertEqual(self.run_at(transport, source_fetch=fail), result)
        self.assertEqual((transport.calls, self.factory.calls), ([], []))

    def test_arbitrary_exception_text_and_mutated_codes_never_enter_receipts(self):
        secret = self.credentials.apify_token
        def fail(*args):
            raise OneShotSourceError(secret)
        result = self.run_at(Transport([]), source_fetch=fail)
        self.assertEqual(result["error_code"], "BACKGROUND_SOURCE_FAILED")
        for path in self.root.glob("*.json"):
            self.assertNotIn(secret, path.read_text("utf-8"))

    def test_non_source_exception_records_stage_without_exception_message(self):
        def fail():
            raise OSError(self.credentials.apify_token)
        result = self.run_at(Transport([]), credential_loader=fail)
        self.assertEqual((result["status"], result["stage"], result["error_code"]),
                         ("UNKNOWN", "CREDENTIAL_LOAD", "BACKGROUND_CREDENTIAL_LOAD_FAILED"))
        self.assertNotIn(self.credentials.apify_token, json.dumps(result))

    def test_malformed_existing_receipt_is_reported_without_repeating_work(self):
        path = self.root / (monitor.due_slot(NOW) + ".receipt.json")
        path.write_text('{"status":"RUNNING"}', encoding="utf-8")
        result = self.run_at(Transport([]))
        self.assertEqual(result, {"status": "BACKGROUND_RECEIPT_UNAVAILABLE"})
        self.assertEqual(self.factory.calls, [])

if __name__ == "__main__":
    unittest.main()
