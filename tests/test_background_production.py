"""Regression checks against the actual scheduled entry point, with no live IO."""
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import shutil
import ssl
import unittest
from unittest.mock import patch
from urllib.error import URLError
from uuid import uuid4


from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.credentials import ProjectCredentials
from scripts import background_monitor as monitor
from scripts.one_shot_source import OneShotSourceError
from tests.test_background_monitor import model_response
from tests.test_one_shot_source import Response, Transport, official_embed
from tests.test_smtp_transport import FakeFactory, FakeSession

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
        self.source_calls = 0
        self.tweets = [{"id": str(2109152716954222850 + index),
            "created_at": "2026-10-12T00:00:00Z", "text": "合成推文" + str(index),
            "url": "https://x.com/thsottiaux/status/" + str(2109152716954222850 + index)}
            for index in range(3)]

    def cleanup(self):
        target = self.root.resolve()
        if target.parent != ROOT or target == ROOT:
            raise AssertionError("UNSAFE_TEST_CLEANUP")
        shutil.rmtree(target)

    def fetch(self, *_):
        self.source_calls += 1
        return self.tweets, {"source_posts": 1}

    def message(self, index=-1):
        return BytesParser(policy=policy.default).parsebytes(self.factory.session.payloads[index][:-3])

    def run_at(self, transport, *, now=NOW, **changes):
        args = dict(root=self.root, credential_loader=lambda: self.credentials,
                    transport=transport, smtp_factory=self.factory,
                    chain_lock=self.root / "chain.lock", manual_results_root=self.root / "manual")
        args.update(changes)
        return monitor.run_slot(now, **args)

    def failing_context_transport(self, *, all_failed=True):
        rows = json.loads((PROJECT / 'tests/fixtures/source-items.json').read_text('utf-8'))
        curated = json.loads((PROJECT / 'tests/fixtures/source-context.json').read_text('utf-8'))
        contexts = [Response(official_embed(item)) for item in curated['items']]
        for index in range(3 if all_failed else 1):
            contexts[index] = URLError(TimeoutError(self.credentials.apify_token))
        return rows, Transport([Response(rows, status=201)] + contexts + [model_response(('IRRELEVANT',) * 3)])

    def test_all_context_failures_use_actual_scheduled_entry_one_model_and_one_mail(self):
        rows, transport = self.failing_context_transport()
        result = self.run_at(transport)
        self.assertEqual((result['status'], result['captured_items'], result['analyzed_items'],
                          result['context_fallback_items']), ('SENT', 3, 3, 3))
        self.assertEqual([call[0].method for call in transport.calls], ['POST', 'GET', 'GET', 'GET', 'POST'])
        model_input = json.loads(json.loads(transport.calls[-1][0].data)['messages'][1]['content'])['tweets']
        for original, packed in zip(reversed(rows), model_input):
            self.assertEqual(set(packed), {'id', 'text', 'created_at', 'context_unavailable'})
            self.assertEqual(packed['text'], original['text'])
            self.assertTrue(packed['context_unavailable'])
        saved = json.loads((self.root / (monitor.due_slot(NOW) + '.source.json')).read_text('utf-8'))
        self.assertEqual(len(saved['tweets']), 3)
        self.assertTrue(all('context' not in tweet for tweet in saved['tweets']))
        body = self.message().get_content()
        self.assertEqual(body.count('分析依据：仅依据主文分析'), 3)
        self.assertEqual(len(self.factory.calls), 1)
        self.assertEqual(self.run_at(transport), result)
        self.assertEqual((len(transport.calls), len(self.factory.calls)), (5, 1))
        for path in self.root.glob('*.json'):
            self.assertNotIn(self.credentials.apify_token, path.read_text('utf-8'))

    def test_mixed_context_results_keep_verified_context_and_only_label_affected_item(self):
        _, transport = self.failing_context_transport(all_failed=False)
        result = self.run_at(transport)
        self.assertEqual((result['status'], result['context_fallback_items']), ('SENT', 1))
        packed = json.loads(json.loads(transport.calls[-1][0].data)['messages'][1]['content'])['tweets']
        self.assertTrue(packed[-1]['context_unavailable'])
        self.assertIn('quoted_tweet', packed[1])
        self.assertNotIn('context_unavailable', packed[1])
        self.assertEqual(self.message().get_content().count('分析依据：仅依据主文分析'), 1)

    def test_primary_only_smtp_uncertainty_preserves_results_without_resending(self):
        _, transport = self.failing_context_transport()
        self.factory = FakeFactory(FakeSession(getreply=TimeoutError('synthetic timeout')))
        result = self.run_at(transport)
        self.assertEqual((result['status'], result['smtp_status']), ('UNKNOWN', 'UNKNOWN'))
        self.assertTrue((self.root / (monitor.due_slot(NOW) + '.result.json')).exists())
        self.assertEqual(self.run_at(transport), result)
        self.assertEqual((len(transport.calls), len(self.factory.calls)), (5, 1))

    def test_primary_checkpoint_write_failure_stops_before_context_model_and_mail(self):
        _, transport = self.failing_context_transport()
        save = monitor._save
        def fail_source(path, value):
            if path.name.endswith('.source.json'):
                raise OSError('synthetic disk failure')
            save(path, value)
        with patch.object(monitor, '_save', side_effect=fail_source):
            result = self.run_at(transport)
        self.assertEqual((result['status'], result['stage']), ('UNKNOWN', 'SOURCE_SAVE'))
        self.assertEqual((len(transport.calls), len(self.factory.calls)), (1, 0))
        self.assertFalse((self.root / 'seen-v1.json').exists())

    def test_conflicting_context_is_not_converted_into_primary_only_success(self):
        _, transport = self.failing_context_transport()
        transport.responses[1] = Response({'id_str': '123'})
        result = self.run_at(transport)
        self.assertEqual((result['status'], result['stage'], result['error_code']),
                         ('FAILED', 'SOURCE_CONTEXT', 'ONE_SHOT_EMBED_ID_MISMATCH'))
        self.assertEqual((len(transport.calls), len(self.factory.calls)), (2, 0))
        self.assertTrue((self.root / (monitor.due_slot(NOW) + '.source.json')).exists())
        self.assertFalse((self.root / 'seen-v1.json').exists())

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
        message = self.message()
        for row in rows:
            self.assertIn(row["text"], message.get_content())
        self.assertIn("IRRELEVANT", message.get_content())
        self.assertIn("PRE_RESET", message.get_content())
        self.assertIn("POST_RESET", message.get_content())
        self.assertIn("涉及额外重置条数：2", message.get_content())
        self.assertIn("额外重置通告", str(message["Subject"]))
        self.assertEqual(self.run_at(transport), result)
        self.assertEqual((len(transport.calls), len(self.factory.calls)), (5, 1))

    def test_all_irrelevant_are_mailed_once_with_all_text_and_classifications(self):
        transport = Transport([model_response(("IRRELEVANT",) * 3)])
        result = self.run_at(transport, source_fetch=self.fetch)
        self.assertEqual((result["status"], result["captured_items"], result["analyzed_items"],
                          result["relevant_items"], result["smtp_status"], result["stage"]),
                         ("SENT", 3, 3, 0, "SENT", "COMPLETE"))
        message = self.message()
        self.assertIn("暂无相关公告", str(message["Subject"]))
        self.assertNotIn("额外重置通告", str(message["Subject"]))
        for row in self.tweets:
            self.assertIn(row["text"], message.get_content())
            self.assertIn(row["url"], message.get_content())
        self.assertEqual(message.get_content().count("（IRRELEVANT）"), 3)
        self.assertIn("合成样本；时间不明。", message.get_content())
        receipt = (self.root / (monitor.due_slot(NOW) + ".receipt.json")).read_bytes()
        self.assertEqual(self.run_at(transport, source_fetch=self.fetch), result)
        self.assertEqual((self.root / (monitor.due_slot(NOW) + ".receipt.json")).read_bytes(), receipt)
        self.assertEqual((self.source_calls, len(transport.calls), len(self.factory.calls)), (1, 1, 1))

    def test_tls_failure_receipt_keeps_stage_and_reason_without_retrying_slot(self):
        error = ssl.SSLError("secret provider diagnostic")
        error.reason = "UNEXPECTED_EOF_WHILE_READING"
        self.factory = FakeFactory(error=error)
        transport = Transport([model_response(("IRRELEVANT",) * 3)])
        result = self.run_at(transport, source_fetch=self.fetch)
        self.assertEqual((result["status"], result["stage"], result["error_code"]),
                         ("FAILED", "SMTP_SEND", "SMTP_CONNECT_TLS_PEER_CLOSED"))
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.run_at(transport, source_fetch=self.fetch), result)
        self.assertEqual((self.source_calls, len(transport.calls), len(self.factory.calls)), (1, 1, 1))

    def test_next_slot_without_new_tweets_mails_status_without_model_cost(self):
        transport = Transport([model_response(("IRRELEVANT",) * 3)])
        self.run_at(transport, source_fetch=self.fetch)
        first_id = str(self.message()["Message-ID"])
        now = NOW.replace(hour=5)
        result = self.run_at(transport, now=now, source_fetch=self.fetch)
        self.assertEqual((result["status"], result["captured_items"], result["new_items"],
                          result["analyzed_items"], result["relevant_items"]), ("SENT", 3, 0, 0, 0))
        message = self.message()
        self.assertIn("暂无新推文", str(message["Subject"]))
        self.assertIn("本轮采集条数：3", message.get_content())
        self.assertIn("没有新推文，未重复调用模型", message.get_content())
        self.assertNotEqual(first_id, str(message["Message-ID"]))
        self.assertEqual(self.run_at(transport, now=now, source_fetch=self.fetch), result)
        self.assertEqual((self.source_calls, len(transport.calls), len(self.factory.calls)), (2, 1, 2))

    def test_empty_collection_sends_status_without_model_request(self):
        self.tweets = []
        transport = Transport([])
        result = self.run_at(transport, source_fetch=self.fetch)
        self.assertEqual((result["status"], result["captured_items"], result["analyzed_items"]),
                         ("SENT", 0, 0))
        message = self.message()
        self.assertIn("未采集到推文", str(message["Subject"]))
        self.assertIn("模型步骤未执行", message.get_content())
        self.assertEqual(self.run_at(transport, source_fetch=self.fetch), result)
        self.assertEqual((self.source_calls, len(transport.calls), len(self.factory.calls)), (1, 0, 1))

    def test_only_unseen_tweets_are_analyzed_and_included_in_summary(self):
        (self.root / "seen-v1.json").write_text(json.dumps({"schema_version": 1,
            "seen_ids": [self.tweets[0]["id"]]}), encoding="utf-8")
        transport = Transport([model_response(("IRRELEVANT", "POSSIBLE_RESET"))])
        result = self.run_at(transport, source_fetch=self.fetch)
        self.assertEqual((result["status"], result["captured_items"], result["new_items"],
                          result["analyzed_items"], result["relevant_items"]), ("SENT", 3, 2, 2, 1))
        request = json.loads(transport.calls[0][0].data)
        inputs = json.loads(request["messages"][1]["content"])["tweets"]
        self.assertEqual([row["text"] for row in inputs], [row["text"] for row in self.tweets[1:]])
        body = self.message().get_content()
        self.assertNotIn(self.tweets[0]["text"], body)
        for row in self.tweets[1:]:
            self.assertIn(row["text"], body)
        self.assertEqual((self.source_calls, len(transport.calls), len(self.factory.calls)), (1, 1, 1))

    def test_status_mail_smtp_failure_or_uncertainty_is_not_retried(self):
        self.tweets = []
        transport = Transport([])
        for hour, session, expected in (
                (0, FakeSession(auth=(535, b"synthetic rejected")), "FAILED"),
                (5, FakeSession(getreply=TimeoutError("synthetic timeout")), "UNKNOWN")):
            with self.subTest(outcome=expected):
                factory = FakeFactory(session)
                now = NOW.replace(hour=hour)
                result = self.run_at(transport, now=now, source_fetch=self.fetch, smtp_factory=factory)
                self.assertEqual((result["status"], result["smtp_status"], result["stage"]),
                                 (expected, expected, "SMTP_SEND"))
                self.assertEqual(self.run_at(transport, now=now, source_fetch=self.fetch,
                                             smtp_factory=factory), result)
                self.assertEqual(len(factory.calls), 1)
        self.assertEqual((self.source_calls, len(transport.calls)), (2, 0))

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
