"""Finite batch reservations: synthetic approvals, real files, no external IO."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import smtplib
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4


from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT
from monitor.batch import BatchAuthorization, BatchLedger, NoExternalCall
from monitor.locking import ProcessLock


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime" / "test-tmp").resolve()
NOW = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)


def synthetic_authorization(state_path, *, now=NOW, ledger_path=None):
    """Artificial test declarations only; no real credential/spending approval."""
    return {
        "schema_version": 1, "approval_id": "synthetic-approval", "batch_id": "synthetic-batch",
        "approved": True, "approved_at": now.isoformat(), "expires_at": (now + timedelta(hours=2)).isoformat(),
        "target": {"username": "thsottiaux", "user_id": "123456789"},
        "state_path": str(state_path), "ledger_path": str(ledger_path or Path(state_path).parent / "fixed-ledger.json"),
        "recipient": LOCAL_RECIPIENT, "sender": "synthetic@example.com",
        "source_contract_evidence_id": "synthetic-source-evidence",
        "deepseek": {"credential_use_authorized": True, "budget_approved": True,
                     "model": "synthetic-model", "currency": "USD", "price_snapshot_id": "synthetic-price",
                     "max_requests": 3, "amount": "0.30"},
        "apify": {"credential_use_authorized": True, "budget_approved": True, "monetary_bound_verified": True,
                  "bound_evidence_id": "synthetic-apify-bound", "currency": "USD", "max_run_starts": 1,
                  "max_run_polls": 3, "max_dataset_reads": 3, "max_http_attempts": 5, "amount": "0.10",
                  "start_max_cost": "0.01", "poll_max_cost": "0.001", "dataset_max_cost": "0.002"},
        "smtp": {"sender_authorized": True, "recipient_authorized": True, "send_authorized": True, "max_attempts": 3},
    }


def child_environment():
    # Fixed OS runtime allowlist; never inherit API keys, SMTP or app settings.
    allowed = ("SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT")
    result = {name: os.environ[name] for name in allowed if name in os.environ}
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


CHILD_PREFIX = """
import json, os, pathlib, socket, ssl, smtplib, sys, time
def blocked(*args, **kwargs):
    raise AssertionError('REAL_EXTERNAL_CALL_FORBIDDEN')
