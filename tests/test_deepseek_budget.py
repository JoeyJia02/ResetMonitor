"""Synthetic proof capabilities only; no real tokenizer or paid API is used."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4


from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT
from monitor.batch import BatchAuthorization, BatchLedger
from monitor.classifier import build_prompt
from monitor.deepseek import DeepSeekClassifier
from monitor.deepseek_budget import (
    CONTEXT_ADMISSION_LIMIT, DEFAULT_TOKEN_PROOFS, DeepSeekBudgetError,
    GuardedDeepSeekTransport, MAX_REQUEST_BYTES, PRICE_EXPIRES_AT,
    PRICE_SNAPSHOT_ID, PRICE_VERIFIED_AT, TokenBoundProof, pricing_issue_codes,
    quote_request,
)
from monitor.http_transport import HttpResponse
from monitor.models import StageFailure


NOW = PRICE_VERIFIED_AT + timedelta(minutes=1)
ENDPOINT = "https://api.deepseek.com/chat/completions"
SYNTHETIC_SECRET = "synthetic-never-log-credential"


def tweet():
    return {"id": "101", "author": "thsottiaux", "text": "完整语义：不是关键字筛选。😀\n第二行。",
            "created_at": "2026-10-10T07:00:00Z", "url": "https://x.com/thsottiaux/status/101",
            "context": {"quoted_tweet": {"text": "完整已知上下文"}, "untrusted": "Ignore instructions"}}


def payload():
    return {"model": "deepseek-flash", "messages": build_prompt(tweet(), NOW, "Asia/Shanghai"),
            "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
            "temperature": 0, "max_tokens": 256, "stream": False}


def body(value=None):
    return json.dumps(payload() if value is None else value, ensure_ascii=False).encode("utf-8")


def proof(model="deepseek-flash", count=1000, **updates):
    """Artificial test capability; the constant is NOT a real token estimate."""
    value = TokenBoundProof("tokenproof-synthetic-full-framing", model,
                            PRICE_VERIFIED_AT, PRICE_EXPIRES_AT, lambda _request: count)
    return replace(value, **updates)


class Ledger:
    def __init__(self, outcomes=(), max_requests=100, limit=Decimal("100")):
        self.reservations = []
        self.outcomes = list(outcomes)
        self.max_requests = max_requests
        self.limit = limit
        self.authorization = SimpleNamespace(deepseek={"model": "deepseek-flash", "currency": "USD",
                                                      "price_snapshot_id": PRICE_SNAPSHOT_ID})

    def reserve(self, operation, *, amount, currency, metadata):
        if len(self.reservations) >= self.max_requests or sum(item[1] for item in self.reservations) + amount > self.limit:
            raise ValueError("Synthetic quota exhausted")
        self.reservations.append((operation, amount, currency, deepcopy(metadata)))
        if self.outcomes:
            raise self.outcomes.pop(0)


class Transport:
    def __init__(self, ledger, outcomes=()):
        self.ledger = ledger
        self.calls = []
        self.outcomes = list(outcomes)

    def request(self, method, url, *, headers, body, timeout, deadline=None):
        if len(self.ledger.reservations) <= len(self.calls):
            raise AssertionError("Attempt preceded its reservation")
        self.calls.append((method, url, dict(headers), body, timeout, deadline))
        outcome = self.outcomes.pop(0) if self.outcomes else HttpResponse(200, b"{}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DeepSeekBudgetTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("socket.socket", side_effect=AssertionError("Real sockets forbidden"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def quote(self, value=None, *, currency="USD", selected=None, now=NOW):
        return quote_request(body(value), currency=currency, now=now,
                             token_proofs={"deepseek-flash": proof() if selected is None else selected})

    def wrapper(self, ledger=None, *, clock=lambda: NOW, selected=None, currency="USD", outcomes=()):
        ledger = ledger or Ledger()
        transport = Transport(ledger, outcomes)
        wrapped = GuardedDeepSeekTransport(transport, ledger, currency=currency,
                                           token_proofs={"deepseek-flash": proof() if selected is None else selected},
                                           clock=clock)
        return wrapped, ledger, transport

    def send(self, wrapped, request_body=None):
        return wrapped.request("POST", ENDPOINT,
                               headers={"Authorization": "Bearer " + SYNTHETIC_SECRET},
                               body=body() if request_body is None else request_body,
                               timeout=20, deadline=1234.5)

    def test_production_registry_is_empty_and_default_blocks_before_ledger_and_io(self):
        self.assertEqual(dict(DEFAULT_TOKEN_PROOFS), {})
        with self.assertRaises(TypeError):
            DEFAULT_TOKEN_PROOFS["deepseek-flash"] = proof()
        ledger = Ledger()
        transport = Transport(ledger)
        wrapped = GuardedDeepSeekTransport(transport, ledger, currency="USD", clock=lambda: NOW)
        with self.assertRaises(DeepSeekBudgetError) as caught:
            self.send(wrapped)
        self.assertEqual(caught.exception.code, "DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual((ledger.reservations, transport.calls), ([], []))

    def test_official_peak_uncached_rates_and_currencies_are_independent(self):
        expected = {("deepseek-flash", "USD"): "0.00060720", ("deepseek-flash", "CNY"): "0.00404800",
                    ("deepseek-v4-pro", "USD"): "0.00233376", ("deepseek-v4-pro", "CNY"): "0.01591200"}
        for (model, currency), amount in expected.items():
            value = payload()
            value["model"] = model
            result = quote_request(body(value), currency=currency, now=NOW,
                                   token_proofs={model: proof(model)})
            self.assertEqual(result.reserved_amount, Decimal(amount))
            self.assertEqual((result.currency, result.price_snapshot_id), (currency, PRICE_SNAPSHOT_ID))

    def test_decimal_reservation_rounds_up_without_float_or_cache_discount(self):
        selected = proof("deepseek-v4-pro", count=1)
        value = payload()
        value["model"] = "deepseek-v4-pro"
        result = quote_request(body(value), currency="USD", now=NOW,
                               token_proofs={"deepseek-v4-pro": selected})
        self.assertIsInstance(result.reserved_amount, Decimal)
        self.assertEqual(result.reserved_amount, Decimal("0.00101508"))

    def test_unsupported_currency_and_retired_model_never_infer_fx_or_alias(self):
        for currency in ("cny", "RMB", "EUR", None, 1, True):
            with self.subTest(currency=currency), self.assertRaises(DeepSeekBudgetError):
                self.quote(currency=currency)
        for model in ("deepseek-chat", "deepseek-v4-flash", "DeepSeek-V4.1-Flash", "provider/model"):
            value = payload()
            value["model"] = model
            with self.subTest(model=model), self.assertRaises(DeepSeekBudgetError):
                self.quote(value)

    def test_price_expiry_and_future_clock_refuse_each_attempt(self):
        now = [NOW]
        wrapped, ledger, transport = self.wrapper(clock=lambda: now[0])
        self.send(wrapped)
        for instant in (PRICE_EXPIRES_AT, PRICE_VERIFIED_AT - timedelta(microseconds=1), NOW.replace(tzinfo=None)):
            now[0] = instant
            with self.assertRaises(DeepSeekBudgetError):
                self.send(wrapped)
        self.assertEqual((len(ledger.reservations), len(transport.calls)), (1, 1))

    def test_price_expiring_during_reservation_keeps_spent_amount_and_prevents_wire(self):
        now = [NOW]
        class ExpiringLedger(Ledger):
            def reserve(self, *args, **kwargs):
                super().reserve(*args, **kwargs)
                now[0] = PRICE_EXPIRES_AT
        wrapped, ledger, transport = self.wrapper(ExpiringLedger(), clock=lambda: now[0])
        with self.assertRaises(DeepSeekBudgetError) as caught:
            self.send(wrapped)
        self.assertEqual(caught.exception.code, "DEEPSEEK_PRICE_SNAPSHOT_STALE")
        self.assertEqual((len(ledger.reservations), len(transport.calls)), (1, 0))

    def test_preflight_reports_price_and_token_proof_missing_together(self):
        self.assertEqual(pricing_issue_codes("deepseek-flash", "CNY", now=PRICE_EXPIRES_AT),
                         ("DEEPSEEK_PRICE_SNAPSHOT_STALE", "DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE"))
        self.assertIn("DEEPSEEK_AWARE_PRICE_CLOCK_REQUIRED",
                      pricing_issue_codes("deepseek-flash", "USD", now=None))
        self.assertEqual(pricing_issue_codes("deepseek-flash", "USD", now=NOW,
                                            token_proofs={"deepseek-flash": proof()}), ())

    def test_unknown_expired_mismatched_and_overlong_proofs_fail_closed(self):
        for selected in (object(), proof(model="deepseek-v4-pro"), proof(expires_at=NOW),
                         proof(verified_at=NOW + timedelta(seconds=1)),
                         proof(expires_at=PRICE_EXPIRES_AT + timedelta(seconds=1)),
                         proof(proof_id="arbitrary-unreviewed"), proof(bounder=None)):
            with self.subTest(kind=type(selected).__name__), self.assertRaises(DeepSeekBudgetError):
                self.quote(selected=selected)

    def test_proof_has_complete_prompt_context_and_format_without_mutating_wire(self):
        captured = []
        def reviewed_synthetic_bounder(value):
            captured.append(deepcopy(value))
            value["messages"][1]["content"] = "mutated synthetic copy"
            return 1234
        wrapped, ledger, transport = self.wrapper(selected=proof(bounder=reviewed_synthetic_bounder))
        original = body()
        self.send(wrapped, original)
        self.assertEqual(captured, [json.loads(original)])
        self.assertEqual(transport.calls[0][3], original)
        parsed = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(parsed["tweets"][0]["text"], tweet()["text"])
        self.assertEqual(parsed["tweets"][0]["quoted_tweet"], tweet()["context"]["quoted_tweet"])
        self.assertEqual(ledger.reservations[0][3]["input_tokens_upper_bound"], 1234)

    def test_oversized_full_prompt_rejects_without_truncation_or_reservation(self):
        wrapped, ledger, transport = self.wrapper()
        value = payload()
        value["messages"][1]["content"] += "😀" * MAX_REQUEST_BYTES
        original = body(value)
        with self.assertRaises(DeepSeekBudgetError) as caught:
            self.send(wrapped, original)
        self.assertEqual(caught.exception.code, "DEEPSEEK_FULL_REQUEST_INVALID_OR_TOO_LARGE")
        self.assertEqual((ledger.reservations, transport.calls), ([], []))
        self.assertEqual(body(value), original)

    def test_invalid_or_overcontext_bound_rejects_before_reservation(self):
        for count in (True, 0, -1, 1.5, "1000", None, CONTEXT_ADMISSION_LIMIT - 255, 10**100):
            with self.subTest(count_type=type(count).__name__), self.assertRaises(DeepSeekBudgetError):
                self.quote(selected=proof(count=count))

    def test_token_bound_failure_redacts_error_and_never_spends(self):
        def fail(_value):
            raise RuntimeError(SYNTHETIC_SECRET)
        wrapped, ledger, transport = self.wrapper(selected=proof(bounder=fail))
        with self.assertRaises(DeepSeekBudgetError) as caught:
            self.send(wrapped)
        self.assertEqual(str(caught.exception), "DEEPSEEK_TOKEN_BOUND_FAILED")
        self.assertNotIn(SYNTHETIC_SECRET, repr(caught.exception))
        self.assertEqual((ledger.reservations, transport.calls), ([], []))

    def test_exact_request_schema_rejects_unbounded_features_before_reservation(self):
        changes = ({"max_tokens": 257}, {"max_tokens": True}, {"stream": True}, {"thinking": {"type": "enabled"}},
                   {"response_format": {"type": "text"}}, {"tools": []}, {"temperature": True},
                   {"messages": [{"role": "user", "content": "Missing system"}]})
        for update in changes:
            wrapped, ledger, transport = self.wrapper()
            value = payload()
            value.update(update)
            with self.subTest(update=list(update)), self.assertRaises(DeepSeekBudgetError):
                self.send(wrapped, body(value))
            self.assertEqual((ledger.reservations, transport.calls), ([], []))

    def test_duplicate_nonfinite_malformed_or_nonbytes_request_is_rejected(self):
        for request_body in (b'{"model":"deepseek-flash","model":"deepseek-v4-pro"}', b"NaN", b"\xff", b"{}", "not bytes", None):
            with self.subTest(body_type=type(request_body).__name__), self.assertRaises(DeepSeekBudgetError):
                quote_request(request_body, currency="USD", now=NOW,
                              token_proofs={"deepseek-flash": proof()})

    def test_each_failed_retry_and_missing_usage_response_keeps_full_reservation(self):
        wrapped, ledger, transport = self.wrapper(outcomes=(StageFailure(), HttpResponse(503, b"{}"), HttpResponse(200, b"{}")))
        with self.assertRaises(StageFailure):
            self.send(wrapped)
        self.send(wrapped)
        self.send(wrapped)
        self.assertEqual((len(ledger.reservations), len(transport.calls)), (3, 3))
        self.assertEqual(sum(item[1] for item in ledger.reservations), Decimal("0.00182160"))
        self.assertEqual(transport.calls[0][-1], 1234.5)

    def test_exhausted_request_or_currency_amount_limit_blocks_io(self):
        for ledger in (Ledger(max_requests=1), Ledger(limit=Decimal("0.0007"))):
            wrapped, ledger, transport = self.wrapper(ledger)
            self.send(wrapped)
            with self.assertRaises(DeepSeekBudgetError) as caught:
                self.send(wrapped)
            self.assertEqual(caught.exception.code, "DEEPSEEK_LEDGER_RESERVATION_REJECTED")
            self.assertEqual((len(ledger.reservations), len(transport.calls)), (1, 1))

    def test_reservation_commit_error_or_crash_never_calls_network(self):
        for error in (RuntimeError(SYNTHETIC_SECRET), KeyboardInterrupt()):
            wrapped, ledger, transport = self.wrapper(Ledger(outcomes=(error,)))
            with self.assertRaises(DeepSeekBudgetError if isinstance(error, Exception) else KeyboardInterrupt) as caught:
                self.send(wrapped)
            self.assertEqual((len(ledger.reservations), len(transport.calls)), (1, 0))
            self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception))

    def test_only_fixed_endpoint_and_metadata_leave_guard(self):
        wrapped, ledger, transport = self.wrapper()
        for method, url in (("GET", ENDPOINT), ("POST", ENDPOINT + "?secret=" + SYNTHETIC_SECRET),
                            ("POST", "https://attacker.invalid")):
            with self.assertRaises(DeepSeekBudgetError):
                wrapped.request(method, url, headers={}, body=body(), timeout=20)
        self.send(wrapped)
        evidence = repr(ledger.reservations)
        self.assertNotIn(SYNTHETIC_SECRET, evidence)
        self.assertNotIn(tweet()["text"], evidence)
        self.assertEqual(set(ledger.reservations[0][3]), {"price_snapshot_id", "model", "token_proof_id",
                                                        "input_tokens_upper_bound", "output_tokens_upper_bound"})
        self.assertEqual(len(transport.calls), 1)

    def test_actual_persistent_ledger_guard_reserves_failures_and_restart_without_reset(self):
        workspace = Path(__file__).resolve().parents[1]
        temporary_root = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or workspace / "runtime" / "test-tmp").resolve()
        temporary = temporary_root / ("deepseek-budget-" + uuid4().hex)
        temporary.mkdir(parents=True)
        def cleanup():
            if temporary.resolve().parent != temporary_root:
                raise AssertionError("Temporary cleanup escaped its isolated root")
            shutil.rmtree(temporary)
        self.addCleanup(cleanup)
        raw = {
            "schema_version": 1, "approval_id": "synthetic-approval", "batch_id": "synthetic-batch",
            "approved": True, "approved_at": PRICE_VERIFIED_AT.isoformat(), "expires_at": PRICE_EXPIRES_AT.isoformat(),
            "target": {"username": "thsottiaux", "user_id": "123456"},
            "state_path": str(temporary / "synthetic-live-state.json"),
            "ledger_path": str(temporary / "synthetic-ledger.json"),
            "recipient": LOCAL_RECIPIENT, "sender": "synthetic@example.invalid",
            "source_contract_evidence_id": "synthetic-source-evidence",
            "deepseek": {"credential_use_authorized": True, "budget_approved": True, "model": "deepseek-flash",
                         "currency": "CNY", "price_snapshot_id": PRICE_SNAPSHOT_ID, "max_requests": 2, "amount": "1"},
            "apify": {"credential_use_authorized": True, "budget_approved": True, "monetary_bound_verified": True,
                      "bound_evidence_id": "synthetic-fee-evidence", "currency": "USD", "max_run_starts": 1,
                      "max_run_polls": 2, "max_dataset_reads": 1, "max_http_attempts": 4,
                      "amount": "1", "start_max_cost": "0.01", "poll_max_cost": "0.01", "dataset_max_cost": "0.01"},
            "smtp": {"sender_authorized": True, "recipient_authorized": True, "send_authorized": True, "max_attempts": 1},
        }
        authorization = BatchAuthorization.from_dict(raw, now=NOW)
        ledger_path = temporary / "synthetic-ledger.json"
        ledger = BatchLedger(ledger_path, authorization, clock=lambda: NOW)
        ledger.initialize()
        attempts = []
        class ActualLedgerBoundary:
            def request(_self, method, url, *, headers, body, timeout, deadline=None):
                entry = ledger.validate_ready()
                self.assertEqual(entry["counts"]["deepseek_request"], len(attempts) + 1)
                self.assertEqual(entry["receipts"][-1]["metadata"]["output_tokens_upper_bound"], 256)
                attempts.append(body)
                if len(attempts) == 1:
                    raise StageFailure()
                return HttpResponse(200, b"{}")
        transport = ActualLedgerBoundary()
        # A reviewed proof for Pro never grants permission to use a Flash
        # approval for that different, more expensive model.
        mismatched = GuardedDeepSeekTransport(transport, ledger, currency="CNY",
                                              token_proofs={"deepseek-flash": proof(), "deepseek-v4-pro": proof("deepseek-v4-pro")},
                                              clock=lambda: NOW)
        different = payload()
        different["model"] = "deepseek-v4-pro"
        with self.assertRaises(DeepSeekBudgetError) as caught:
            self.send(mismatched, body(different))
        self.assertEqual(caught.exception.code, "DEEPSEEK_AUTHORIZED_MODEL_MISMATCH")
        self.assertEqual(ledger.validate_ready()["counts"]["deepseek_request"], 0)
        self.assertEqual(attempts, [])
        wrapped = GuardedDeepSeekTransport(transport, ledger, currency="CNY",
                                           token_proofs={"deepseek-flash": proof()}, clock=lambda: NOW)
        with self.assertRaises(StageFailure):
            self.send(wrapped)
        ledger = BatchLedger(ledger_path, authorization, clock=lambda: NOW)
        ledger.initialize()  # Idempotent reopening never resets a spent attempt.
        wrapped = GuardedDeepSeekTransport(transport, ledger, currency="CNY",
                                           token_proofs={"deepseek-flash": proof()}, clock=lambda: NOW)
        self.send(wrapped)
        with self.assertRaises(DeepSeekBudgetError):
            self.send(wrapped)
        entry = ledger.validate_ready()
        self.assertEqual(entry["counts"]["deepseek_request"], 2)
        self.assertEqual(entry["reserved"]["deepseek"], {"currency": "CNY", "amount": "0.00809600"})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(entry["receipts"][0]["metadata"]["model"], "deepseek-flash")
        self.assertEqual(entry["receipts"][0]["metadata"]["token_proof_id"], proof().proof_id)
        self.assertNotIn(SYNTHETIC_SECRET, ledger_path.read_text(encoding="utf-8"))
        reverse_raw = deepcopy(raw)
        reverse_raw.update(approval_id="synthetic-pro-approval", batch_id="synthetic-pro-batch")
        reverse_raw["ledger_path"] = str(temporary / "synthetic-pro-ledger.json")
        reverse_raw["deepseek"]["model"] = "deepseek-v4-pro"
        reverse = BatchAuthorization.from_dict(reverse_raw, now=NOW)
        reverse_ledger = BatchLedger(temporary / "synthetic-pro-ledger.json", reverse, clock=lambda: NOW)
        reverse_ledger.initialize()
        reverse_wrapped = GuardedDeepSeekTransport(transport, reverse_ledger, currency="CNY",
                                                  token_proofs={"deepseek-flash": proof(), "deepseek-v4-pro": proof("deepseek-v4-pro")},
                                                  clock=lambda: NOW)
        with self.assertRaises(DeepSeekBudgetError) as caught:
            self.send(reverse_wrapped)
        self.assertEqual(caught.exception.code, "DEEPSEEK_AUTHORIZED_MODEL_MISMATCH")
        self.assertEqual(reverse_ledger.validate_ready()["counts"]["deepseek_request"], 0)
        self.assertEqual(len(attempts), 2)

    def test_authorized_currency_and_price_snapshot_must_match_before_reservation(self):
        for field, value, expected in (("currency", "CNY", "DEEPSEEK_AUTHORIZED_CURRENCY_MISMATCH"),
                                       ("price_snapshot_id", "different-price", "DEEPSEEK_AUTHORIZED_PRICE_SNAPSHOT_MISMATCH")):
            wrapped, ledger, transport = self.wrapper()
            ledger.authorization.deepseek[field] = value
            with self.assertRaises(DeepSeekBudgetError) as caught:
                self.send(wrapped)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual((ledger.reservations, transport.calls), ([], []))

    def test_invalid_proof_registry_or_clock_configuration_is_fixed_and_offline(self):
        ledger = Ledger()
        transport = Transport(ledger)
        for settings in ({"token_proofs": True}, {"clock": "not callable"}):
            with self.assertRaises(DeepSeekBudgetError):
                GuardedDeepSeekTransport(transport, ledger, currency="USD", **settings)
        self.assertEqual((ledger.reservations, transport.calls), ([], []))

    def test_real_classifier_builds_full_wire_then_guard_reserves_with_fake_io(self):
        completion = {"id": "synthetic-completion", "created": 1791619200, "model": "deepseek-flash",
                      "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                          "role": "assistant", "content": json.dumps({"results": [{"id": "1", "label": "IRRELEVANT",
                                                                          "reset_time": None, "reason": "Synthetic label"}]})}}]}
        wrapped, ledger, transport = self.wrapper(outcomes=(HttpResponse(200, json.dumps(completion).encode()),))
        classifier = DeepSeekClassifier(SYNTHETIC_SECRET, wrapped)
        classifier.classify(tweet(), now=NOW, timezone="Asia/Shanghai")
        self.assertEqual((len(ledger.reservations), len(transport.calls)), (1, 1))
        self.assertIsNone(classifier.last_usage)
        self.assertEqual(json.loads(json.loads(transport.calls[0][3])["messages"][1]["content"])["tweets"][0]["text"], tweet()["text"])


if __name__ == "__main__":
    unittest.main()
