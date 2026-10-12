"""Offline transactions and recovery, with real local state files and fake IO."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from config import Config
from monitor.classifier import ClassifierReply
from monitor.engine import Monitor
from monitor.fakes import FakeClassifier, FakeFetcher, FakeNotifier
from monitor.locking import ProcessLock
from monitor.models import FetchSnapshot, SourceContractError, StageFailure, validate_snapshot
from monitor.notifier import DeliveryResult
from monitor.state import StateError, StateStore, initial_state

ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = Path(os.environ.get(
    "RESET_MONITOR_TEST_ROOT", str(ROOT / "runtime" / "test-tmp"),
)).resolve()


def tweet(identifier, text=None, hour=1):
    return {"id": str(identifier), "author": "thsottiaux", "text": text or f"Original tweet {identifier}",
            "url": f"https://x.com/thsottiaux/status/{identifier}", "created_at": f"2026-10-10T{hour:02}:00:00Z"}


def classification(label="PRE_RESET"):
    return {"label": label, "confidence": 0.95, "reset_time": None, "reason": "Offline test classification."}


class Crash(BaseException):
    pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = TEST_TMP
        self.directory = self.temp_root / uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.config = Config.from_env({"STATE_FILE": str(self.directory / "state.json")}, ROOT)
        self.store = StateStore(self.config.state_path)
        self.now = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
        self.sleeps = []

    def cleanup(self):
        resolved = self.directory.resolve()
        if resolved.parent != self.temp_root or resolved == self.temp_root:
            raise RuntimeError("Refusing cleanup outside the isolated test directory")
        shutil.rmtree(resolved)

    def monitor(self, fetcher=None, classifier=None, notifier=None, hook=None, config=None):
        return Monitor(config or self.config, self.store, fetcher or FakeFetcher(),
                       classifier or FakeClassifier(), notifier or FakeNotifier(),
                       clock=lambda: self.now, sleep=self.sleeps.append, fault_hook=hook)

    def baseline(self, identifier="100"):
        self.monitor(FakeFetcher([tweet(identifier)])).run()

    def advance(self, hours):
        self.now += timedelta(hours=hours)

    def crash_at(self, event):
        def hook(actual, state):
            if actual == event:
                raise Crash()
        return hook

    def test_T21_first_baseline_and_empty_initial_fetch(self):
        self.assertFalse(self.monitor().run().details["initialized"])
        classifier, notifier = FakeClassifier(), FakeNotifier()
        self.monitor(FakeFetcher([tweet(99), tweet(100)]), classifier, notifier).run()
        state = self.store.load()
        self.assertEqual(state["initial_baseline_id"], "100")
        self.assertEqual(state["processed_ids"], {})
        self.assertEqual((classifier.calls, notifier.calls), ([], []))
        self.monitor(FakeFetcher([tweet(98), tweet(99)]), classifier, notifier).run()
        self.assertEqual(self.store.load()["processed_ids"], {})

    def test_T01_T04_ten_hours_without_new_tweets_is_healthy(self):
        self.baseline()
        classifier, notifier = FakeClassifier(), FakeNotifier()
        for _ in range(5):
            self.advance(2)
            self.assertEqual(self.monitor(FakeFetcher([tweet(100)]), classifier, notifier).run().status, "OK")
        state = self.store.load()
        self.assertEqual(state["health"]["consecutive_failures"], 0)
        self.assertEqual(state["last_success_at"], self.now.isoformat())
        self.assertEqual((classifier.calls, notifier.calls), ([], []))

    def test_T02_raw_tweet_is_saved_before_classification(self):
        self.baseline()
        original = "Unchanged original\nwith multiple lines"
        classifier = FakeClassifier({"101": classification()})
        original_method = classifier.classify
        def checked(item, **kwargs):
            saved = self.store.load()
            self.assertEqual(saved["inbox"]["101"]["tweet"]["text"], original)
            self.assertEqual(saved["last_seen_id"], "101")
            return original_method(item, **kwargs)
        classifier.classify = checked
        result = self.monitor(FakeFetcher([tweet(101, original)]), classifier).run()
        self.assertEqual((result.ingested, result.classified, result.sent), (1, 1, 1))

    def test_T03_T17_three_delivered_items_order_and_no_reclassification(self):
        self.baseline()
        fetcher = FakeFetcher([tweet(103, hour=3), tweet(101, hour=1), tweet(102, hour=2)])
        classifier = FakeClassifier({str(i): classification() for i in (101, 102, 103)})
        notifier = FakeNotifier()
        self.monitor(fetcher, classifier, notifier).run()
        self.assertEqual([t["id"] for t in classifier.calls], ["101", "102", "103"])
        self.monitor(fetcher, classifier, notifier).run()
        self.assertEqual(fetcher.calls, [3, 3])
        self.assertEqual((len(classifier.calls), len(notifier.calls)), (3, 3))
        state = self.store.load()
        self.assertEqual(len(state["notified_ids"]), 3)
        for key, sent in state["notified_ids"].items():
            self.assertEqual(state["pending_notifications"][key]["status"], "SENT")
            self.assertEqual(state["pending_notifications"][key]["sent_at"], sent["sent_at"])

    def test_T05_reject_four_raw_items_even_if_one_is_duplicate(self):
        self.baseline()
        for items in ([tweet(i) for i in range(101, 105)], [tweet(101)] * 4):
            with self.subTest(items=len(items)):
                classifier, notifier = FakeClassifier(), FakeNotifier()
                result = self.monitor(FakeFetcher(items), classifier, notifier).run()
                self.assertEqual(result.status, "DEGRADED")
                self.assertEqual(self.store.load()["last_seen_id"], "100")
                self.assertEqual((classifier.calls, notifier.calls), ([], []))

    def test_T12_partial_snapshot_cannot_advance_or_queue(self):
        self.baseline()
        result = self.monitor(FakeFetcher([tweet(101)], complete=False)).run()
        state = self.store.load()
        self.assertEqual(result.status, "DEGRADED")
        self.assertEqual(state["last_seen_id"], "100")
        self.assertEqual(state["inbox"], {})

    def test_T10_failed_old_item_recovers_outside_the_next_snapshot(self):
        self.baseline()
        classifier = FakeClassifier({"102": classification("IRRELEVANT")}, outcomes=[StageFailure()] * 3)
        self.monitor(FakeFetcher([tweet(101), tweet(102, hour=2)]), classifier).run()
        state = self.store.load()
        self.assertIn("101", state["inbox"])
        self.assertIn("102", state["processed_ids"])
        self.assertEqual(self.sleeps, [5, 15])
        self.advance(2)
        recovered = FakeClassifier({"101": classification()})
        self.monitor(FakeFetcher(), recovered).run()
        self.assertEqual([t["id"] for t in recovered.calls], ["101"])
        self.assertEqual(self.store.load()["inbox"], {})

    def test_T12_source_failure_does_not_block_local_queue_recovery(self):
        self.baseline()
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101)]), hook=self.crash_at("after_fetch_saved")).run()
        classifier = FakeClassifier({"101": classification("IRRELEVANT")})
        result = self.monitor(FakeFetcher(outcomes=[StageFailure()]), classifier).run()
        self.assertEqual(result.status, "DEGRADED")
        self.assertEqual(result.classified, 1)
        self.assertIn("101", self.store.load()["processed_ids"])

    def test_T11_invalid_classifier_output_is_not_irrelevant(self):
        self.baseline()
        classifier = FakeClassifier(outcomes=[ClassifierReply("{}")] * 3)
        self.monitor(FakeFetcher([tweet(101)]), classifier).run()
        state = self.store.load()
        self.assertEqual(len(classifier.calls), 3)
        self.assertEqual(state["processed_ids"], {})
        self.assertIn("classify:101", state["failed_items"])
        self.assertIn("101", state["inbox"])

    def test_T15_T16_crash_after_ingest_keeps_complete_raw_snapshot(self):
        self.baseline()
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101), tweet(102), tweet(103)]), hook=self.crash_at("after_fetch_saved")).run()
        state = self.store.load()
        self.assertEqual(state["last_seen_id"], "103")
        self.assertEqual(set(state["inbox"]), {"101", "102", "103"})
        classifier = FakeClassifier({str(i): classification("IRRELEVANT") for i in range(101, 104)})
        self.assertEqual(self.monitor(classifier=classifier).run().classified, 3)

    def test_T15_crash_after_classification_response_may_repeat_only_uncommitted_work(self):
        self.baseline()
        classifier = FakeClassifier({"101": classification("IRRELEVANT")})
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101)]), classifier, hook=self.crash_at("after_classification_response")).run()
        self.assertIn("101", self.store.load()["inbox"])
        self.monitor(classifier=classifier).run()
        self.assertEqual(len(classifier.calls), 2)

    def test_T15_classification_and_outbox_commit_survive_crash(self):
        self.baseline()
        classifier, notifier = FakeClassifier({"101": classification()}), FakeNotifier()
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101)]), classifier, notifier, hook=self.crash_at("after_classification_saved")).run()
        state = self.store.load()
        self.assertIn("101", state["processed_ids"])
        self.assertEqual(state["pending_notifications"]["101:PRE_RESET"]["status"], "PENDING")
        self.monitor(classifier=classifier, notifier=notifier).run()
        self.assertEqual((len(classifier.calls), len(notifier.calls)), (1, 1))

    def test_T14_T16_sending_crash_becomes_unknown_without_immediate_resend(self):
        self.baseline()
        notifier = FakeNotifier()
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier,
                         hook=self.crash_at("after_sending_saved")).run()
        self.assertEqual(notifier.calls, [])
        self.monitor(notifier=notifier).run()
        state = self.store.load()
        notification = state["pending_notifications"]["101:PRE_RESET"]
        self.assertEqual(notification["status"], "UNKNOWN")
        deadline = notification["next_retry_at"]
        self.advance(2)
        self.monitor(notifier=notifier).run()
        self.assertEqual(self.store.load()["pending_notifications"]["101:PRE_RESET"]["next_retry_at"], deadline)
        self.assertEqual(notifier.calls, [])

    def test_T15_fake_acceptance_before_commit_recovers_conservatively(self):
        self.baseline()
        notifier = FakeNotifier()
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier,
                         hook=self.crash_at("after_delivery")).run()
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(self.store.load()["notified_ids"], {})
        self.monitor(notifier=notifier).run()
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(self.store.load()["pending_notifications"]["101:PRE_RESET"]["status"], "UNKNOWN")

    def test_T29_unknown_budget_is_committed_before_resend_and_failed_cannot_bypass(self):
        self.baseline()
        notifier = FakeNotifier(outcomes=[DeliveryResult("UNKNOWN"), DeliveryResult("FAILED")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.advance(23)
        self.monitor(notifier=notifier).run()
        self.assertEqual(len(notifier.calls), 1)
        self.advance(1)
        method = notifier.send
        def checked(notification):
            saved = self.store.load()["pending_notifications"][notification["key"]]
            self.assertEqual(saved["unknown_retry_used"], 1)
            self.assertEqual(saved["status"], "SENDING")
            return method(notification)
        notifier.send = checked
        self.monitor(notifier=notifier).run()
        notice = self.store.load()["pending_notifications"]["101:PRE_RESET"]
        self.assertEqual((notice["status"], notice["unknown_retry_used"], notice["needs_attention"]), ("FAILED", 1, True))
        notifier.send = method
        for _ in range(3):
            self.advance(48)
            self.monitor(notifier=notifier).run()
        business = [v for v in notifier.calls if v["label"] != "HEALTH"]
        self.assertEqual(len(business), 2)

    def test_T29_second_unknown_pauses_automatic_resend(self):
        self.baseline()
        notifier = FakeNotifier(outcomes=[DeliveryResult("UNKNOWN"), DeliveryResult("UNKNOWN")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.advance(24)
        self.monitor(notifier=notifier).run()
        notice = self.store.load()["pending_notifications"]["101:PRE_RESET"]
        self.assertTrue(notice["needs_attention"])
        self.assertEqual(notice["unknown_retry_used"], 1)

    def test_T29_crash_after_budget_commit_before_sending_cannot_resend(self):
        self.baseline()
        notifier = FakeNotifier(outcomes=[DeliveryResult("UNKNOWN")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.advance(24)
        original_save = self.store.save
        def crash_after_budget(state):
            original_save(state)
            notice = state["pending_notifications"]["101:PRE_RESET"]
            if notice["status"] == "UNKNOWN" and notice["unknown_retry_used"] == 1:
                raise Crash()
        with patch.object(self.store, "save", side_effect=crash_after_budget):
            with self.assertRaises(Crash):
                self.monitor(notifier=notifier).run()
        self.store = StateStore(self.config.state_path)
        notice = self.store.load()["pending_notifications"]["101:PRE_RESET"]
        self.assertEqual((notice["status"], notice["unknown_retry_used"], notice["needs_attention"]), ("UNKNOWN", 1, True))
        self.monitor(notifier=notifier).run()
        self.monitor(notifier=notifier).run(resume_paused=True)
        business = [n for n in notifier.calls if n["tweet_id"] == "101"]
        self.assertEqual(len(business), 1)

    def test_T13_confirmed_failure_retries_once_per_due_round(self):
        self.baseline()
        notifier = FakeNotifier(outcomes=[DeliveryResult("FAILED"), DeliveryResult("SENT")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.monitor(notifier=notifier).run()
        self.assertEqual(len(notifier.calls), 1)
        self.advance(2)
        self.monitor(notifier=notifier).run()
        self.assertEqual(len(notifier.calls), 2)
        self.assertIn("101:PRE_RESET", self.store.load()["notified_ids"])

    def test_T31_permanent_classification_and_delivery_failures_pause(self):
        self.baseline()
        classifier = FakeClassifier(outcomes=[StageFailure(retryable=False)])
        self.monitor(FakeFetcher([tweet(101)]), classifier).run()
        self.advance(48)
        self.monitor(classifier=classifier).run()
        self.assertEqual(len(classifier.calls), 1)
        self.assertFalse(self.store.load()["inbox"]["101"]["retryable"])
        classifier.classifications["101"] = classification("IRRELEVANT")
        self.monitor(classifier=classifier).run(resume_paused=True)
        self.assertIn("101", self.store.load()["processed_ids"])

    def test_T31_permanent_source_failure_pauses_fetch_but_recovers_local_work(self):
        self.baseline()
        with self.assertRaises(Crash):
            self.monitor(FakeFetcher([tweet(101)]), hook=self.crash_at("after_fetch_saved")).run()
        fetcher = FakeFetcher(outcomes=[StageFailure(retryable=False)])
        classifier = FakeClassifier({"101": classification("IRRELEVANT")})
        self.monitor(fetcher, classifier).run()
        self.assertFalse(self.store.load()["failed_items"]["fetch"]["retryable"])
        result = self.monitor(fetcher, classifier).run()
        self.assertEqual(result.errors, ["FETCH_PAUSED"])
        self.assertEqual(fetcher.calls, [3])
        self.assertIn("101", self.store.load()["processed_ids"])
        self.monitor(fetcher, classifier).run(resume_paused=True)
        self.assertEqual(fetcher.calls, [3, 3])
        self.assertNotIn("fetch", self.store.load()["failed_items"])

    def test_T31_explicit_retry_only_unpauses_confirmed_delivery_failures(self):
        self.baseline()
        notifier = FakeNotifier(outcomes=[DeliveryResult("FAILED", retryable=False), DeliveryResult("SENT")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.advance(48)
        self.monitor(notifier=notifier).run()
        self.assertEqual(len(notifier.calls), 1)
        self.monitor(notifier=notifier).run(resume_paused=True)
        self.assertEqual(len(notifier.calls), 2)
        # The same operator option must not reset a spent UNKNOWN budget.
        notifier = FakeNotifier(outcomes=[DeliveryResult("UNKNOWN"), DeliveryResult("FAILED", retryable=False)])
        self.monitor(FakeFetcher([tweet(102)]), FakeClassifier({"102": classification()}), notifier).run()
        self.advance(24)
        self.monitor(notifier=notifier).run()
        self.monitor(notifier=notifier).run(resume_paused=True)
        business = [n for n in notifier.calls if n["tweet_id"] == "102"]
        self.assertEqual(len(business), 2)
        self.assertEqual(self.store.load()["pending_notifications"]["102:PRE_RESET"]["unknown_retry_used"], 1)

    def test_unknown_exception_is_not_assumed_definitely_undelivered(self):
        self.baseline()
        notifier = FakeNotifier(outcomes=[TimeoutError("SECRET MUST NOT BE PERSISTED")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.assertEqual(self.store.load()["pending_notifications"]["101:PRE_RESET"]["status"], "UNKNOWN")
        self.assertNotIn("SECRET MUST", self.config.state_path.read_text(encoding="utf-8"))

    def test_T28_confirmed_acceptance_result_stays_sent_despite_quit_error_code(self):
        # This exercises the transport contract, not a real SMTP DATA exchange.
        self.baseline()
        notifier = FakeNotifier(outcomes=[DeliveryResult("SENT", error_code="QUIT_TIMEOUT")])
        self.monitor(FakeFetcher([tweet(101)]), FakeClassifier({"101": classification()}), notifier).run()
        self.monitor(notifier=notifier).run()
        state = self.store.load()
        self.assertEqual(state["pending_notifications"]["101:PRE_RESET"]["status"], "SENT")
        self.assertIn("101:PRE_RESET", state["notified_ids"])
        self.assertEqual(len(notifier.calls), 1)

    def test_T18_explicit_new_evidence_allows_one_upgrade(self):
        self.baseline()
        classifier = FakeClassifier({"101": classification("POSSIBLE_RESET")})
        notifier = FakeNotifier()
        self.monitor(FakeFetcher([tweet(101)]), classifier, notifier).run()
        self.monitor(FakeFetcher([tweet(101)]), classifier, notifier).run()
        self.assertEqual(len(classifier.calls), 1)
        classifier.classifications["101"] = classification("PRE_RESET")
        request = {"tweet_id": "101", "evidence": "Explicit new confirmation", "basis": "Operator supplied new source context"}
        self.monitor(classifier=classifier, notifier=notifier).run(upgrade_requests=[request])
        self.monitor(classifier=classifier, notifier=notifier).run(upgrade_requests=[request])
        self.assertEqual(len(classifier.calls), 2)
        self.assertEqual([n["label"] for n in notifier.calls], ["POSSIBLE_RESET", "PRE_RESET"])
        self.assertEqual(len(self.store.load()["processed_ids"]["101"]["evidence_hashes"]), 1)

    def test_T22_delayed_id_below_watermark_but_after_baseline_is_processed(self):
        self.baseline()
        self.monitor(FakeFetcher([tweet(110)]), FakeClassifier({"110": classification("IRRELEVANT")})).run()
        classifier = FakeClassifier({"105": classification("IRRELEVANT")})
        self.monitor(FakeFetcher([tweet(105)]), classifier).run()
        self.assertEqual([t["id"] for t in classifier.calls], ["105"])
        self.assertEqual(self.store.load()["last_seen_id"], "110")

    def test_T23_field_author_and_uint64_contract_errors(self):
        for bad in ({**tweet(101), "id": "18446744073709551616"},
                    {**tweet(101), "author": "someone_else"},
                    {k: v for k, v in tweet(101).items() if k != "created_at"}):
            with self.subTest(bad=bad):
                with self.assertRaises(SourceContractError):
                    validate_snapshot(FetchSnapshot([bad]), "thsottiaux")

    def test_source_rejects_invalid_offset_components_without_normalizing(self):
        for offset in ("+00:60", "+00:99", "-01:60", "+24:00", "+0060", "-0099"):
            with self.subTest(offset=offset), self.assertRaises(SourceContractError):
                validate_snapshot(FetchSnapshot([{**tweet(101), "created_at": "2026-10-10T12:00:00" + offset}]), "thsottiaux")
        for value in ("2026-10-10T12:00:00+08:00", "20261010T120000+0800", "2026-10-10T12:00Z"):
            self.assertEqual(len(validate_snapshot(FetchSnapshot([{**tweet(101), "created_at": value}]), "thsottiaux")), 1)

    def test_T27_single_health_incident_survives_smtp_failure_then_recovers(self):
        self.baseline()
        fetcher = FakeFetcher(outcomes=[StageFailure()] * 4)
        notifier = FakeNotifier(outcomes=[DeliveryResult("FAILED"), DeliveryResult("SENT")])
        incident_key = None
        for round_number in range(1, 5):
            self.monitor(fetcher, notifier=notifier).run()
            notices = [n for n in self.store.load()["pending_notifications"].values() if n["label"] == "HEALTH"]
            self.assertEqual(len(notices), 0 if round_number < 3 else 1)
            if round_number == 3:
                incident_key = notices[0]["key"]
                self.assertEqual(len(notifier.calls), 1)
            elif round_number == 4:
                self.assertEqual(notices[0]["key"], incident_key)
                self.assertEqual(len(notifier.calls), 1)
        health = [n for n in self.store.load()["pending_notifications"].values() if n["label"] == "HEALTH"]
        self.assertEqual(len(health), 1)
        snapshot_body = health[0]["body"]
        self.advance(2)
        self.assertEqual(self.monitor(notifier=notifier).run().status, "OK")
        state = self.store.load()
        self.assertIsNone(state["health"]["incident_id"])
        self.assertIsNotNone(state["health"]["recovered_at"])
        self.assertEqual(state["pending_notifications"][health[0]["key"]]["body"], snapshot_body)
        self.assertEqual(len(notifier.calls), 2)

    def test_T24_all_new_items_are_classified_without_keyword_or_confidence_filter(self):
        # Deliberately unrelated-looking text and supplied labels exercise routing,
        # not actual semantic correctness of a model.
        self.baseline()
        tweets = [tweet(101, "天气很好。"), tweet(102, "12345", hour=2), tweet(103, "Hello.", hour=3)]
        labels = ("PRE_RESET", "POST_RESET", "POSSIBLE_RESET")
        outputs = {str(101 + i): {**classification(label), "confidence": 0.01} for i, label in enumerate(labels)}
        classifier, notifier = FakeClassifier(outputs), FakeNotifier()
        result = self.monitor(FakeFetcher(tweets), classifier, notifier).run()
        self.assertEqual([t["id"] for t in classifier.calls], ["101", "102", "103"])
        self.assertEqual([n["label"] for n in notifier.calls], list(labels))
        self.assertEqual((result.classified, result.sent), (3, 3))

    def test_T20_lock_conflict_does_not_read_state_or_call_services(self):
        self.baseline()
        before = self.config.state_path.read_bytes()
        fetcher, classifier, notifier = FakeFetcher(), FakeClassifier(), FakeNotifier()
        with ProcessLock(str(self.config.state_path) + ".lock"):
            with patch.object(self.store, "load", side_effect=AssertionError("Must not read")):
                self.assertEqual(self.monitor(fetcher, classifier, notifier).run().status, "LOCKED")
        self.assertEqual(self.config.state_path.read_bytes(), before)
        self.assertEqual((fetcher.calls, classifier.calls, notifier.calls), ([], [], []))

    def test_dry_run_is_read_only_including_retry_budget_health_and_lock(self):
        self.baseline()
        def files():
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir() if p.is_file()}
        before = files()
        fetcher, classifier, notifier = FakeFetcher(), FakeClassifier(), FakeNotifier()
        result = self.monitor(fetcher, classifier, notifier).run(dry_run=True)
        self.assertEqual(result.status, "DRY_RUN")
        self.assertEqual(files(), before)
        self.assertEqual((fetcher.calls, classifier.calls, notifier.calls), ([], [], []))
        absent = self.directory / "absent" / "state.json"
        config = replace(self.config, state_path=absent)
        monitor = Monitor(config, StateStore(absent), fetcher, classifier, notifier)
        self.assertEqual(monitor.run(dry_run=True).status, "DRY_RUN")
        self.assertFalse(absent.parent.exists())

    def test_T19_backup_recovery_repairs_file_but_pauses_all_service_calls(self):
        self.baseline()
        self.monitor().run()
        self.config.state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateError):
            self.monitor().run()
        fetcher, classifier, notifier = FakeFetcher(), FakeClassifier(), FakeNotifier()
        result = self.monitor(fetcher, classifier, notifier).run(recover_state=True)
        self.assertEqual(result.status, "RECOVERY_REQUIRED")
        self.assertTrue(self.store.load()["recovery_required"])
        self.assertTrue(list(self.directory.glob("*.corrupt-*")))
        self.assertEqual(self.monitor(fetcher, classifier, notifier).run().status, "RECOVERY_REQUIRED")
        self.assertEqual((fetcher.calls, classifier.calls, notifier.calls), ([], [], []))


if __name__ == "__main__":
    unittest.main()
