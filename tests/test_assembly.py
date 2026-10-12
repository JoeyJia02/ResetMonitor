"""Three actual adapters, artificial wire boundaries, and no external services."""

from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email import policy as email_policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import shutil
import ssl
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from config import Config
from monitor.apify import ApifyFetcher, ApifyPolicy
from monitor.assembly import (
    AUTHORIZED_RECIPIENT, IntegrationCredentials, PreparedIntegration,
    PreflightError, assemble_monitor, preflight,
)
from monitor.deepseek import DeepSeekClassifier
from monitor.batch import BatchAuthorization, BatchLedger
from monitor.deepseek_budget import PRICE_SNAPSHOT_ID, PRICE_VERIFIED_AT, TokenBoundProof
from monitor.http_transport import HttpResponse
from monitor.smtp_transport import SMTPNotifier
from monitor.state import StateStore, initial_state
from tests.test_smtp_transport import FakeSession
from tests.test_apify import identity_contract
from tests.test_batch import synthetic_authorization


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime" / "test-tmp").resolve()
NOW = PRICE_VERIFIED_AT + timedelta(minutes=1)
RUN_ID, DATASET_ID = "R" * 17, "D" * 17
CREDENTIALS = IntegrationCredentials(
    apify_token="synthetic-apify-token", deepseek_api_key="synthetic-deepseek-key",
    smtp_user="synthetic-smtp-user", smtp_password="synthetic-smtp-password",
)


def json_response(status, value, headers=None):
    return HttpResponse(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), headers or {})


def source_cycle(identifiers):
    rows = [{
        "inputType": "profile", "normalizedInput": "thsottiaux", "success": True, "status": "success",
        "tweetId": str(identifier), "text": f"完整原文 {identifier}\n第二行。",
        "createdAt": f"2026-10-10T07:{identifier % 60:02d}:00Z",
        "url": f"https://x.com/thsottiaux/status/{identifier}", "authorUsername": "thsottiaux",
        "authorId": "12345", "author": {"id": "12345", "username": "thsottiaux"},
        "isReply": identifier == 102, "isRetweet": False, "retweetedTweet": None,
    } for identifier in identifiers]
    # Provider ordering is chronological latest-first, not decimal-ID order.
    rows.sort(key=lambda row: row["createdAt"], reverse=True)
    headers = {
        "X-Apify-Pagination-Offset": "0", "X-Apify-Pagination-Limit": "3",
        "X-Apify-Pagination-Count": str(len(rows)), "X-Apify-Pagination-Total": str(len(rows)),
        "X-Apify-Pagination-Desc": "false",
    }
    return (
        json_response(201, {"data": {"id": RUN_ID, "status": "RUNNING", "defaultDatasetId": DATASET_ID,
                                    "buildId": "B" * 17}}),
        json_response(200, {"data": {"id": RUN_ID, "status": "SUCCEEDED", "defaultDatasetId": DATASET_ID,
                                    "buildId": "B" * 17,
                                    "chargedEventCounts": {"tweet-result": len(rows)}, "usageTotalUsd": "0.0009"}}),
        json_response(200, rows, headers),
    )


def completion(label):
    classification = {"label": label, "confidence": 0.9, "reset_time": None,
                      "reason": "人工离线标注；未进行真实模型推理。"}
    return json_response(200, {
        "id": "synthetic-completion", "created": 1791619200, "model": "deepseek-flash",
        "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps({"results": [{"id": "1", **{key: val for key, val in classification.items() if key != "confidence"}}]}, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20,
                  "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10},
    })


