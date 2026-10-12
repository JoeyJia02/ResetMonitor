"""Offline full-entrypoint diagnostics for the isolated background candidate."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import smtplib
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.error import URLError
from uuid import uuid4


from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.credentials import ProjectCredentials
from monitor.credential_store import CredentialStoreError
from monitor.locking import LockBusy, ProcessLock
from monitor.notifier import DeliveryResult
from scripts import background_monitor_recovery_candidate as monitor
from scripts.one_shot_source_diagnostic_candidate import OneShotSourceError
from tests.test_background_monitor import model_response
from tests.test_one_shot_source import Response, Transport, official_embed
from tests.test_smtp_transport import FakeFactory


ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT", str(Path(tempfile.gettempdir()) / "resetmonitor-tests"))).resolve()
NOW = datetime(2026, 10, 12, 0, 0, 11, tzinfo=timezone.utc)
SECRET = "SYNTHETIC_DIAGNOSTIC_SECRET_TOKEN"
SENTINEL = "RAW_EXCEPTION_BODY_AND_URL_QUERY_MUST_NOT_APPEAR"
CASE_RESULTS = []


class DiagnosticsCandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ("diagnostic-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("REAL_EXTERNAL_IO_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)
        self.rows = json.loads((Path(__file__).resolve().parents[1] /
            "tests/fixtures/source-items.json").read_text(encoding="utf-8"))
        curated = json.loads((Path(__file__).resolve().parents[1] /
            "tests/fixtures/source-context.json").read_text(encoding="utf-8"))
        self.embeds = [official_embed(item) for item in curated["items"]]
        self.credentials = ProjectCredentials(apify_token=SECRET,
            deepseek_api_key="SYNTHETIC_DIAGNOSTIC_MODEL_KEY",
            smtp_user=LOCAL_SENDER, smtp_password="abcdefghijklmnop")

    def cleanup(self):
        target = self.root.resolve()
        if target.parent != ROOT or target == ROOT:
            raise AssertionError("UNSAFE_TEST_CLEANUP")
        shutil.rmtree(target)

    def wire(self, *, rows=None, embeds=None, actor_status=201):
        return Transport([Response(self.rows if rows is None else rows, status=actor_status)] +
                         [Response(item) for item in (self.embeds if embeds is None else embeds)])

    def run_at(self, transport, *, when=NOW, **overrides):
        arguments = dict(root=self.root, credential_loader=lambda: self.credentials,
            transport=transport, smtp_factory=FakeFactory(),
            chain_lock=self.root / "shared.lock", manual_results_root=self.root / "manual")
        arguments.update(overrides)
        result = monitor.run_slot(when, **arguments)
        receipt_path = arguments["root"] / (monitor.due_slot(when) + ".receipt.json")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else None
        detail = receipt or result
        CASE_RESULTS.append({"test": self.id().rsplit(".", 1)[-1],
            "slot": monitor.due_slot(when), "status": detail.get("status"),
            "stage": detail.get("stage"), "error_code": detail.get("error_code"),
            "exception_class": detail.get("exception_class"),
            "source_failure_stage": detail.get("source_failure_stage"),
            "source_error_code": detail.get("source_error_code"),
            "source_exception_class": detail.get("source_exception_class"),
            "source_journal_written": detail.get("persistence", {}).get("source_journal_written"),
            "requests": [{"kind": item["kind"], "context_index": item["context_index"],
                          "http_status": item["http_status"], "state": item["state"]}
                         for item in detail.get("requests", [])]})
        return result, receipt

    def assert_private(self, receipt):
        text = json.dumps(receipt, ensure_ascii=False)
        for forbidden in (SECRET, self.credentials.deepseek_api_key,
                          self.credentials.smtp_password, SENTINEL,
                          "?id=", "Authorization", "Bearer ", "full_url", "headers"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn(self.rows[0]["text"], text)

    def assert_failure(self, wire, stage, code, kind=None, exception_class=None,
                       http_status=None, context_index=None, request_state="FAILED"):
        result, receipt = self.run_at(wire)
        self.assertIsNotNone(receipt)
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"]),
                         ("UNKNOWN", stage, code))
        if exception_class:
            self.assertEqual(receipt["exception_class"], exception_class)
        self.assertFalse(receipt["persistence"]["source_journal_written"])
        if kind:
            request = receipt["requests"][-1]
            self.assertEqual((request["kind"], request["context_index"]), (kind, context_index))
            self.assertEqual(request["state"], request_state)
            self.assertEqual(request["http_status"], http_status)
            self.assertRegex(request["request_id"], r"^[0-9a-f]{32}$")
            datetime.fromisoformat(request["started_at"])
        self.assert_private(receipt)
        return receipt

    def test_actor_post_transport_http_error_preserves_class_and_safe_status(self):
        error = HTTPError("https://api.example.invalid/?token=" + SENTINEL,
                          401, "synthetic", None, None)
        self.addCleanup(error.close)
        self.assert_failure(Transport([error]), "ACTOR_POST_OPEN",
            "BACKGROUND_ACTOR_POST_OPEN_FAILED", "ACTOR_POST", "HTTP_ERROR", 401)

    def test_actor_post_timeout_preserves_transport_class(self):
        self.assert_failure(Transport([TimeoutError(SENTINEL)]), "ACTOR_POST_OPEN",
            "BACKGROUND_ACTOR_POST_OPEN_FAILED", "ACTOR_POST", "TIMEOUT_ERROR")

    def test_actor_post_tls_failure_preserves_transport_class(self):
        self.assert_failure(Transport([ssl.SSLError(SENTINEL)]), "ACTOR_POST_OPEN",
            "BACKGROUND_ACTOR_POST_OPEN_FAILED", "ACTOR_POST", "SSL_ERROR")

    def test_url_error_timeout_reason_preserves_original_class(self):
        self.assert_failure(Transport([URLError(TimeoutError(SENTINEL))]), "ACTOR_POST_OPEN",
            "BACKGROUND_ACTOR_POST_OPEN_FAILED", "ACTOR_POST", "TIMEOUT_ERROR")

    def test_url_error_tls_reason_preserves_original_class(self):
        self.assert_failure(Transport([URLError(ssl.SSLError(SENTINEL))]), "ACTOR_POST_OPEN",
            "BACKGROUND_ACTOR_POST_OPEN_FAILED", "ACTOR_POST", "SSL_ERROR")

    def test_url_error_raw_reason_is_only_classified(self):
        self.assert_failure(Transport([URLError(SENTINEL)]), "ACTOR_POST_OPEN",
            "BACKGROUND_ACTOR_POST_OPEN_FAILED", "ACTOR_POST", "URL_ERROR")

    def test_actor_http_status_is_not_collapsed_into_generic_request_failure(self):
        self.assert_failure(Transport([Response([], status=503)]), "ACTOR_POST_HTTP",
            "ONE_SHOT_SOURCE_HTTP_FAILED", "ACTOR_POST", "SOURCE_VALIDATION", 503)

    def test_actor_json_error_records_parse_stage_and_class(self):
        self.assert_failure(Transport([Response(b"{invalid json", status=201)]), "ACTOR_POST_JSON",
            "ONE_SHOT_SOURCE_JSON_INVALID", "ACTOR_POST", "JSON_ERROR", 201)

    def test_actor_body_read_failure_preserves_stage_and_class(self):
        class BrokenRead(Response):
            def read(self, limit):
                raise TimeoutError(SENTINEL)
        self.assert_failure(Transport([BrokenRead(self.rows, status=201)]), "ACTOR_POST_BODY",
            "BACKGROUND_ACTOR_POST_BODY_FAILED", "ACTOR_POST", "TIMEOUT_ERROR", 201)

    def test_actor_rows_invalid_records_validation_stage(self):
        rows = deepcopy(self.rows)
        rows[0]["success"] = False
        self.assert_failure(self.wire(rows=rows), "ACTOR_ROWS",
            "ONE_SHOT_SOURCE_ROW_FAILED", "ACTOR_POST", "SOURCE_VALIDATION", 201)

    def test_json_escaped_secret_is_rejected_after_parsing_without_reflection(self):
        escaped = "".join("\\u%04x" % ord(char) for char in SECRET)
        body = ('["' + escaped + '"]').encode("ascii")
        self.assertNotIn(SECRET.encode(), body)
        self.assert_failure(Transport([Response(body, status=201)]), "ACTOR_POST_JSON",
            "ONE_SHOT_SOURCE_SECRET_REFLECTION", "ACTOR_POST", "SOURCE_VALIDATION", 201)

    def test_official_get_transport_http_error_preserves_class_and_ordinal(self):
        error = HTTPError("https://cdn.example.invalid/?id=" + SENTINEL,
                          429, "synthetic", None, None)
        self.addCleanup(error.close)
        self.assert_failure(Transport([Response(self.rows, status=201), error]),
            "CONTEXT_GET_OPEN", "BACKGROUND_CONTEXT_GET_OPEN_FAILED",
            "CONTEXT_GET", "HTTP_ERROR", 429, 1)

    def test_official_get_http_status(self):
        self.assert_failure(Transport([Response(self.rows, status=201),
                                       Response(self.embeds[0], status=502)]),
            "CONTEXT_GET_HTTP", "ONE_SHOT_EMBED_HTTP_FAILED",
            "CONTEXT_GET", "SOURCE_VALIDATION", 502, 1)

    def test_official_get_json_error(self):
        self.assert_failure(Transport([Response(self.rows, status=201), Response(b"{bad")]),
            "CONTEXT_GET_JSON", "ONE_SHOT_EMBED_JSON_INVALID",
            "CONTEXT_GET", "JSON_ERROR", 200, 1)

    def test_official_get_body_read_failure_preserves_stage_and_class(self):
        class BrokenRead(Response):
            def read(self, limit):
                raise OSError(SENTINEL)
        self.assert_failure(Transport([Response(self.rows, status=201),
                                       BrokenRead(self.embeds[0])]), "CONTEXT_GET_BODY",
            "BACKGROUND_CONTEXT_GET_BODY_FAILED", "CONTEXT_GET", "OS_ERROR", 200, 1)

    def test_official_get_json_escaped_secret_is_rejected_after_parsing(self):
        embed = deepcopy(self.embeds[0])
        embed["unexpected"] = SECRET
        escaped = "".join("\\u%04x" % ord(char) for char in SECRET)
        body = json.dumps(embed, ensure_ascii=False).replace(SECRET, escaped).encode("utf-8")
        self.assertNotIn(SECRET.encode(), body)
        self.assert_failure(Transport([Response(self.rows, status=201), Response(body)]),
            "CONTEXT_GET_JSON", "ONE_SHOT_EMBED_SECRET_REFLECTION",
            "CONTEXT_GET", "SOURCE_VALIDATION", 200, 1)

    def test_official_get_id_validation(self):
        embeds = deepcopy(self.embeds)
        embeds[0]["id_str"] = "123"
        self.assert_failure(self.wire(embeds=embeds), "CONTEXT_ID",
            "ONE_SHOT_EMBED_ID_MISMATCH", "CONTEXT_GET", "SOURCE_VALIDATION", 200, 1)

    def test_official_get_author_validation(self):
        embeds = deepcopy(self.embeds)
        embeds[0]["user"]["id_str"] = "123"
        self.assert_failure(self.wire(embeds=embeds), "CONTEXT_AUTHOR",
            "ONE_SHOT_EMBED_IDENTITY_MISMATCH", "CONTEXT_GET", "SOURCE_VALIDATION", 200, 1)

    def test_official_get_time_validation(self):
        embeds = deepcopy(self.embeds)
        embeds[0]["created_at"] = "2026-10-10T05:22:48Z"
        self.assert_failure(self.wire(embeds=embeds), "CONTEXT_TIME",
            "ONE_SHOT_EMBED_TIME_MISMATCH", "CONTEXT_GET", "SOURCE_VALIDATION", 200, 1)

    def test_official_get_text_validation(self):
        embeds = deepcopy(self.embeds)
        embeds[0]["text"] = "wrong synthetic text"
        self.assert_failure(self.wire(embeds=embeds), "CONTEXT_TEXT",
            "ONE_SHOT_EMBED_TEXT_MISMATCH", "CONTEXT_GET", "SOURCE_VALIDATION", 200, 1)

    def test_official_get_quote_validation(self):
        embeds = deepcopy(self.embeds)
        embeds[1].pop("quoted_tweet")
        self.assert_failure(self.wire(embeds=embeds), "CONTEXT_QUOTE",
            "ONE_SHOT_EMBED_QUOTE_MISSING", "CONTEXT_GET", "SOURCE_VALIDATION", 200, 2)

    def test_source_journal_write_failure_persists_diagnostic_in_receipt(self):
        original = monitor._checkpoint
        def fail_result(path, job, secrets):
            if path.name.endswith(".result.json"):
                raise PermissionError(SENTINEL)
            return original(path, job, secrets)
        with patch.object(monitor, "_checkpoint", side_effect=fail_result):
            receipt = self.assert_failure(self.wire(), "SOURCE_JOURNAL_WRITE",
                "BACKGROUND_SOURCE_JOURNAL_WRITE_FAILED", "CONTEXT_GET", "PERMISSION_ERROR",
                200, 3, request_state="VALIDATED")
        self.assertEqual([item["state"] for item in receipt["requests"]], ["VALIDATED"] * 4)
        self.assertFalse((self.root / (monitor.due_slot(NOW) + ".result.json")).exists())

    def test_lock_busy_is_durable_and_does_not_read_credentials(self):
        with ProcessLock(self.root / "shared.lock"):
            result, receipt = self.run_at(Transport([]), credential_loader=lambda: self.fail("credential read"))
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"]),
                         ("BUSY", "LOCK_ACQUIRE", "BACKGROUND_LOCK_BUSY"))
        self.assertEqual(receipt["requests"], [])
        self.assert_private(receipt)

    def test_same_slot_lock_busy_precedes_claim_and_receipt_io(self):
        original = ProcessLock.acquire
        def occupied(lock):
            if lock.path.name.endswith(".slot.lock"):
                raise LockBusy("synthetic same-slot overlap")
            return original(lock)
        with patch.object(ProcessLock, "acquire", new=occupied):
            result, receipt = self.run_at(Transport([]),
                credential_loader=lambda: self.fail("credential read"))
        self.assertIsNone(receipt)
        self.assertEqual((result["status"], result["stage"], result["error_code"],
                          result["exception_class"]),
                         ("BUSY", "SLOT_LOCK", "BACKGROUND_LOCK_BUSY", "LOCK_BUSY"))
        self.assertFalse((self.root / (monitor.due_slot(NOW) + ".attempt.json")).exists())

    def test_manual_seed_failure_is_localized_before_credentials(self):
        with patch.object(monitor, "_successful_manual_ids", side_effect=OSError(SENTINEL)):
            result, receipt = self.run_at(Transport([]), credential_loader=lambda: self.fail("credential read"))
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"],
                          receipt["exception_class"]),
                         ("UNKNOWN", "MANUAL_SEED", "BACKGROUND_MANUAL_SEED_FAILED", "OS_ERROR"))
        self.assert_private(receipt)

    def test_seen_state_corruption_has_fixed_diagnostic(self):
        (self.root / "seen-v1.json").write_text('{"schema_version":true,"seen_ids":[]}', encoding="utf-8")
        result, receipt = self.run_at(Transport([]))
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"],
                          receipt["exception_class"]),
                         ("FAILED", "SEEN_LOAD", "BACKGROUND_STATE_REVIEW_REQUIRED",
                          "BACKGROUND_VALIDATION"))
        self.assertEqual(receipt["requests"], [])

    def test_prior_journal_corruption_has_fixed_diagnostic(self):
        (self.root / "scheduled-20261011-2000.result.json").write_text("{bad", encoding="utf-8")
        result, receipt = self.run_at(Transport([]))
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"]),
                         ("FAILED", "JOURNAL_SCAN", "BACKGROUND_JOURNAL_REVIEW_REQUIRED"))
        self.assertEqual(receipt["requests"], [])

    def test_credential_loader_failure_is_localized(self):
        def fail():
            raise PermissionError(SENTINEL)
        result, receipt = self.run_at(Transport([]), credential_loader=fail)
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"],
                          receipt["exception_class"]),
                         ("UNKNOWN", "CREDENTIAL_LOAD", "BACKGROUND_CREDENTIAL_LOAD_FAILED",
                          "PERMISSION_ERROR"))
        self.assert_private(receipt)

    def test_credential_store_fixed_error_code_is_preserved(self):
        for code in ("CREDENTIAL_STORE_READ_FAILED", "CREDENTIAL_STORE_INVALID"):
            with self.subTest(code=code):
                child = self.root / ("credential-" + code.lower())
                def fail():
                    raise CredentialStoreError(code)
                result, receipt = self.run_at(Transport([]), root=child,
                                              chain_lock=child / "shared.lock", credential_loader=fail)
                self.assertEqual((result["status"], receipt["stage"], receipt["error_code"],
                                  receipt["exception_class"]),
                                 ("UNKNOWN", "CREDENTIAL_LOAD", code, "CREDENTIAL_STORE_ERROR"))
                self.assert_private(receipt)

    def test_mutated_credential_error_code_cannot_enter_receipt(self):
        error = CredentialStoreError("CREDENTIAL_STORE_READ_FAILED")
        error.code = SENTINEL
        def fail():
            raise error
        result, receipt = self.run_at(Transport([]), credential_loader=fail)
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"]),
                         ("UNKNOWN", "CREDENTIAL_LOAD", "BACKGROUND_CREDENTIAL_LOAD_FAILED"))
        self.assert_private(receipt)

    def test_mutated_source_error_code_cannot_enter_receipt(self):
        error = OneShotSourceError("ONE_SHOT_SOURCE_REQUEST_FAILED")
        error.code = SENTINEL
        def fail(*_):
            raise error
        result, receipt = self.run_at(Transport([]), source_fetch=fail)
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"]),
                         ("UNKNOWN", "SOURCE_FETCH", "BACKGROUND_SOURCE_FETCH_FAILED"))
        self.assert_private(receipt)

    def test_transport_builder_failure_is_localized(self):
        with patch.object(monitor, "_build_transport", side_effect=RuntimeError(SENTINEL)):
            result, receipt = self.run_at(None)
        self.assertEqual((result["status"], receipt["stage"], receipt["error_code"]),
                         ("UNKNOWN", "TRANSPORT_BUILD", "BACKGROUND_TRANSPORT_BUILD_FAILED"))
        self.assert_private(receipt)

    def test_root_storage_failure_returns_safe_status_without_receipt(self):
        blocked_root = self.root / "not-a-directory"
        blocked_root.write_text("fixture", encoding="utf-8")
        result, receipt = self.run_at(Transport([]), root=blocked_root)
        self.assertIsNone(receipt)
        self.assertEqual((result["status"], result["stage"], result["error_code"]),
                         ("FAILED", "ROOT_SETUP", "BACKGROUND_ROOT_SETUP_FAILED"))

    def test_claim_write_failure_returns_safe_status_without_receipt(self):
        original = Path.open
        def fail_claim(path, *args, **kwargs):
            if path.name.endswith(".attempt.json"):
                raise PermissionError(SENTINEL)
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", new=fail_claim):
            result, receipt = self.run_at(Transport([]))
        self.assertIsNone(receipt)
        self.assertEqual((result["status"], result["stage"], result["error_code"]),
                         ("FAILED", "CLAIM_WRITE", "BACKGROUND_CLAIM_WRITE_FAILED"))

    def test_receipt_init_failure_returns_safe_status_without_provider_call(self):
        original = monitor._save
        def fail_receipt(path, value):
            if path.name.endswith(".receipt.json"):
                raise PermissionError(SENTINEL)
            return original(path, value)
        with patch.object(monitor, "_save", side_effect=fail_receipt):
            result, receipt = self.run_at(Transport([]))
        self.assertIsNone(receipt)
        self.assertEqual((result["status"], result["stage"], result["error_code"]),
                         ("FAILED", "RECEIPT_INIT", "BACKGROUND_RECEIPT_INIT_FAILED"))

    def test_prior_source_failure_does_not_mask_recovery_model_failure(self):
        first_wire = self.wire()
        first_wire.responses.append(Response({"choices": [], "usage": {}}))
        first, first_receipt = self.run_at(first_wire)
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertTrue(first_receipt["persistence"]["source_journal_written"])
        later = NOW.replace(hour=5)
        second_wire = Transport([TimeoutError(SENTINEL), Response({"choices": [], "usage": {}})])
        second, receipt = self.run_at(second_wire, when=later)
        self.assertEqual((second["status"], receipt["stage"], receipt["error_code"]),
                         ("UNKNOWN", "MODEL_REQUEST", "BACKGROUND_MODEL_REQUEST_FAILED"))
        self.assertEqual((receipt["source_failure_stage"], receipt["source_error_code"],
                          receipt["source_exception_class"]),
                         ("ACTOR_POST_OPEN", "BACKGROUND_ACTOR_POST_OPEN_FAILED", "TIMEOUT_ERROR"))
        self.assertNotEqual(receipt["error_code"], receipt["source_error_code"])
        self.assert_private(receipt)

    def test_success_persists_request_and_stage_markers_without_raw_data(self):
        wire = self.wire()
        wire.responses.append(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        result, receipt = self.run_at(wire)
        self.assertEqual((result["status"], receipt["stage"]), ("SENT", "COMPLETE"))
        self.assertEqual(receipt["persistence"], {
            "receipt_initialized": True, "source_journal_written": True,
            "analysis_journal_written": True, "delivery_journal_written": True,
            "seen_written": True})
        requests = receipt["requests"]
        self.assertEqual(len(requests), 4)
        self.assertEqual([item["kind"] for item in requests],
                         ["ACTOR_POST"] + ["CONTEXT_GET"] * 3)
        self.assertEqual([item["context_index"] for item in requests], [None, 1, 2, 3])
        self.assertEqual([item["http_status"] for item in requests], [201, 200, 200, 200])
        self.assertEqual([item["state"] for item in requests], ["VALIDATED"] * 4)
        self.assertEqual(len({item["request_id"] for item in requests}), 4)
        self.assert_private(receipt)

    def test_untrusted_smtp_error_code_is_not_persisted(self):
        wire = self.wire()
        wire.responses.append(model_response(("POST_RESET", "IRRELEVANT", "PRE_RESET")))
        with patch.object(monitor.SMTPNotifier, "send", return_value=DeliveryResult(
                "FAILED", retryable=False, error_code=SENTINEL)):
            result, receipt = self.run_at(wire)
        self.assertEqual((result["status"], receipt["error_code"]),
                         ("FAILED", "BACKGROUND_SMTP_SEND_FAILED"))
        self.assert_private(receipt)


if __name__ == "__main__":
    unittest.main()
