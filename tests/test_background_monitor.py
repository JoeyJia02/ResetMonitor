"""Offline checks for the persistent user's three daily monitoring slots."""
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4


from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.credentials import ProjectCredentials
from monitor.locking import ProcessLock
from scripts import background_monitor_recovery_candidate as monitor
from tests.test_classification_setup_probe import FakeResponse, FakeTransport
from tests.test_smtp_transport import FakeFactory, FakeSession


ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT",
    str(Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"))).resolve()
SLOTS = (datetime(2026, 10, 12, 0, 0, 11, tzinfo=timezone.utc),
         datetime(2026, 10, 12, 5, 0, 11, tzinfo=timezone.utc),
         datetime(2026, 10, 12, 12, 0, 11, tzinfo=timezone.utc))


def model_response(labels):
    results = [{"id": str(index), "label": label, "reset_time": None,
                "reason": "合成样本；时间不明。"}
               for index, label in enumerate(labels, 1)]
    return FakeResponse({"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"results": results})}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 170,
                  "total_tokens": 1370}})


class BackgroundMonitorTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ("bg-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("REAL_EXTERNAL_IO_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)
        self.credentials = ProjectCredentials(apify_token="SYNTHETIC_APIFY_TOKEN",
            deepseek_api_key="SYNTHETIC_MODEL_KEY", smtp_user=LOCAL_SENDER,
            smtp_password="abcdefghijklmnop")
        self.tweets = [{"id": str(2109152716954222850 + index),
            "created_at": "2026-10-12T00:00:00Z", "text": "合成推文" + str(index),
            "url": "https://x.com/thsottiaux/status/" + str(2109152716954222850 + index),
            "context": {"quoted_tweet": {"text": "合成引用"}}} for index in range(3)]
        self.source_calls = 0
        self.credential_reads = 0
        self.transport = FakeTransport(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        self.factory = FakeFactory()

    def cleanup(self):
        target = self.root.resolve()
        if target.parent != ROOT or target == ROOT:
            raise AssertionError("UNSAFE_TEST_CLEANUP")
        shutil.rmtree(target)

    def fetch(self, *_):
        self.source_calls += 1
        return self.tweets, {"source_posts": 1}

    def load(self):
        self.credential_reads += 1
        return self.credentials

    def record_manual_success(self, tweets):
        manual_root = self.root / "manual-results"
        manual_root.mkdir(exist_ok=True)
        run_id = "manual-test-" + uuid4().hex
        (manual_root / (run_id + ".receipt.json")).write_text(json.dumps({
            "plan_id": run_id, "status": "FULL_CHAIN_OK"}), encoding="utf-8")
        (manual_root / (run_id + ".result.json")).write_text(json.dumps({
            "mode": "MANUAL", "plan_id": run_id, "tweets": tweets}), encoding="utf-8")

    def run_at(self, now, **updates):
        arguments = dict(root=self.root, credential_loader=self.load,
            source_fetch=self.fetch, transport=self.transport, smtp_factory=self.factory,
            chain_lock=self.root / "shared-manual.lock",
            manual_results_root=self.root / "manual-results")
        arguments.update(updates)
        return monitor.run_slot(now, **arguments)

    def test_beijing_schedule_and_outside_minute_are_read_only(self):
        self.assertEqual([monitor.due_slot(value) for value in SLOTS],
            ["scheduled-20261012-0800", "scheduled-20261012-1300", "scheduled-20261012-2000"])
        self.assertEqual(self.run_at(SLOTS[0].replace(minute=1)), {"status": "NOT_DUE"})
        self.assertEqual((self.credential_reads, self.source_calls, len(self.transport.calls)), (0, 0, 0))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_new_three_are_analyzed_once_and_only_relevant_items_are_mailed(self):
        first = self.run_at(SLOTS[0])
        self.assertEqual((first["status"], first["captured_items"], first["new_items"],
                          first["analyzed_items"], first["relevant_items"], first["smtp_status"]),
                         ("SENT", 3, 3, 3, 2, "SENT"))
        first_id = monitor.due_slot(SLOTS[0])
        result = json.loads((self.root / (first_id + ".result.json")).read_text("utf-8"))
        body = result["notification"]["body"]
        self.assertIn("POST_RESET", body)
        self.assertIn("PRE_RESET", body)
        self.assertNotIn("IRRELEVANT", body)
        self.assertNotIn(self.tweets[1]["text"], body)
        self.assertNotIn("一次性测试", body)
        self.assertEqual(self.run_at(SLOTS[0]), first)
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (1, 1, 1))
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["captured_items"], second["new_items"]),
                         ("NO_NEW_ITEMS", 3, 0))
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 1))

    def test_irrelevant_items_are_seen_without_sending_mail(self):
        self.transport = FakeTransport(model_response(("IRRELEVANT",) * 3))
        first = self.run_at(SLOTS[0])
        self.assertEqual((first["status"], first["analyzed_items"], first["relevant_items"]),
                         ("NO_RELEVANT_ITEMS", 3, 0))
        self.assertEqual(len(self.factory.calls), 0)
        self.assertEqual(self.run_at(SLOTS[1])["status"], "NO_NEW_ITEMS")
        self.assertEqual(len(self.transport.calls), 1)

    def test_uncertain_smtp_keeps_result_and_no_later_duplicate_mail(self):
        factory = FakeFactory(FakeSession(send=OSError("SYNTHETIC_FAILURE")))
        first = self.run_at(SLOTS[0], smtp_factory=factory)
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertEqual(first["relevant_items"], 2)
        self.assertTrue((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).is_file())
        journal = json.loads((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).read_text("utf-8"))
        self.assertEqual(journal["status"], "SMTP_UNKNOWN")
        self.assertEqual(self.run_at(SLOTS[1], smtp_factory=factory)["status"], "NO_NEW_ITEMS")
        self.assertEqual(len(factory.calls), 1)

    def test_previous_successful_manual_run_seeds_deduplication(self):
        manual_root = self.root / "manual-results"
        manual_root.mkdir()
        run_id = "manual-test-" + "a" * 32
        (manual_root / (run_id + ".receipt.json")).write_text(json.dumps({
            "plan_id": run_id, "status": "FULL_CHAIN_OK"}), encoding="utf-8")
        (manual_root / (run_id + ".result.json")).write_text(json.dumps({
            "mode": "MANUAL", "plan_id": run_id, "tweets": self.tweets}), encoding="utf-8")
        result = self.run_at(SLOTS[0])
        self.assertEqual((result["status"], result["new_items"]), ("NO_NEW_ITEMS", 0))
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (1, 0, 0))

    def test_corrupt_seen_state_blocks_before_credentials_or_source(self):
        (self.root / "seen-v1.json").write_text('{"schema_version":true,"seen_ids":[]}', encoding="utf-8")
        result = self.run_at(SLOTS[0])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual((self.credential_reads, self.source_calls, len(self.transport.calls)), (0, 0, 0))
        self.assertEqual(self.run_at(SLOTS[0]), result)

    def test_source_failure_is_claimed_once_and_next_slot_is_independent(self):
        def fail(*_):
            self.source_calls += 1
            raise RuntimeError("SYNTHETIC_SOURCE_FAILURE")
        self.assertEqual(self.run_at(SLOTS[0], source_fetch=fail)["status"], "UNKNOWN")
        self.assertEqual(self.run_at(SLOTS[0], source_fetch=fail)["status"], "UNKNOWN")
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (1, 0, 0))
        self.assertEqual(self.run_at(SLOTS[1])["status"], "SENT")
        self.assertEqual(self.source_calls, 2)

    def test_failed_model_resumes_at_next_slot_without_a_second_source_run_in_that_slot(self):
        failed_transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.transport = failed_transport
        first = self.run_at(SLOTS[0])
        self.assertEqual(first["status"], "UNKNOWN")
        original_run = monitor.due_slot(SLOTS[0])
        source_copy = json.loads((self.root / (original_run + ".result.json")).read_text("utf-8"))
        self.assertEqual((source_copy["status"], len(source_copy["tweets"])), ("MODEL_FAILED", 3))
        self.assertEqual(self.run_at(SLOTS[0]), first)
        self.assertEqual((self.source_calls, len(failed_transport.calls), len(self.factory.calls)), (1, 1, 0))
        self.transport = FakeTransport(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["recovered_run_id"], second["analyzed_items"]),
                         ("SENT", original_run, 3))
        self.assertEqual((self.source_calls, len(failed_transport.calls),
                          len(self.transport.calls), len(self.factory.calls)), (2, 1, 1, 1))
        recovered = json.loads((self.root / (original_run + ".result.json")).read_text("utf-8"))
        self.assertEqual(recovered["status"], "SENT")
        self.assertEqual(self.run_at(SLOTS[2])["status"], "NO_NEW_ITEMS")

    def test_explicit_smtp_failure_reuses_analysis_and_only_retries_mail_next_slot(self):
        failed_factory = FakeFactory(FakeSession(ehlo=(451, b"synthetic rejection")))
        first = self.run_at(SLOTS[0], smtp_factory=failed_factory)
        original_run = monitor.due_slot(SLOTS[0])
        self.assertEqual((first["status"], first["smtp_status"]), ("FAILED", "FAILED"))
        journal = json.loads((self.root / (original_run + ".result.json")).read_text("utf-8"))
        self.assertEqual(journal["status"], "SMTP_FAILED")
        self.assertEqual(len(self.transport.calls), 1)
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["recovered_run_id"],
                          second["analyzed_items"], second["reused_analysis_items"]),
                         ("SENT", original_run, 0, 3))
        self.assertEqual((self.source_calls, len(self.transport.calls),
                          len(failed_factory.calls), len(self.factory.calls)), (2, 1, 1, 1))
        self.assertEqual(self.run_at(SLOTS[2])["status"], "NO_NEW_ITEMS")

    def test_crash_after_model_checkpoint_recovers_mail_without_reanalyzing(self):
        with patch.object(monitor, "_notification", side_effect=RuntimeError("SYNTHETIC_CRASH")):
            first = self.run_at(SLOTS[0])
        self.assertEqual(first["status"], "UNKNOWN")
        original_run = monitor.due_slot(SLOTS[0])
        journal = json.loads((self.root / (original_run + ".result.json")).read_text("utf-8"))
        self.assertEqual((journal["status"], len(journal["results"]), journal["notification"]),
                         ("ANALYZED", 3, None))
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["recovered_run_id"],
                          second["analyzed_items"], second["reused_analysis_items"]),
                         ("SENT", original_run, 0, 3))
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 1))

    def test_ambiguous_smtp_crash_is_never_automatically_resent(self):
        with patch.object(monitor.SMTPNotifier, "send", side_effect=RuntimeError("SYNTHETIC_CRASH")) as send:
            first = self.run_at(SLOTS[0])
        self.assertEqual(first["status"], "UNKNOWN")
        journal_path = self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")
        self.assertEqual(json.loads(journal_path.read_text("utf-8"))["status"], "SMTP_ATTEMPTED")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.run_at(SLOTS[1])["status"], "NO_NEW_ITEMS")
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 0))

    def test_old_pending_is_prioritized_and_new_three_wait_for_a_later_slot(self):
        self.transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.assertEqual(self.run_at(SLOTS[0])["status"], "UNKNOWN")
        old_run = monitor.due_slot(SLOTS[0])
        self.tweets = [{**tweet, "id": str(int(tweet["id"]) + 100),
                        "url": tweet["url"].rsplit("/", 1)[0] + "/" + str(int(tweet["id"]) + 100)}
                       for tweet in self.tweets]
        self.transport = FakeTransport(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["recovered_run_id"], second["new_items"]),
                         ("SENT", old_run, 3))
        new_run = monitor.due_slot(SLOTS[1])
        self.assertEqual(json.loads((self.root / (new_run + ".result.json")).read_text("utf-8"))["status"],
                         "SOURCE_READY")
        third = self.run_at(SLOTS[2])
        self.assertEqual((third["status"], third["recovered_run_id"], third["new_items"]),
                         ("SENT", new_run, 0))
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (3, 2, 2))

    def test_source_failure_does_not_block_prior_model_recovery(self):
        self.transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.assertEqual(self.run_at(SLOTS[0])["status"], "UNKNOWN")
        self.transport = FakeTransport(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        def failed_source(*_):
            self.source_calls += 1
            raise RuntimeError("SYNTHETIC_SOURCE_FAILURE")
        second = self.run_at(SLOTS[1], source_fetch=failed_source)
        self.assertEqual((second["status"], second["source_status"], second["recovered_run_id"]),
                         ("SENT", "FAILED", monitor.due_slot(SLOTS[0])))
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 1))

    def test_repeated_model_failure_stops_after_one_scheduled_recovery_attempt(self):
        self.transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.assertEqual(self.run_at(SLOTS[0])["status"], "UNKNOWN")
        self.assertEqual(self.run_at(SLOTS[1])["status"], "UNKNOWN")
        journal = json.loads((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).read_text("utf-8"))
        self.assertEqual((journal["status"], journal["model_attempts"]), ("MODEL_REVIEW_REQUIRED", 2))
        self.assertEqual(self.run_at(SLOTS[2])["status"], "NO_NEW_ITEMS")
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (3, 2, 0))

    def test_repeated_definite_smtp_failure_stops_after_one_scheduled_retry(self):
        failed_factory = FakeFactory(FakeSession(ehlo=[(451, b"synthetic rejection"),
                                                          (451, b"synthetic rejection")]))
        self.assertEqual(self.run_at(SLOTS[0], smtp_factory=failed_factory)["status"], "FAILED")
        self.assertEqual(self.run_at(SLOTS[1], smtp_factory=failed_factory)["status"], "FAILED")
        journal = json.loads((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).read_text("utf-8"))
        self.assertEqual((journal["status"], journal["smtp_attempts"]), ("SMTP_REVIEW_REQUIRED", 2))
        self.assertEqual(self.run_at(SLOTS[2], smtp_factory=failed_factory)["status"], "NO_NEW_ITEMS")
        self.assertEqual((self.source_calls, len(self.transport.calls), len(failed_factory.calls)), (3, 1, 2))

    def test_model_recovery_survives_module_reload_like_a_process_restart(self):
        self.transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.assertEqual(self.run_at(SLOTS[0])["status"], "UNKNOWN")
        self.transport = FakeTransport(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        importlib.reload(monitor)
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["recovered_run_id"]),
                         ("SENT", monitor.due_slot(SLOTS[0])))
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 1))

    def test_overlap_lock_and_same_slot_replay_never_call_a_provider(self):
        with ProcessLock(self.root / "shared-manual.lock"):
            first = self.run_at(SLOTS[0])
        self.assertEqual(first["status"], "BUSY")
        self.assertEqual(self.run_at(SLOTS[0]), first)
        self.assertEqual((self.credential_reads, self.source_calls, len(self.transport.calls)), (0, 0, 0))
        self.assertEqual(self.run_at(SLOTS[1])["status"], "SENT")
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (1, 1, 1))

    def test_legacy_v1_result_is_reserved_without_mutating_historical_file(self):
        prior_run = monitor.due_slot(SLOTS[0])
        legacy_path = self.root / (prior_run + ".result.json")
        legacy_path.write_text(json.dumps({"schema_version": 1, "run_id": prior_run,
                                           "status": "SOURCE_READY", "tweets": self.tweets,
                                           "source": {"source_posts": 1}}), encoding="utf-8")
        original = legacy_path.read_bytes()
        result = self.run_at(SLOTS[1])
        self.assertEqual(result["status"], "NO_NEW_ITEMS")
        self.assertEqual(legacy_path.read_bytes(), original)
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (1, 0, 0))

    def test_manual_success_between_slots_suppresses_a_model_failed_id(self):
        self.transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.assertEqual(self.run_at(SLOTS[0])["status"], "UNKNOWN")
        handled = self.tweets[1]
        self.record_manual_success([handled])
        self.transport = FakeTransport(model_response(("POST_RESET", "PRE_RESET")))
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["analyzed_items"], second["recovered_run_id"]),
                         ("SENT", 2, monitor.due_slot(SLOTS[0])))
        journal = json.loads((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).read_text("utf-8"))
        self.assertEqual([tweet["id"] for tweet in journal["tweets"]],
                         [self.tweets[0]["id"], self.tweets[2]["id"]])
        self.assertEqual(journal["manual_suppressed_ids"], [handled["id"]])
        self.assertNotIn(handled["text"], journal["notification"]["body"])
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 1))

    def test_manual_success_between_slots_rebuilds_failed_mail_without_model(self):
        failed_factory = FakeFactory(FakeSession(ehlo=(451, b"synthetic rejection")))
        self.assertEqual(self.run_at(SLOTS[0], smtp_factory=failed_factory)["status"], "FAILED")
        handled = self.tweets[0]
        self.record_manual_success([handled])
        second = self.run_at(SLOTS[1])
        self.assertEqual((second["status"], second["analyzed_items"], second["reused_analysis_items"]),
                         ("SENT", 0, 2))
        journal = json.loads((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).read_text("utf-8"))
        self.assertEqual(journal["status"], "SENT")
        self.assertNotIn(handled["text"], journal["notification"]["body"])
        self.assertIn(self.tweets[2]["text"], journal["notification"]["body"])
        self.assertEqual((self.source_calls, len(self.transport.calls), len(failed_factory.calls),
                          len(self.factory.calls)), (2, 1, 1, 1))

    def test_fully_manual_completed_pending_is_not_recovered(self):
        self.transport = FakeTransport(FakeResponse({"choices": [], "usage": {}}))
        self.assertEqual(self.run_at(SLOTS[0])["status"], "UNKNOWN")
        self.record_manual_success(self.tweets)
        second = self.run_at(SLOTS[1])
        self.assertEqual(second["status"], "NO_NEW_ITEMS")
        journal = json.loads((self.root / (monitor.due_slot(SLOTS[0]) + ".result.json")).read_text("utf-8"))
        self.assertEqual(journal["status"], "SUPERSEDED_BY_MANUAL")
        self.assertEqual((self.source_calls, len(self.transport.calls), len(self.factory.calls)), (2, 1, 0))

    def test_service_heartbeat_is_written_without_provider_calls_before_next_slot(self):
        now = SLOTS[0].replace(minute=2)
        def stop(_):
            raise StopIteration()
        self.assertEqual(monitor.serve(clock=lambda: now, sleep=stop, root=self.root), 1)
        status = json.loads((self.root / "status.json").read_text("utf-8"))
        self.assertEqual(status["state"], "RUNNING")
        self.assertIsNone(status["last_slot"])
        self.assertEqual((self.credential_reads, self.source_calls), (0, 0))


if __name__ == "__main__":
    unittest.main()