socket.socket = blocked
socket.create_connection = blocked
smtplib.SMTP = blocked
smtplib.SMTP_SSL = blocked
from datetime import datetime
from monitor.batch import BatchAuthorization, BatchLedger, NoExternalCall
directory = pathlib.Path(sys.argv[1])
raw = json.loads((directory / 'authorization.json').read_text(encoding='utf-8'))
now = datetime.fromisoformat(raw['approved_at'])
auth = BatchAuthorization.from_dict(raw, now=now)
"""


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = TEST_ROOT / ("batch-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.path = self.directory / "fixed-ledger.json"
        self.now = NOW
        self.raw = synthetic_authorization(self.directory / "live-state.json")
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("REAL_EXTERNAL_CALL_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)

    def cleanup(self):
        path, root = self.directory.resolve(), TEST_ROOT.resolve()
        if path.parent != root or path == root:
            raise RuntimeError("Refusing cleanup outside isolated test root")
        shutil.rmtree(path)

    def auth(self, raw=None):
        return BatchAuthorization.from_dict(raw or self.raw, now=self.now)

    def ledger(self, raw=None, hook=None):
        return BatchLedger(self.path, self.auth(raw), clock=lambda: self.now, fault_hook=hook)

    def initialize(self):
        ledger = self.ledger()
        ledger.initialize()
        return ledger

    def assert_denied(self, call, code=None):
        with self.assertRaises(NoExternalCall) as caught:
            call()
        self.assertFalse(caught.exception.retryable)
        if code:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_all_approvals_and_nonsecret_strict_schema_are_required(self):
        for section, field in ((None, "approved"), ("deepseek", "credential_use_authorized"),
                               ("deepseek", "budget_approved"), ("apify", "monetary_bound_verified"),
                               ("apify", "budget_approved"), ("smtp", "sender_authorized"),
                               ("smtp", "recipient_authorized"), ("smtp", "send_authorized")):
            with self.subTest(section=section, field=field):
                for value in (False, None, 1, "true"):
                    raw = deepcopy(self.raw)
                    (raw if section is None else raw[section])[field] = value
                    self.assert_denied(lambda: self.auth(raw))
        raw = deepcopy(self.raw)
        raw["api_key"] = "synthetic-secret-do-not-persist"
        error = self.assert_denied(lambda: self.auth(raw))
        self.assertNotIn("synthetic-secret", str(error))
        self.assertFalse(self.path.exists())

    def test_authorization_window_is_bounded_and_timezone_aware(self):
        for expiry in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(hours=24, seconds=1)):
            raw = deepcopy(self.raw)
            raw["expires_at"] = expiry.isoformat()
            self.assert_denied(lambda: self.auth(raw))
        raw = deepcopy(self.raw)
        raw["expires_at"] = (NOW + timedelta(hours=24)).isoformat()
        self.auth(raw)
        raw["approved_at"] = NOW.replace(tzinfo=None).isoformat()
        self.assert_denied(lambda: self.auth(raw))

    def test_money_rejects_float_nonfinite_exponent_and_ambiguous_values(self):
        for value in (0.1, True, 1, "NaN", "Infinity", "-1", "1e-2", "00.1", "0", "0.0000000000000000001"):
            with self.subTest(value=repr(value)):
                raw = deepcopy(self.raw)
                raw["deepseek"]["amount"] = value
                self.assert_denied(lambda: self.auth(raw))

    def test_run_never_initializes_or_restores_missing_ledger(self):
        ledger = self.ledger()
        self.assert_denied(ledger.validate_ready, "VALIDATION_LEDGER_NOT_INITIALIZED")
        self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_LEDGER_NOT_INITIALIZED")
        self.assertFalse(self.path.exists())
        self.path.with_name(self.path.name + ".bak").write_text("{}", encoding="utf-8")
        self.assert_denied(ledger.initialize, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assertFalse(self.path.exists())

    def test_explicit_initialization_is_idempotent_and_never_resets_spent_quota(self):
        ledger = self.initialize()
        receipt = ledger.reserve("smtp_attempt")
        before = self.path.read_bytes()
        again = self.ledger().initialize()
        self.assertEqual(again["counts"]["smtp_attempt"], 1)
        self.assertEqual(again["receipts"], [receipt])
        self.assertEqual(self.path.read_bytes(), before)

    def test_authorization_mutation_new_batch_and_new_state_cannot_reuse_approval(self):
        self.initialize().reserve("smtp_attempt")
        before = self.path.read_bytes()
        for change in ("amount", "batch", "state", "sender", "recipient"):
            with self.subTest(change=change):
                raw = deepcopy(self.raw)
                if change == "amount": raw["deepseek"]["amount"] = "0.40"
                elif change == "batch": raw["batch_id"] = "synthetic-second-batch"
                elif change == "state": raw["state_path"] = str(self.directory / "another-live.json")
                elif change == "sender": raw["sender"] = "another@example.com"
                else: raw["smtp"]["max_attempts"] = 4
                ledger = self.ledger(raw)
                self.assert_denied(ledger.initialize)
                self.assert_denied(ledger.validate_ready)
                self.assert_denied(lambda: ledger.reserve("smtp_attempt"))
                self.assertEqual(self.path.read_bytes(), before)

    def test_new_separately_approved_batch_preserves_old_history(self):
        first = self.initialize()
        first.reserve("smtp_attempt")
        second_raw = deepcopy(self.raw)
        second_raw.update(batch_id="synthetic-second-batch", approval_id="synthetic-second-approval")
        second = self.ledger(second_raw)
        second.initialize()
        second.reserve("smtp_attempt")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["batches"]), 2)
        self.assertEqual(first.validate_ready()["counts"]["smtp_attempt"], 1)
        self.assertEqual(second.validate_ready()["counts"]["smtp_attempt"], 1)

    def test_reservations_keep_exact_decimal_cost_without_usage_or_refunds(self):
        ledger = self.initialize()
        for _ in range(3):
            ledger.reserve("deepseek_request", amount=Decimal("0.10"), currency="USD",
                           metadata={"input_tokens_upper_bound": 1000, "output_tokens_upper_bound": 256,
                                     "price_snapshot_id": "synthetic-price"})
            # A timeout, HTTP failure or absent usage is not a refund operation.
        entry = self.ledger().validate_ready()
        self.assertEqual(entry["reserved"]["deepseek"], {"currency": "USD", "amount": "0.30"})
        self.assertEqual(entry["counts"]["deepseek_request"], 3)
        self.assert_denied(lambda: ledger.reserve("deepseek_request", amount="0.01", currency="USD"))

    def test_request_and_money_caps_are_independent(self):
        raw = deepcopy(self.raw)
        raw["deepseek"].update(max_requests=1, amount="1")
        ledger = self.ledger(raw)
        ledger.initialize()
        ledger.reserve("deepseek_request", amount="0.01", currency="USD")
        self.assert_denied(lambda: ledger.reserve("deepseek_request", amount="0.01", currency="USD"), "VALIDATION_ATTEMPT_LIMIT_REACHED")
        raw = deepcopy(self.raw)
        raw.update(approval_id="synthetic-new-approval", batch_id="synthetic-new-batch")
        raw["deepseek"].update(max_requests=10, amount="0.01")
        ledger = self.ledger(raw)
        ledger.initialize()
        self.assert_denied(lambda: ledger.reserve("deepseek_request", amount="0.02", currency="USD"), "VALIDATION_MONEY_LIMIT_REACHED")
        self.assertEqual(ledger.validate_ready()["counts"]["deepseek_request"], 0)

    def test_usd_and_cny_never_convert_or_share_reservations(self):
        raw = deepcopy(self.raw)
        raw["deepseek"].update(currency="CNY", amount="1")
        ledger = self.ledger(raw)
        ledger.initialize()
        self.assert_denied(lambda: ledger.reserve("deepseek_request", amount="1", currency="USD"), "VALIDATION_CURRENCY_MISMATCH")
        ledger.reserve("deepseek_request", amount="1", currency="CNY")
        self.assertEqual(ledger.validate_ready()["reserved"]["deepseek"]["amount"], "1")
        # Ledger supports separate units; root pricing preflight must prove CNY.

    def test_apify_each_http_attempt_and_cost_is_reserved_including_rereads(self):
        raw = deepcopy(self.raw)
        raw["apify"]["max_http_attempts"] = 3
        ledger = self.ledger(raw)
        ledger.initialize()
        ledger.reserve("apify_start", amount="0.01", currency="USD")
        ledger.reserve("apify_poll", amount="0.001", currency="USD")
        ledger.reserve("apify_dataset", amount="0.002", currency="USD")
        self.assert_denied(lambda: ledger.reserve("apify_dataset", amount="0.002", currency="USD"), "VALIDATION_APIFY_HTTP_LIMIT_REACHED")
        entry = ledger.validate_ready()
        self.assertEqual(entry["counts"]["apify_http_attempts"], 3)
        self.assertEqual(entry["reserved"]["apify"]["amount"], "0.013")

    def test_apify_understated_bound_and_second_start_are_rejected(self):
        ledger = self.initialize()
        self.assert_denied(lambda: ledger.reserve("apify_start", amount="0.001", currency="USD"), "VALIDATION_COST_BOUND_INSUFFICIENT")
        self.assertEqual(ledger.validate_ready()["counts"]["apify_http_attempts"], 0)
        ledger.reserve("apify_start", amount="0.01", currency="USD")
        self.assert_denied(lambda: ledger.reserve("apify_start", amount="0.01", currency="USD"), "VALIDATION_ATTEMPT_LIMIT_REACHED")
        self.assertEqual(ledger.validate_ready()["counts"]["apify_http_attempts"], 1)

    def test_smtp_all_kinds_and_retries_use_the_same_attempt_cap(self):
        ledger = self.initialize()
        for kind in ("TWEET", "TWEET", "HEALTH"):
            ledger.reserve("smtp_attempt", metadata={"notification_kind": kind})
        self.assert_denied(lambda: self.ledger().reserve("smtp_attempt", metadata={"notification_kind": "HEALTH"}), "VALIDATION_ATTEMPT_LIMIT_REACHED")
        self.assertEqual(ledger.validate_ready()["counts"]["smtp_attempt"], 3)

    def test_save_failure_prevents_guarded_io_and_does_not_consume(self):
        self.initialize()
        external = []
        def fail(event, _snapshot):
            if event == "before_replace":
                raise OSError("synthetic-sensitive-error-must-not-leak")
        ledger = self.ledger(hook=fail)
        def guarded():
            ledger.reserve("smtp_attempt")
            external.append("attempt")
        error = self.assert_denied(guarded, "VALIDATION_LEDGER_COMMIT_FAILED")
        self.assertNotIn("sensitive", str(error))
        self.assertEqual(external, [])
        self.assertEqual(self.ledger().validate_ready()["counts"]["smtp_attempt"], 0)

    def test_crash_after_commit_before_io_loses_quota_conservatively(self):
        self.initialize()
        external = []
        def crash(event, _snapshot):
            if event == "after_replace":
                raise SystemExit(23)
        with self.assertRaises(SystemExit):
            self.ledger(hook=crash).reserve("smtp_attempt")
            external.append("attempt")
        self.assertEqual(external, [])
        self.assertEqual(self.ledger().validate_ready()["counts"]["smtp_attempt"], 1)

    def test_every_boundary_rechecks_expiry_and_cannot_extend_it(self):
        ledger = self.initialize()
        ledger.reserve("smtp_attempt")
        before = self.path.read_bytes()
        self.now = NOW + timedelta(hours=2)
        self.assert_denied(ledger.validate_ready, "VALIDATION_AUTHORIZATION_EXPIRED")
        self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_AUTHORIZATION_EXPIRED")
        self.assertEqual(self.path.read_bytes(), before)

    def test_forged_dto_and_secret_metadata_are_refused(self):
        auth = replace(self.auth(), target_user_id="999")
        ledger = BatchLedger(self.path, auth, clock=lambda: self.now)
        self.assert_denied(ledger.initialize, "VALIDATION_AUTHORIZATION_BINDING_INVALID")
        ledger = self.initialize()
        for metadata in ({"api_key": "synthetic-secret"}, {"price_snapshot_id": "synthetic-secret"},
                         {"input_tokens_upper_bound": True}, {"notification_kind": "synthetic-secret"}):
            self.assert_denied(lambda: ledger.reserve("smtp_attempt", metadata=metadata), "VALIDATION_RESERVATION_METADATA_INVALID")
        self.assertNotIn("synthetic-secret", self.path.read_text(encoding="utf-8"))

    def test_corrupt_main_is_never_repaired_from_old_backup(self):
        ledger = self.initialize()
        ledger.reserve("smtp_attempt")
        backup = self.path.with_name(self.path.name + ".bak")
        backup.write_bytes(self.path.read_bytes())
        self.path.write_text("{not valid JSON", encoding="utf-8")
        before = self.path.read_bytes()
        self.assert_denied(ledger.validate_ready, "VALIDATION_LEDGER_INVALID")
        self.assert_denied(ledger.initialize, "VALIDATION_LEDGER_INVALID")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(backup.exists())

    def test_missing_main_with_permanent_guard_cannot_reinitialize_or_refund(self):
        ledger = self.initialize()
        ledger.reserve("smtp_attempt")
        guard_before = ledger.guard_path.read_bytes()
        self.path.unlink()
        self.assert_denied(ledger.initialize, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assert_denied(ledger.validate_ready, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assertFalse(self.path.exists())
        self.assertEqual(ledger.guard_path.read_bytes(), guard_before)

    def test_rolling_main_back_to_valid_old_snapshot_requires_reconciliation(self):
        ledger = self.initialize()
        old_main = self.path.read_bytes()
        ledger.reserve("smtp_attempt")
        guard_before = ledger.guard_path.read_bytes()
        self.path.write_bytes(old_main)
        self.assert_denied(ledger.validate_ready, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assert_denied(ledger.initialize, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_LEDGER_RECOVERY_REQUIRED")
        self.assertEqual(ledger.guard_path.read_bytes(), guard_before)
        self.assertEqual(self.path.read_bytes(), old_main)

    def test_missing_or_corrupt_guard_is_never_regenerated_for_existing_main(self):
        ledger = self.initialize()
        ledger.reserve("smtp_attempt")
        main_before = self.path.read_bytes()
        good_guard = ledger.guard_path.read_bytes()
        for content in (None, b"broken guard", b'{"schema_version":true,"revision":2,"ledger_sha256":"bad"}'):
            with self.subTest(content=content):
                if content is None:
                    ledger.guard_path.unlink(missing_ok=True)
                else:
                    ledger.guard_path.write_bytes(content)
                self.assert_denied(ledger.validate_ready, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
                self.assert_denied(ledger.initialize, "VALIDATION_LEDGER_RECOVERY_REQUIRED")
                self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_LEDGER_RECOVERY_REQUIRED")
                self.assertEqual(self.path.read_bytes(), main_before)
                if content is None:
                    self.assertFalse(ledger.guard_path.exists())
                else:
                    self.assertEqual(ledger.guard_path.read_bytes(), content)
        ledger.guard_path.write_bytes(good_guard)
        self.assertEqual(ledger.validate_ready()["counts"]["smtp_attempt"], 1)

    def test_crash_between_witness_and_main_commit_pauses_all_further_attempts(self):
        ledger = self.initialize()
        main_before = self.path.read_bytes()
        def crash(event, _snapshot):
            if event == "after_guard_replace":
                raise SystemExit(24)
        external = []
        with self.assertRaises(SystemExit):
            self.ledger(hook=crash).reserve("smtp_attempt")
            external.append("attempt")
        self.assertEqual(external, [])
        self.assertEqual(self.path.read_bytes(), main_before)
        for action in (ledger.validate_ready, ledger.initialize, lambda: ledger.reserve("smtp_attempt")):
            self.assert_denied(action, "VALIDATION_LEDGER_RECOVERY_REQUIRED")

    def test_first_initialization_crash_after_witness_leaves_a_permanent_hold(self):
        def crash(event, _snapshot):
            if event == "after_guard_replace":
                raise SystemExit(25)
        ledger = self.ledger(hook=crash)
        with self.assertRaises(SystemExit):
            ledger.initialize()
        self.assertTrue(ledger.guard_path.exists())
        self.assertFalse(self.path.exists())
        self.assert_denied(self.ledger().initialize, "VALIDATION_LEDGER_RECOVERY_REQUIRED")

    def test_tampered_derived_counts_receipts_and_hash_are_refused(self):
        ledger = self.initialize()
        ledger.reserve("smtp_attempt")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for change in ("count", "bool_count", "hash", "receipt", "approval_index"):
            with self.subTest(change=change):
                raw = deepcopy(original)
                entry = raw["batches"][self.auth().batch_id]
                if change == "count": entry["counts"]["smtp_attempt"] = 0
                elif change == "bool_count": entry["counts"]["smtp_attempt"] = True
                elif change == "hash": entry["authorization_sha256"] = "0" * 64
                elif change == "receipt": entry["receipts"][0]["sequence"] = 2
                else: raw["approval_index"] = {}
                data = json.dumps(raw).encode("utf-8")
                self.path.write_bytes(data)
                # A matching synthetic witness lets this case independently
                # exercise strict ledger validation. The rollback tests above
                # cover a changed main with its real, unchanged witness.
                ledger.guard_path.write_text(json.dumps({"schema_version": 1, "revision": raw["revision"],
                    "ledger_sha256": hashlib.sha256(data).hexdigest()}), encoding="ascii")
                self.assert_denied(ledger.validate_ready, "VALIDATION_LEDGER_INVALID")

    def test_lock_conflict_has_no_reservation_or_external_side_effect(self):
        ledger = self.initialize()
        before = self.path.read_bytes()
        with ProcessLock(str(self.path) + ".lock"):
            self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_LEDGER_BUSY")
        self.assertEqual(self.path.read_bytes(), before)

    def test_fixed_ledger_path_cannot_alias_state_backup_or_lock(self):
        auth = self.auth()
        for suffix in ("", ".bak", ".lock", ".guard"):
            path = auth.state_path.with_name(auth.state_path.name + suffix)
            raw = deepcopy(self.raw)
            raw["ledger_path"] = str(path)
            self.assert_denied(lambda: self.auth(raw), "VALIDATION_LEDGER_STATE_PATH_CONFLICT")
        raw = deepcopy(self.raw)
        raw["ledger_path"] = str(self.directory / "reset-ledger.json")
        self.assert_denied(lambda: BatchLedger(self.path, self.auth(raw), clock=lambda: self.now), "VALIDATION_LEDGER_PATH_BINDING_MISMATCH")

    def test_ledger_constructor_is_pure_and_rejects_windows_remote_or_ambiguous_paths(self):
        auth = self.auth()
        with patch.object(Path, "resolve", side_effect=AssertionError("FILESYSTEM_RESOLUTION_FORBIDDEN")), \
                patch.object(Path, "exists", side_effect=AssertionError("FILESYSTEM_READ_FORBIDDEN")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("FILESYSTEM_READ_FORBIDDEN")):
            BatchLedger(self.path, auth, clock=lambda: self.now)
        if os.name == "nt":
            for value in (r"\\server\share\ledger.json", r"\\?\C:\ledger.json", r"C:\ledger.json:stream",
                          r"C:\CON\ledger.json", "C:\\folder.\\ledger.json"):
                self.assert_denied(lambda: BatchLedger(value, auth, clock=lambda: self.now))
        raw = deepcopy(self.raw)
        raw["state_path"] = str(self.path) + ".lock"
        self.assert_denied(lambda: self.auth(raw), "VALIDATION_LEDGER_STATE_PATH_CONFLICT")

    def test_same_authorization_in_two_project_roots_preserves_one_absolute_ledger(self):
        first_root, second_root = self.directory / "project-one", self.directory / "project-two"
        first_root.mkdir()
        second_root.mkdir()
        for root in (first_root, second_root):
            (root / "authorization.json").write_text(json.dumps(self.raw), encoding="utf-8")
        first_auth = BatchAuthorization.from_dict(json.loads((first_root / "authorization.json").read_text(encoding="utf-8")), now=self.now)
        first = BatchLedger(first_auth.ledger_path, first_auth, clock=lambda: self.now)
        first.initialize()
        first.reserve("smtp_attempt")
        second_auth = BatchAuthorization.from_dict(json.loads((second_root / "authorization.json").read_text(encoding="utf-8")), now=self.now)
        second = BatchLedger(second_auth.ledger_path, second_auth, clock=lambda: self.now)
        second.initialize()
        self.assertEqual(second.validate_ready()["counts"]["smtp_attempt"], 1)
        second.reserve("smtp_attempt")
        self.assertEqual(first.validate_ready()["counts"]["smtp_attempt"], 2)
        self.assertEqual(first.path, second.path)
        for root in (first_root, second_root):
            self.assertFalse((root / "fixed-ledger.json").exists())
            self.assert_denied(lambda: BatchLedger(root / "fixed-ledger.json", first_auth, clock=lambda: self.now),
                               "VALIDATION_LEDGER_PATH_BINDING_MISMATCH")

    def test_ledger_path_is_required_and_changes_authorization_hash(self):
        raw = deepcopy(self.raw)
        del raw["ledger_path"]
        self.assert_denied(lambda: self.auth(raw))
        original = self.auth()
        raw = deepcopy(self.raw)
        raw["ledger_path"] = str(self.directory / "new-ledger.json")
        changed = self.auth(raw)
        self.assertNotEqual(original.authorization_sha256, changed.authorization_sha256)
        self.assert_denied(lambda: BatchLedger(changed.ledger_path, original, clock=lambda: self.now),
                           "VALIDATION_LEDGER_PATH_BINDING_MISMATCH")

    def test_two_processes_cannot_both_consume_last_attempt(self):
        self.raw["smtp"]["max_attempts"] = 1
        self.initialize()
        (self.directory / "authorization.json").write_text(json.dumps(self.raw), encoding="utf-8")
        code = CHILD_PREFIX + """