class FakeHTTP:
    def __init__(self, *replies):
        self.replies = deque(replies)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout, deadline=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body,
                           "timeout": timeout, "deadline": deadline})
        if not self.replies:
            raise AssertionError("UNEXPECTED_FAKE_HTTP_CALL")
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeSMTPFactory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.calls = []

    def __call__(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        if not self.sessions:
            raise AssertionError("UNEXPECTED_FAKE_SMTP_CALL")
        return self.sessions.popleft()


class NoEnvironment:
    def __getitem__(self, key):
        raise AssertionError("ENVIRONMENT_ACCESS_FORBIDDEN")

    get = __getitem__

    def __iter__(self):
        raise AssertionError("ENVIRONMENT_ACCESS_FORBIDDEN")


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.directory = TEST_ROOT / ("assembly-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.now = NOW
        self.ticks = 0.0
        self.store = StateStore(self.directory / "live-state.json")
        self.config = Config(state_path=self.store.path, target_user_id="12345")
        raw = synthetic_authorization(self.store.path, now=NOW,
                                      ledger_path=self.directory / "batch-ledger.json")
        raw.update(expires_at=(NOW + timedelta(hours=24)).isoformat(),
                   source_contract_evidence_id="synthetic-offline-contract",
                   sender="sender@example.invalid")
        raw["target"]["user_id"] = "12345"
        raw["deepseek"].update(model="deepseek-flash", price_snapshot_id=PRICE_SNAPSHOT_ID,
                               max_requests=6, amount="0.01")
        raw["apify"].update(max_run_starts=1, max_run_polls=3,
                            max_dataset_reads=3, max_http_attempts=7)
        self.authorization = BatchAuthorization.from_dict(raw, now=NOW)
        self.ledger = BatchLedger(self.directory / "batch-ledger.json", self.authorization,
                                  clock=lambda: self.now)
        self.ledger.initialize()
        self.batch_sequence = 1
        entry = replace(identity_contract(), reviewed_at=NOW.isoformat(),
                        expires_at=(NOW + timedelta(days=7)).isoformat())
        self.source_catalog = {entry.contract_id: entry}
        # Synthetic reviewed capability for these artificial short requests;
        # this constant does not establish any real tokenizer/framing proof.
        proof = TokenBoundProof("tokenproof-synthetic-assembly", "deepseek-flash",
                                PRICE_VERIFIED_AT, PRICE_VERIFIED_AT + timedelta(hours=24),
                                lambda _payload: 1000)
        self.token_proofs = {proof.model: proof}
        from monitor.validation import ApifyCostProof
        cost = ApifyCostProof("synthetic-apify-bound", entry.contract_id, entry.build_id,
                              NOW, NOW + timedelta(hours=24),
                              ("https://docs.apify.com/api/v2/actors-runs-post",), "b" * 64,
                              Decimal("0.01"), Decimal("0.001"), Decimal("0.002"))
        self.apify_cost_proofs = {cost.proof_id: cost}
        self.apify = FakeHTTP()
        self.deepseek = FakeHTTP()
        self.smtp = FakeSMTPFactory()
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL",
                       "monitor.http_transport.StdlibHTTPTransport.request"):
            guard = patch(target, side_effect=AssertionError("REAL_EXTERNAL_CALL_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        guard = patch("monitor.smtp_transport.ssl.create_default_context", return_value=self.context)
        guard.start()
        self.addCleanup(guard.stop)

    def cleanup(self):
        path, intended = self.directory.resolve(), TEST_ROOT.resolve()
        if path == intended or path.parent != intended:
            raise RuntimeError("Refusing cleanup outside isolated test root")
        shutil.rmtree(path)

    def prepared(self, **changes):
        # These are synthetic acceptance facts for this offline test only.
        values = dict(
            config=self.config, credentials=CREDENTIALS,
            apify_policy=ApifyPolicy(approved=True, available_balance_usd="1",
                max_total_charge_usd="0.001", minimum_charge_cap_usd="0.001",
                per_result_usd="0.0003", pricing_verified=True, source_contract_verified=True),
            deepseek_credential_use_authorized=True, deepseek_test_budget_approved=True,
            deepseek_test_budget_usd="0.01",  # Arbitrary synthetic fact; no actual budget approval.
            smtp_host="smtp.example.invalid", smtp_sender="sender@example.invalid",
            recipient_authorized=True, sender_authorized=True, send_authorized=True,
            offline_state_paths=(self.directory / "offline-state.json", self.directory / "demo-state.json"),
            state_inspected=True, state_paths_verified=True,
            authorization=self.authorization, ledger=self.ledger,
            source_catalog=self.source_catalog, token_proofs=self.token_proofs,
            apify_cost_proofs=self.apify_cost_proofs, checked_at=self.now,
        )
        values.update(changes)
        return PreparedIntegration(**values)

    def assemble(self, plan=None, **changes):
        values = dict(store=self.store, apify_transport=self.apify, deepseek_transport=self.deepseek,
                      smtp_factory=self.smtp, clock=lambda: self.now, sleep=self.sleep,
                      monotonic=lambda: self.ticks)
        values.update(changes)
        return assemble_monitor(self.prepared() if plan is None else plan, **values)

    def sleep(self, seconds):
        self.ticks += seconds
        self.now += timedelta(seconds=seconds)

    def next_explicit_synthetic_batch(self):
        """A distinct artificial approval, not an automatic production refill.

        Every batch retains the one-run envelope and shares the same cumulative
        ledger. Real callers cannot obtain another approval by invoking this
        test-only helper.
        """
        self.batch_sequence += 1
        raw = self.authorization.to_dict()
        raw.update(approval_id=f"synthetic-approval-{self.batch_sequence}",
                   batch_id=f"synthetic-batch-{self.batch_sequence}",
                   approved_at=self.now.isoformat())
        self.authorization = BatchAuthorization.from_dict(raw, now=self.now)
        self.ledger = BatchLedger(self.ledger.path, self.authorization, clock=lambda: self.now)
        self.ledger.initialize()

    def seed_synthetic_baseline(self):
        state = initial_state("thsottiaux", execution_mode="LIVE", target_user_id="12345")
        state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        self.store.save(state)

    def assert_blocked_before_services(self, plan, expected, **injections):
        snapshot = deepcopy(plan.existing_state)
        with patch("monitor.validation.ApifyFetcher") as apify_class, \
                patch("monitor.validation.DeepSeekClassifier") as deepseek_class, \
                patch("monitor.validation.SMTPNotifier") as smtp_class:
            with self.assertRaises(PreflightError) as caught:
                self.assemble(plan, **injections)
            self.assertIn(expected, caught.exception.report.issue_codes)
            for service_class in (apify_class, deepseek_class, smtp_class):
                service_class.assert_not_called()
        self.assertEqual(plan.existing_state, snapshot)
        self.assertEqual(self.apify.calls, [])
        self.assertEqual(self.deepseek.calls, [])
        self.assertEqual(self.smtp.calls, [])
        self.assertFalse(self.store.path.exists())

    def test_default_preparation_refuses_without_service_construction_or_secret_repr(self):
        plan = PreparedIntegration(self.config)
        report = preflight(plan)
        self.assertFalse(report.ok)
        for code in ("APIFY_BUDGET_NOT_APPROVED", "SMTP_SEND_APPROVAL_REQUIRED", "STATE_INSPECTION_REQUIRED",
                     "DEEPSEEK_CREDENTIAL_USE_APPROVAL_REQUIRED", "DEEPSEEK_TEST_BUDGET_APPROVAL_REQUIRED",
                     "DEEPSEEK_TEST_BUDGET_AMOUNT_INVALID"):
            self.assertIn(code, report.issue_codes)
        self.assert_blocked_before_services(plan, "APIFY_BUDGET_NOT_APPROVED")
        for secret in (CREDENTIALS.apify_token, CREDENTIALS.deepseek_api_key,
                       CREDENTIALS.smtp_user, CREDENTIALS.smtp_password):
            self.assertNotIn(secret, repr(CREDENTIALS))
            self.assertNotIn(secret, repr(self.prepared()))
            self.assertNotIn(secret, repr(report))

    def test_scope_recipient_sender_and_explicit_id_gates(self):
        cases = (
            (replace(self.config, target_username="someoneelse"), {}, "TARGET_NOT_AUTHORIZED"),
            (replace(self.config, target_user_id=None), {}, "TARGET_ID_REQUIRED"),
            (replace(self.config, fetch_limit=20), {}, "FETCH_LIMIT_MUST_BE_THREE"),
            (replace(self.config, fetch_limit=True), {}, "FETCH_LIMIT_MUST_BE_THREE"),
            (replace(self.config, schedule_timezone="UTC"), {}, "FIXED_SHANGHAI_SCHEDULE_REQUIRED"),
            (replace(self.config, schedule_times=("08:00", "13:00")), {}, "FIXED_SHANGHAI_SCHEDULE_REQUIRED"),
            (replace(self.config, email_to="attacker@example.com"), {}, "RECIPIENT_NOT_AUTHORIZED"),
            (replace(self.config, email_to=AUTHORIZED_RECIPIENT + ",other@example.com"), {}, "RECIPIENT_NOT_AUTHORIZED"),
            (replace(self.config, email_to=AUTHORIZED_RECIPIENT + "\r\nBcc:other@example.com"), {}, "RECIPIENT_NOT_AUTHORIZED"),
            (self.config, {"recipient_authorized": False}, "RECIPIENT_APPROVAL_REQUIRED"),
            (self.config, {"sender_authorized": False}, "SMTP_SENDER_APPROVAL_REQUIRED"),
            (self.config, {"send_authorized": False}, "SMTP_SEND_APPROVAL_REQUIRED"),
            (self.config, {"smtp_sender": "bad\r\nBcc:other@example.com"}, "SMTP_CONFIG_INVALID"),
        )
        for config, changes, code in cases:
            with self.subTest(code=code):
                self.assert_blocked_before_services(self.prepared(config=config, **changes), code)
        for field in ("include_replies", "exclude_retweets"):
            config = deepcopy(self.config)
            object.__setattr__(config, field, False)
            self.assert_blocked_before_services(self.prepared(config=config), "SOURCE_SCOPE_INVALID")

    def test_all_apify_budget_and_source_gates_fail_closed(self):
        base = self.prepared().apify_policy
        cases = (
            ({"approved": False}, "APIFY_BUDGET_NOT_APPROVED"),
            ({"pricing_verified": False}, "APIFY_BUDGET_NOT_APPROVED"),
            ({"source_contract_verified": False}, "APIFY_SOURCE_CONTRACT_UNVERIFIED"),
            ({"available_balance_usd": None}, "APIFY_BUDGET_FACTS_UNKNOWN"),
            ({"max_total_charge_usd": None}, "APIFY_BUDGET_FACTS_UNKNOWN"),
            ({"minimum_charge_cap_usd": None}, "APIFY_BUDGET_FACTS_UNKNOWN"),
            ({"per_result_usd": None}, "APIFY_BUDGET_FACTS_UNKNOWN"),
            ({"max_total_charge_usd": "0.0008"}, "APIFY_APPROVED_CAP_INSUFFICIENT"),
            ({"minimum_charge_cap_usd": "0.002"}, "APIFY_APPROVED_CAP_INSUFFICIENT"),
            ({"available_balance_usd": "0.0001"}, "APIFY_BALANCE_INSUFFICIENT"),
            ({"per_result_usd": float("nan")}, "APIFY_BUDGET_FACTS_UNKNOWN"),
        )
        for changes, code in cases:
            with self.subTest(fields=list(changes)):
                self.assert_blocked_before_services(self.prepared(apify_policy=replace(base, **changes)), code)

    def test_changed_retry_policy_cannot_reduce_uncertainty_cooldown_or_expand_budget(self):
        for field, values in (
                ("unknown_cooldown_seconds", (0, 86399, True)),
                ("failure_cooldown_seconds", (0, 1, True)),
                ("retry_attempts", (0, 9, True)),
                ("retry_delays", ((), (0, 0), [5, 15])),
                ("classification_retry_budget_seconds", (-1, 600, True, float("inf")))):
            for value in values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_blocked_before_services(
                        self.prepared(config=replace(self.config, **{field: value})), "RETRY_POLICY_INVALID")

    def test_deepseek_authority_is_not_inferred_from_dummy_key_or_other_service_approvals(self):
        plan = self.prepared(deepseek_credential_use_authorized=False,
                             deepseek_test_budget_approved=False, deepseek_test_budget_usd=None)
        report = preflight(plan)
        for code in ("DEEPSEEK_CREDENTIAL_USE_APPROVAL_REQUIRED", "DEEPSEEK_TEST_BUDGET_APPROVAL_REQUIRED",
                     "DEEPSEEK_TEST_BUDGET_AMOUNT_INVALID"):
            self.assertIn(code, report.issue_codes)
        self.assert_blocked_before_services(plan, "DEEPSEEK_CREDENTIAL_USE_APPROVAL_REQUIRED")
        for field, code in (
                ("deepseek_credential_use_authorized", "DEEPSEEK_CREDENTIAL_USE_APPROVAL_REQUIRED"),
                ("deepseek_test_budget_approved", "DEEPSEEK_TEST_BUDGET_APPROVAL_REQUIRED")):
            for value in (False, 1, "true"):
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assert_blocked_before_services(self.prepared(**{field: value}), code)

    def test_deepseek_declared_test_amount_is_positive_finite_and_locally_bounded(self):
        for value in (None, 0, -1, True, float("nan"), float("inf"), "NaN", "Infinity", "bad amount",
                      Decimal("1.000001"), "9999999999", "1" * 129, object()):
            with self.subTest(value_type=type(value).__name__):
                self.assert_blocked_before_services(self.prepared(deepseek_test_budget_usd=value),
                                                   "DEEPSEEK_TEST_BUDGET_AMOUNT_INVALID")
        for value in (Decimal("0.01"), "0.01", 1, 1.0):
            with self.subTest(valid_value_type=type(value).__name__):
                report = preflight(self.prepared(deepseek_test_budget_usd=value))
                if Decimal(str(value)) == Decimal("0.01"):
                    self.assertTrue(report.ok)
                else:
                    self.assertIn("DEEPSEEK_TEST_BUDGET_BINDING_INVALID", report.issue_codes)

    def test_preflight_is_pure_and_assembly_does_not_touch_io(self):
        plan = self.prepared()
        before = sorted(path.name for path in self.directory.iterdir())
        with patch.object(os, "environ", NoEnvironment()), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("FILE_READ_FORBIDDEN")), \
                patch.object(Path, "open", side_effect=AssertionError("FILE_OPEN_FORBIDDEN")), \
                patch.object(Path, "stat", side_effect=AssertionError("FILE_STAT_FORBIDDEN")), \
                patch.object(Path, "resolve", side_effect=AssertionError("PATH_RESOLUTION_FORBIDDEN")):
            self.assertTrue(preflight(plan).ok)
            monitor = self.assemble(plan)
        self.assertIsInstance(monitor.fetcher, ApifyFetcher)
        self.assertIsInstance(monitor.classifier, DeepSeekClassifier)
        self.assertIsInstance(monitor.notifier.notifier, SMTPNotifier)
        self.assertEqual(monitor.config.execution_mode, "LIVE")
        self.assertEqual(self.config.execution_mode, "OFFLINE")
        self.assertEqual(self.apify.calls, [])
        self.assertEqual(self.deepseek.calls, [])
        self.assertEqual(self.smtp.calls, [])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), before)

    def test_state_paths_are_absolute_separate_and_store_matches(self):
        cases = (
            (self.prepared(config=replace(self.config, state_path=Path("relative.json"))), "LIVE_STATE_ABSOLUTE_PATH_REQUIRED"),
            (self.prepared(offline_state_paths=()), "OFFLINE_DEMO_PATHS_REQUIRED"),
            (self.prepared(offline_state_paths=(Path("offline.json"), Path("demo.json"))), "OFFLINE_DEMO_PATHS_INVALID"),
            (self.prepared(offline_state_paths=(self.store.path, self.directory / "demo.json")), "LIVE_OFFLINE_STATE_PATH_CONFLICT"),
            (self.prepared(offline_state_paths=(Path(str(self.store.path) + ".bak"), self.directory / "demo.json")), "LIVE_OFFLINE_STATE_PATH_CONFLICT"),
            (self.prepared(state_paths_verified=False), "STATE_PATH_ALIAS_REVIEW_REQUIRED"),
            (self.prepared(state_inspected=False), "STATE_INSPECTION_REQUIRED"),
        )
        for plan, code in cases:
            with self.subTest(code=code):
                self.assert_blocked_before_services(plan, code)
        other_store = StateStore(self.directory / "other-live.json")
        self.assert_blocked_before_services(self.prepared(), "STATE_STORE_PATH_MISMATCH", store=other_store)

    def test_offline_unknown_recovered_and_invalid_state_never_become_live(self):
        offline = initial_state("thsottiaux")
        unknown = deepcopy(offline)
        unknown.pop("execution_mode")
        initialized = deepcopy(offline)
        initialized.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        for state in (offline, unknown, initialized):
            self.assert_blocked_before_services(self.prepared(existing_state=state), "STATE_ORIGIN_UNSAFE")
        live = initial_state("thsottiaux", execution_mode="LIVE", target_user_id="12345")
        self.assertTrue(preflight(self.prepared(existing_state=live)).ok)
        recovered = dict(live, recovery_required=True)
        self.assert_blocked_before_services(self.prepared(existing_state=recovered), "STATE_RECOVERY_REVIEW_REQUIRED")
        self.assert_blocked_before_services(self.prepared(existing_state={"execution_mode": "LIVE"}), "EXISTING_STATE_INVALID")

    def test_missing_injected_boundaries_and_invalid_credentials_are_redacted(self):
        for injections, code in (
                ({"apify_transport": None}, "APIFY_TRANSPORT_REQUIRED"),
                ({"deepseek_transport": None}, "DEEPSEEK_DEADLINE_TRANSPORT_REQUIRED"),
                ({"smtp_factory": None}, "SMTP_FACTORY_REQUIRED")):
            self.assert_blocked_before_services(self.prepared(), code, **injections)

        class LegacyHTTP:
            def request(self, method, url, *, headers, body, timeout):
                raise AssertionError("LEGACY_HTTP_MUST_NOT_BE_CALLED")
        self.assert_blocked_before_services(self.prepared(), "DEEPSEEK_DEADLINE_TRANSPORT_REQUIRED",
                                           deepseek_transport=LegacyHTTP())
        secret = "SYNTHETIC_SECRET_MUST_NOT_APPEAR\r\n"
        plan = self.prepared(credentials=replace(CREDENTIALS, apify_token=secret, deepseek_api_key=secret,
                                                smtp_password=secret))
        report = preflight(plan)
        self.assertFalse(report.ok)
        self.assertNotIn("SYNTHETIC_SECRET", repr(report))
        with self.assertRaises(PreflightError) as caught:
            self.assemble(plan)
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))

    def test_actual_adapters_baseline_latest_three_and_deduplicate(self):
        self.apify.replies.extend(source_cycle([100, 99, 98]))
        self.apify.replies.extend(source_cycle([103, 102, 101]))
        self.apify.replies.extend(source_cycle([103, 102, 101]))
        self.deepseek.replies.extend(completion(label) for label in ("POST_RESET", "IRRELEVANT", "PRE_RESET"))
        sessions = [FakeSession(), FakeSession()]
        self.smtp.sessions.extend(sessions)
        monitor = self.assemble()
        baseline = monitor.run()
        self.assertEqual((baseline.status, baseline.classified, baseline.sent), ("OK", 0, 0))
        self.assertEqual(self.deepseek.calls, [])
        self.assertEqual(self.smtp.calls, [])
        self.now += timedelta(hours=2)
        self.next_explicit_synthetic_batch()
        monitor = self.assemble(self.prepared(existing_state=self.store.load()))
        latest = monitor.run()
        self.assertEqual((latest.status, latest.ingested, latest.classified, latest.sent), ("OK", 3, 3, 2))
        state = self.store.load()
        self.assertEqual(state["execution_mode"], "LIVE")
        self.assertEqual(set(state["processed_ids"]), {"101", "102", "103"})
        self.assertEqual(len(state["notified_ids"]), 2)
        self.assertIsNone(state["active_fetch"])
        self.assertEqual(state["last_completed_fetch"]["phase"], "READY")
        self.now += timedelta(hours=2)
        self.next_explicit_synthetic_batch()
        monitor = self.assemble(self.prepared(existing_state=self.store.load()))
        duplicate = monitor.run()
        self.assertEqual((duplicate.status, duplicate.ingested, duplicate.classified, duplicate.sent), ("OK", 0, 0, 0))
        self.assertEqual(len(self.deepseek.calls), 3)
        self.assertEqual(len(self.smtp.calls), 2)
        posts = [call for call in self.apify.calls if call["method"] == "POST"]
        self.assertEqual(len(posts), 3)
        for call in posts:
            self.assertEqual(json.loads(call["body"]), {"profiles": ["thsottiaux"], "maxResults": 3})
            self.assertEqual(parse_qs(urlsplit(call["url"]).query)["maxTotalChargeUsd"], ["0.001"])
        for call in self.deepseek.calls:
            self.assertIsNotNone(call["deadline"])
            self.assertEqual(json.loads(call["body"])["response_format"], {"type": "json_object"})
        batches = json.loads(self.ledger.path.read_text(encoding="utf-8"))["batches"]
        self.assertEqual(len(batches), 3)
        for batch in batches.values():
            self.assertEqual(batch["counts"]["apify_start"], 1)
            self.assertEqual(batch["authorization"]["apify"]["max_run_starts"], 1)
        for operation, expected in (("apify_start", 3), ("apify_poll", 3), ("apify_dataset", 3),
                                    ("deepseek_request", 3), ("smtp_attempt", 2)):
            self.assertEqual(sum(batch["counts"][operation] for batch in batches.values()), expected)
        for session in sessions:
            message = BytesParser(policy=email_policy.default).parsebytes(session.payloads[0][:-3])
            self.assertEqual(message["To"], AUTHORIZED_RECIPIENT)
            self.assertTrue(str(message["Subject"]).startswith("【ResetMonitor 测试】"))
            self.assertIn("完整原文", message.get_content())
            self.assertEqual(session.names().count("send"), 1)

    def test_known_paid_run_failure_resumes_by_get_then_classifies_and_sends(self):
        self.apify.replies.extend(source_cycle([100, 99, 98]))
        monitor = self.assemble()
        self.assertEqual(monitor.run().status, "OK")
        cycle = source_cycle([103, 102, 101])
        self.apify.replies.extend((cycle[0], json_response(503, {"error": "synthetic unavailable"})))
        self.now += timedelta(hours=2)
        self.next_explicit_synthetic_batch()
        monitor = self.assemble(self.prepared(existing_state=self.store.load()))
        failed = monitor.run()
        self.assertEqual((failed.status, failed.classified, failed.sent), ("DEGRADED", 0, 0))
        retained = self.store.load()
        self.assertEqual(retained["active_fetch"]["phase"], "RUNNING")
        self.assertEqual(retained["active_fetch"]["run_id"], RUN_ID)
        calls_before = len(self.apify.calls)
        self.apify.replies.extend(cycle[1:])
        self.deepseek.replies.extend(completion(label) for label in ("IRRELEVANT", "POSSIBLE_RESET", "IRRELEVANT"))
        self.smtp.sessions.append(FakeSession())
        resumed = self.assemble(self.prepared(existing_state=retained))
        self.now += timedelta(hours=2)
        result = resumed.run()
        self.assertEqual((result.status, result.classified, result.sent), ("OK", 3, 1))
        self.assertEqual([call["method"] for call in self.apify.calls[calls_before:]], ["GET", "GET"])
        self.assertIsNone(self.store.load()["active_fetch"])

    def test_smtp_uncertainty_survives_reassembly_but_expired_batch_blocks_resend(self):
        for ids in ([100, 99, 98], [103, 102, 101]):
            self.apify.replies.extend(source_cycle(ids))
        self.deepseek.replies.extend(completion(label) for label in ("PRE_RESET", "IRRELEVANT", "IRRELEVANT"))
        uncertain, accepted = FakeSession(getreply=OSError("synthetic disconnect")), FakeSession()
        self.smtp.sessions.extend((uncertain, accepted))
        monitor = self.assemble()
        self.assertEqual(monitor.run().status, "OK")
        self.now += timedelta(hours=2)
        self.next_explicit_synthetic_batch()
        monitor = self.assemble(self.prepared(existing_state=self.store.load()))
        self.assertEqual(monitor.run().status, "DEGRADED")
        retained = self.store.load()
        notification = retained["pending_notifications"]["101:PRE_RESET"]
        message_id = notification["message_id"]
        self.assertEqual(notification["status"], "UNKNOWN")
        self.assertEqual(retained["notified_ids"], {})
        resumed = self.assemble(self.prepared(existing_state=retained))
        self.now += timedelta(hours=2)
        self.assertEqual(resumed.run().sent, 0)
        self.assertEqual(len(self.smtp.calls), 1)
        self.now += timedelta(hours=24)
        result = resumed.run()
        self.assertEqual((result.status, result.classified, result.sent), ("DEGRADED", 0, 0))
        saved = self.store.load()["pending_notifications"]["101:PRE_RESET"]
        self.assertEqual(saved["message_id"], message_id)
        self.assertEqual(saved["unknown_retry_used"], 1)
        self.assertEqual(saved["status"], "FAILED")
        self.assertEqual(accepted.payloads, [])
        self.assertEqual(len(self.smtp.calls), 1)
        self.assertEqual(len(self.deepseek.calls), 3)

    def test_deepseek_retry_after_retains_inbox_then_recovers_after_reassembly(self):
        for ids in ([100, 99, 98], [103, 102, 101]):
            self.apify.replies.extend(source_cycle(ids))
        self.deepseek.replies.extend((
            json_response(429, {"error": {"message": "synthetic rate limit"}}, {"Retry-After": "120"}),
            completion("IRRELEVANT"), completion("IRRELEVANT"), completion("PRE_RESET"),
        ))
        self.smtp.sessions.append(FakeSession())
        monitor = self.assemble()
        self.assertEqual(monitor.run().status, "OK")
        self.now += timedelta(hours=2)
        self.next_explicit_synthetic_batch()
        monitor = self.assemble(self.prepared(existing_state=self.store.load()))
        failed = monitor.run()
        self.assertEqual((failed.status, failed.classified, failed.sent), ("DEGRADED", 2, 0))
        retained = self.store.load()
        self.assertEqual(set(retained["inbox"]), {"101"})
        self.assertTrue(retained["inbox"]["101"]["retryable"])
        self.assertEqual(retained["inbox"]["101"]["attempts"], 1)
        self.assertEqual(self.ticks, 0.0)  # 120 seconds cannot fit the 60-second item budget.
        self.assertEqual(len(self.deepseek.calls), 3)
        resumed = self.assemble(self.prepared(existing_state=retained))
        self.now += timedelta(hours=2)
        result = resumed.run()
        self.assertEqual((result.status, result.ingested, result.classified, result.sent), ("DEGRADED", 0, 1, 1))
        self.assertEqual(self.store.load()["inbox"], {})
        self.assertEqual(len(self.deepseek.calls), 4)
        self.assertEqual(len(self.smtp.calls), 1)

    def test_state_replaced_after_preflight_is_still_blocked_before_calls(self):
        monitor = self.assemble()
        self.store.save(initial_state("thsottiaux", execution_mode="OFFLINE"))
        with self.assertRaisesRegex(Exception, "mode|origin"):
            monitor.run()
        self.assertEqual(self.apify.calls, [])
        self.assertEqual(self.deepseek.calls, [])
        self.assertEqual(self.smtp.calls, [])

    def test_legacy_assembly_cannot_bypass_batch_and_reviewed_evidence_gates(self):
        cases = (
            ({"authorization": None}, "VALIDATION_AUTHORIZATION_REQUIRED"),
            ({"ledger": None}, "VALIDATION_LEDGER_REQUIRED"),
            ({"checked_at": None}, "VALIDATION_AUTHORIZATION_INVALID_OR_EXPIRED"),
            ({"source_catalog": None}, "APIFY_SOURCE_CONTRACT_UNVERIFIED"),
            ({"token_proofs": None}, "DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE"),
            ({"apify_cost_proofs": None}, "APIFY_TOTAL_COST_BOUND_UNVERIFIED"),
        )
        for changes, code in cases:
            with self.subTest(fields=tuple(changes)):
                self.assert_blocked_before_services(self.prepared(**changes), code)
        raw = self.authorization.to_dict()
        raw["batch_id"] = "synthetic-other-batch"
        raw["ledger_path"] = str(self.directory / "other-ledger.json")
        other = BatchAuthorization.from_dict(raw, now=self.now)
        other_ledger = BatchLedger(self.directory / "other-ledger.json", other, clock=lambda: self.now)
        self.assert_blocked_before_services(self.prepared(ledger=other_ledger),
                                           "VALIDATION_LEDGER_AUTHORIZATION_MISMATCH")
        raw = self.authorization.to_dict()
        raw["deepseek"].update(currency="CNY", amount="1")
        raw["ledger_path"] = str(self.directory / "cny-ledger.json")
        cny = BatchAuthorization.from_dict(raw, now=self.now)
        cny_ledger = BatchLedger(self.directory / "cny-ledger.json", cny, clock=lambda: self.now)
        self.assert_blocked_before_services(self.prepared(authorization=cny, ledger=cny_ledger,
                                                         deepseek_test_budget_usd="1"),
                                           "DEEPSEEK_TEST_BUDGET_BINDING_INVALID")
        for section, field, limit in (("apify", "max_run_starts", 2), ("apify", "max_run_polls", 4),
                                      ("apify", "max_dataset_reads", 4), ("apify", "max_http_attempts", 8),
                                      ("deepseek", "max_requests", 10), ("smtp", "max_attempts", 4)):
            raw = self.authorization.to_dict()
            raw[section][field] = limit
            broader = BatchAuthorization.from_dict(raw, now=self.now)
            detached = BatchLedger(self.ledger.path, broader, clock=lambda: self.now)
            with self.subTest(section=section, field=field):
                self.assert_blocked_before_services(self.prepared(authorization=broader, ledger=detached),
                                                   "VALIDATION_OPERATION_LIMIT_EXCEEDS_ENVELOPE")

    def test_reassembly_cannot_reset_exhausted_smtp_attempt_ledger(self):
        for kind in ("TWEET", "HEALTH", "TWEET"):
            self.ledger.reserve("smtp_attempt", metadata={"notification_kind": kind})
        self.seed_synthetic_baseline()
        self.apify.replies.extend(source_cycle([103, 102, 101]))
        self.deepseek.replies.extend(completion(label) for label in ("PRE_RESET", "IRRELEVANT", "IRRELEVANT"))
        self.smtp.sessions.append(FakeSession())
        monitor = self.assemble()
        self.now += timedelta(hours=2)
        resumed = self.assemble(self.prepared(existing_state=self.store.load()))
        result = resumed.run()
        self.assertEqual((result.status, result.classified, result.sent), ("DEGRADED", 3, 0))
        self.assertEqual(self.smtp.calls, [])
        ledger = json.loads(self.ledger.path.read_text(encoding="utf-8"))["batches"][self.authorization.batch_id]
        self.assertEqual(ledger["counts"]["smtp_attempt"], 3)
        self.assertEqual(ledger["counts"]["deepseek_request"], 3)
        saved = self.store.load()["pending_notifications"]["101:PRE_RESET"]
        self.assertEqual((saved["status"], saved["retryable"]), ("FAILED", False))


if __name__ == "__main__":
    unittest.main()
