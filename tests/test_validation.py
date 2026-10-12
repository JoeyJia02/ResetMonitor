"""Actual persistent quotas and three adapter classes, artificial wire only."""
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from email import policy as email_policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from config import Config

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT
from monitor.batch import BatchAuthorization, BatchLedger, NoExternalCall
from monitor.credentials import ProjectCredentials
from monitor.deepseek_budget import PRICE_VERIFIED_AT, PRICE_SNAPSHOT_ID, TokenBoundProof
from monitor.fakes import FakeNotifier
from monitor.http_transport import HttpResponse
from monitor.notifier import DeliveryResult
from monitor.source_contract import SourceContract
from monitor.state import StateStore, initial_state
from monitor.validation import (ApifyCostProof, GuardedApifyTransport, GuardedSMTPNotifier,
    assemble_validation_monitor, validation_preflight)
from tests.test_batch import synthetic_authorization
from tests.test_smtp_transport import FakeSession


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime/test-tmp").resolve()
NOW = PRICE_VERIFIED_AT + timedelta(minutes=1)
BUILD_ID, RUN_ID, DATASET_ID = "B" * 17, "R" * 17, "D" * 17
SECRETS = ("synthetic-apify-secret", "synthetic-deepseek-secret", "synthetic-smtp-user", "synthetic-smtp-secret")


def validation_facts(state_path, *, now=NOW):
    """All approvals, bounds and evidence here are deliberately fictitious."""
    raw = synthetic_authorization(state_path, now=now)
    raw["expires_at"] = (now + timedelta(hours=23)).isoformat()
    raw["source_contract_evidence_id"] = "synthetic-offline-contract"
    raw["deepseek"].update(model="deepseek-flash", currency="CNY", price_snapshot_id=PRICE_SNAPSHOT_ID,
                           max_requests=9, amount="1")
    raw["apify"].update(max_run_starts=1, max_run_polls=3, max_dataset_reads=3, max_http_attempts=7)
    authorization = BatchAuthorization.from_dict(raw, now=now)
    config = Config(state_path=state_path, target_user_id=authorization.target_user_id)
    contract = SourceContract("synthetic-offline-contract", "lance_api~x-tweet-scraper-api", BUILD_ID,
        "0.0.123", "thsottiaux", authorization.target_user_id,
        ("https://apify.com/lance_api/x-tweet-scraper-api",), "a" * 64,
        (now - timedelta(minutes=1)).isoformat(), (now + timedelta(hours=23)).isoformat())
    proof = TokenBoundProof("tokenproof-synthetic-complete-request", "deepseek-flash",
                           now - timedelta(minutes=1), now + timedelta(hours=23), lambda _full_body: 2000)
    cost = ApifyCostProof("synthetic-apify-bound", contract.contract_id, BUILD_ID,
        now - timedelta(minutes=1), now + timedelta(hours=23),
        ("https://apify.com/pricing",), "b" * 64, Decimal("0.01"), Decimal("0.001"), Decimal("0.002"))
    settings = {"schema_version": 1, "smtp_host": "smtp.example.com", "smtp_port": 465,
        "smtp_security": "SSL", "apify_policy": {"available_balance_usd": "1",
        "max_total_charge_usd": "0.001", "minimum_charge_cap_usd": "0.001", "per_result_usd": "0.0003"}}
    return dict(config=config, settings=settings, authorization=authorization,
                source_catalog={contract.contract_id: contract}, token_proofs={proof.model: proof},
                apify_cost_proofs={cost.proof_id: cost})


def response(status, value, headers=None):
    return HttpResponse(status, json.dumps(value, ensure_ascii=False).encode(), headers or {})