identity = sys.argv[2]
(directory / ('ready-' + identity)).write_text('ready', encoding='ascii')
deadline = time.monotonic() + 10
while not (directory / 'go').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('SYNTHETIC_BARRIER_TIMEOUT')
    time.sleep(0.01)
ledger = BatchLedger(directory / 'fixed-ledger.json', auth, clock=lambda: now)
try:
    ledger.reserve('smtp_attempt')
    result = 'RESERVED'
except NoExternalCall as error:
    result = error.code
(directory / ('result-' + identity)).write_text(result, encoding='ascii')
"""
        processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(self.directory), str(index)],
                                      cwd=ROOT, env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for index in range(2)]
        self.addCleanup(lambda: [process.kill() for process in processes if process.poll() is None])
        deadline = time.monotonic() + 10
        while not all((self.directory / f"ready-{index}").exists() for index in range(2)):
            if time.monotonic() >= deadline:
                self.fail("Synthetic subprocess barrier did not become ready")
            time.sleep(0.01)
        (self.directory / "go").write_text("go", encoding="ascii")
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, (stdout + stderr).decode(errors="replace"))
        results = [(self.directory / f"result-{index}").read_text(encoding="ascii") for index in range(2)]
        self.assertEqual(results.count("RESERVED"), 1)
        self.assertIn(next(item for item in results if item != "RESERVED"), ("VALIDATION_LEDGER_BUSY", "VALIDATION_ATTEMPT_LIMIT_REACHED"))
        self.assertEqual(self.ledger().validate_ready()["counts"]["smtp_attempt"], 1)

    def test_process_exit_after_reservation_never_reuses_consumed_attempt(self):
        self.raw["smtp"]["max_attempts"] = 1
        self.initialize()
        (self.directory / "authorization.json").write_text(json.dumps(self.raw), encoding="utf-8")
        code = CHILD_PREFIX + """
def crash(event, snapshot):
    if event == 'after_replace':
        os._exit(23)
ledger = BatchLedger(directory / 'fixed-ledger.json', auth, clock=lambda: now, fault_hook=crash)
ledger.reserve('smtp_attempt')
raise AssertionError('GUARDED_IO_MUST_NOT_BE_REACHED')
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.directory)], cwd=ROOT,
                                env=child_environment(), capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 23, (result.stdout + result.stderr).decode(errors="replace"))
        ledger = self.ledger()
        self.assertEqual(ledger.validate_ready()["counts"]["smtp_attempt"], 1)
        self.assert_denied(lambda: ledger.reserve("smtp_attempt"), "VALIDATION_ATTEMPT_LIMIT_REACHED")

    def test_process_exit_between_guard_and_main_leaves_recovery_hold(self):
        self.initialize()
        main_before = self.path.read_bytes()
        (self.directory / "authorization.json").write_text(json.dumps(self.raw), encoding="utf-8")
        code = CHILD_PREFIX + """
def crash(event, snapshot):
    if event == 'after_guard_replace':
        os._exit(24)
ledger = BatchLedger(directory / 'fixed-ledger.json', auth, clock=lambda: now, fault_hook=crash)
ledger.reserve('smtp_attempt')
raise AssertionError('GUARDED_IO_MUST_NOT_BE_REACHED')
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.directory)], cwd=ROOT,
                                env=child_environment(), capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 24, (result.stdout + result.stderr).decode(errors="replace"))
        self.assertEqual(self.path.read_bytes(), main_before)
        ledger = self.ledger()
        for action in (ledger.validate_ready, ledger.initialize, lambda: ledger.reserve("smtp_attempt")):
            self.assert_denied(action, "VALIDATION_LEDGER_RECOVERY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