def source_cycle(ids, *, bad_author=False):
    rows = [{"inputType": "profile", "normalizedInput": "thsottiaux", "success": True, "status": "success",
        "tweetId": str(i), "text": f"完整合成原文 {i}\n第二行。", "createdAt": f"2026-10-10T07:{i % 60:02d}:00Z",
        "url": f"https://x.com/thsottiaux/status/{i}", "authorUsername": "thsottiaux",
        "authorId": "999" if bad_author else "123456789",
        "author": {"username": "thsottiaux", "id": "123456789"},
        "isReply": i == 102, "isRetweet": False, "retweetedTweet": None} for i in ids]
    rows.sort(key=lambda row: row["createdAt"], reverse=True)
    headers = {"X-Apify-Pagination-Offset": "0", "X-Apify-Pagination-Limit": "3",
        "X-Apify-Pagination-Count": str(len(rows)), "X-Apify-Pagination-Total": str(len(rows)),
        "X-Apify-Pagination-Desc": "false"}
    return (response(201, {"data": {"id": RUN_ID, "status": "RUNNING", "defaultDatasetId": DATASET_ID, "buildId": BUILD_ID}}),
        response(200, {"data": {"id": RUN_ID, "status": "SUCCEEDED", "defaultDatasetId": DATASET_ID,
            "buildId": BUILD_ID, "usageTotalUsd": "0.0009", "chargedEventCounts": {"tweet-result": len(rows)}}}),
        response(200, rows, headers))


def completion(label):
    return response(200, {"id": "synthetic", "created": 1, "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(
            {"results": [{"id": "1", "label": label, "reset_time": None, "reason": "合成分类结果"}]})}}]})


class FakeHTTP:
    def __init__(self, *replies):
        self.replies, self.calls = deque(replies), []

    def request(self, method, url, *, headers, body, timeout, deadline=None):
        self.calls.append((method, url, body))
        if not self.replies:
            raise AssertionError("UNEXPECTED_FAKE_REQUEST")
        reply = self.replies.popleft()
        if isinstance(reply, BaseException): raise reply
        return reply


class FakeSMTP:
    def __init__(self): self.sessions, self.calls = deque(), []

    def __call__(self, host, port, **kwargs):
        self.calls.append((host, port))
        return self.sessions.popleft()


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TEST_ROOT / ("validation-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.now = NOW
        self.facts = validation_facts(self.directory / "live-state.json")
        self.ledger = BatchLedger(self.directory / "fixed-ledger.json", self.facts["authorization"], clock=lambda: self.now)
        self.ledger.initialize()
        self.http, self.ds, self.smtp = FakeHTTP(), FakeHTTP(), FakeSMTP()
        self.credentials = ProjectCredentials(**dict(zip(("apify_token", "deepseek_api_key", "smtp_user", "smtp_password"), SECRETS)))
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("REAL_EXTERNAL_CALL_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)

    def cleanup(self):
        target, root = self.directory.resolve(), TEST_ROOT.resolve()
        if target == root or target.parent != root: raise RuntimeError("UNSAFE_CLEANUP")
        shutil.rmtree(target)

    def monitor(self, **changes):
        facts = dict(self.facts, credentials=self.credentials, ledger=self.ledger,
            apify_transport=self.http, deepseek_transport=self.ds, smtp_factory=self.smtp,
            clock=lambda: self.now, sleep=lambda _seconds: None, monotonic=lambda: 0.0)
        facts.update(changes)
        return assemble_validation_monitor(**facts)

    def test_all_default_evidence_gates_remain_closed_despite_synthetic_approval_booleans(self):
        issues = validation_preflight(self.facts["config"], self.facts["settings"], self.facts["authorization"], now=self.now)
        for code in ("APIFY_SOURCE_CONTRACT_UNVERIFIED", "DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE", "APIFY_TOTAL_COST_BOUND_UNVERIFIED"):
            self.assertIn(code, issues)
        with self.assertRaises(NoExternalCall): self.monitor(source_catalog=None, token_proofs=None, apify_cost_proofs=None)
        self.assertEqual(self.http.calls + self.ds.calls + self.smtp.calls, [])

    def test_validation_rejects_wrong_timezone_or_extra_daily_slot_before_services(self):
        for change in ({"schedule_timezone": "UTC"},
                       {"schedule_times": ("02:00", "08:00", "13:00", "20:00")},
                       {"schedule_times": ["08:00", "13:00", "20:00"]}):
            with self.subTest(fields=tuple(change)):
                config = replace(self.facts["config"], **change)
                issues = validation_preflight(config, self.facts["settings"], self.facts["authorization"], now=self.now)
                self.assertIn("VALIDATION_SCOPE_BINDING_MISMATCH", issues)
                with self.assertRaises(NoExternalCall):
                    self.monitor(config=config)
        self.assertEqual(self.http.calls + self.ds.calls + self.smtp.calls, [])

    def test_three_adapters_actual_ledger_new_three_test_subject_and_spent_start_blocks_next_cycle(self):
        store = StateStore(self.facts["config"].state_path)
        baseline = initial_state("thsottiaux", execution_mode="LIVE", target_user_id="123456789")
        baseline.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        store.save(baseline)
        self.http.replies.extend(source_cycle([103, 102, 101]))
        self.ds.replies.extend(completion(label) for label in ("PRE_RESET", "IRRELEVANT", "POST_RESET"))
        sessions = [FakeSession(), FakeSession()]
        self.smtp.sessions.extend(sessions)
        monitor = self.monitor()
        result = monitor.run()
        self.assertEqual((result.status, result.classified, result.sent), ("OK", 3, 2))
        result = self.monitor().run()
        self.assertEqual((result.status, result.classified, result.sent), ("DEGRADED", 0, 0))
        self.assertEqual(len(self.http.calls), 3)
        counts = self.ledger.validate_ready()["counts"]
        self.assertEqual((counts["apify_start"], counts["apify_poll"], counts["apify_dataset"], counts["deepseek_request"], counts["smtp_attempt"]), (1, 1, 1, 3, 2))
        for session in sessions:
            message = BytesParser(policy=email_policy.default).parsebytes(session.payloads[0][:-3])
            self.assertTrue(str(message["Subject"]).startswith("【ResetMonitor 测试】"))
            self.assertIn("完整合成原文", message.get_content())
        state = StateStore(self.facts["config"].state_path).load()
        self.assertEqual(state["target_user_id"], "123456789")
        self.assertEqual({item["tweet"]["author_id"] for item in state["processed_ids"].values()}, {"123456789"})
        combined = self.ledger.path.read_text(encoding="utf-8") + self.ledger.path.with_name(self.ledger.path.name + ".guard").read_text(encoding="utf-8") + self.facts["config"].state_path.read_text(encoding="utf-8")
        for secret in SECRETS: self.assertNotIn(secret, combined)

    def test_wrong_author_does_not_advance_watermark_or_classify_but_attempts_stay_reserved(self):
        store = StateStore(self.facts["config"].state_path)
        state = initial_state("thsottiaux", execution_mode="LIVE", target_user_id="123456789")
        state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        store.save(state)
        self.http.replies.extend(source_cycle([103, 102, 101], bad_author=True))
        result = self.monitor().run()
        self.assertEqual(result.status, "DEGRADED")
        self.assertEqual(store.load()["last_seen_id"], "100")
        self.assertEqual(self.ds.calls, [])
        self.assertEqual(self.ledger.validate_ready()["counts"]["apify_http_attempts"], 3)

    def test_known_run_get_retry_uses_same_run_and_reserves_every_attempt(self):
        cycle = source_cycle([100, 99, 98])
        self.http.replies.extend((cycle[0], response(503, {"error": "synthetic"})))
        monitor = self.monitor()
        self.assertEqual(monitor.run().status, "DEGRADED")
        self.http.replies.extend(cycle[1:])
        self.assertEqual(self.monitor().run().status, "OK")
        self.assertEqual([call[0] for call in self.http.calls], ["POST", "GET", "GET", "GET"])
        counts = self.ledger.validate_ready()["counts"]
        self.assertEqual((counts["apify_start"], counts["apify_poll"], counts["apify_dataset"]), (1, 2, 1))

    def test_all_mail_kinds_failures_and_uncertainty_share_one_attempt_limit(self):
        notifier = FakeNotifier(outcomes=[DeliveryResult("UNKNOWN"), DeliveryResult("FAILED"), DeliveryResult("SENT")])
        guarded = GuardedSMTPNotifier(notifier, self.ledger)
        notice = {"recipient": LOCAL_RECIPIENT, "subject": "【ResetMonitor 测试】合成", "label": "PRE_RESET"}
        for label in ("PRE_RESET", "PRE_RESET", "HEALTH"):
            self.assertIn(guarded.send(dict(notice, label=label)).status, {"UNKNOWN", "FAILED", "SENT"})
        result = guarded.send(notice)
        self.assertEqual((result.status, result.retryable), ("FAILED", False))
        self.assertEqual(len(notifier.calls), 3)
        self.assertEqual(self.ledger.validate_ready()["counts"]["smtp_attempt"], 3)

    def test_mail_reservation_commit_failure_is_known_not_sent_and_never_unknown(self):
        ledger = BatchLedger(self.ledger.path, self.facts["authorization"], clock=lambda: self.now,
            fault_hook=lambda event, _state: (_ for _ in ()).throw(OSError("synthetic failure")) if event == "before_save" else None)
        notifier = FakeNotifier()
        result = GuardedSMTPNotifier(notifier, ledger).send({"recipient": LOCAL_RECIPIENT, "subject": "【ResetMonitor 测试】合成"})
        self.assertEqual((result.status, result.retryable), ("FAILED", False))
        self.assertEqual(notifier.calls, [])

    def test_missing_ledger_refuses_before_post_and_preserves_paused_start(self):
        raw = self.facts["authorization"].to_dict()
        raw["ledger_path"] = str(self.directory / "uninitialized-ledger.json")
        self.facts["authorization"] = BatchAuthorization.from_dict(raw, now=self.now)
        missing = BatchLedger(self.directory / "uninitialized-ledger.json", self.facts["authorization"], clock=lambda: self.now)
        result = self.monitor(ledger=missing).run()
        self.assertEqual(result.status, "DEGRADED")
        self.assertEqual(self.http.calls, [])
        state = StateStore(self.facts["config"].state_path).load()
        self.assertEqual(state["active_fetch"]["phase"], "PAUSED")
        self.assertIsNone(state["last_seen_id"])

    def test_current_validation_caps_cannot_be_enlarged_by_authorization(self):
        for section, field, excessive in (("apify", "max_run_starts", 2),
                ("apify", "max_run_polls", 4), ("apify", "max_dataset_reads", 4),
                ("apify", "max_http_attempts", 8), ("deepseek", "max_requests", 10),
                ("smtp", "max_attempts", 4)):
            with self.subTest(field=field):
                raw = self.facts["authorization"].to_dict()
                raw[section][field] = excessive
                facts = dict(self.facts, authorization=BatchAuthorization.from_dict(raw, now=self.now))
                self.assertIn("VALIDATION_OPERATION_LIMIT_EXCEEDS_ENVELOPE",
                    validation_preflight(**facts, now=self.now))
        self.assertEqual(self.http.calls + self.ds.calls + self.smtp.calls, [])

    def test_existing_live_state_without_stable_id_is_not_silently_rebound(self):
        store = StateStore(self.facts["config"].state_path)
        store.save(initial_state("thsottiaux", execution_mode="LIVE"))
        with self.assertRaisesRegex(Exception, "identity"):
            self.monitor().run()
        self.assertEqual(self.http.calls + self.ds.calls + self.smtp.calls, [])

    def test_expired_batch_cannot_reuse_mail_attempts_or_source_request(self):
        self.now += timedelta(hours=23)
        result = GuardedSMTPNotifier(FakeNotifier(), self.ledger).send({"recipient": LOCAL_RECIPIENT, "subject": "【ResetMonitor 测试】合成"})
        self.assertEqual((result.status, result.retryable), ("FAILED", False))
        with self.assertRaises(NoExternalCall): self.monitor()
        self.assertEqual(self.http.calls + self.ds.calls + self.smtp.calls, [])


if __name__ == "__main__": unittest.main()
