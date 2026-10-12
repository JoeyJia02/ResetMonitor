"""Offline fixed-sample classification boundaries; no actual model requests."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4
import warnings

from scripts import analyze_recovered_three as probe


TEST_ROOT = (Path(__file__).resolve().parents[1] / "runtime" / "test-tmp").resolve()


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def getcode(self):
        return self.status

    def read(self, limit):
        return self.body[:limit]


class FakeTransport:
    def __init__(self, action, before=None):
        self.action, self.before, self.calls = action, before, []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.before:
            self.before()
        if isinstance(self.action, BaseException):
            raise self.action
        return self.action

class FakeApprovedBudget:
    """Synthetic financial permission for isolated response-contract tests.

    Real cumulative accounting is exercised in test_manual_model_budget.
    """
    def preflight(self, maximum_cny):
        pass
    def inspect(self, maximum_cny):
        pass
    def reserve(self, maximum_cny, *, request_sha256):
        pass


def response_document():
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
        "results": [{"id": identifier, "label": "IRRELEVANT", "reset_time": None, "reason": "没有确认额度重置；发生时间不明。"}
                    for identifier in ("1", "2", "3")]})}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 160, "total_tokens": 1360},
        "ignored": "PRIVATE_PROVIDER_EXTRA"}


class ClassificationSetupProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket", "socket.create_connection"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("REAL_NETWORK_FORBIDDEN")))
        self.stack.enter_context(patch.object(probe.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
        self.stack.enter_context(patch.object(probe.sys, "stderr", SimpleNamespace(isatty=lambda: True)))
        self.workspace = TEST_ROOT / ("classification-setup-" + uuid4().hex)
        self.workspace.mkdir(parents=True)
        self.addCleanup(self.cleanup_workspace)
        self.directory = self.workspace / "trial"
        self.output = io.StringIO()
        self.key = "SYNTHETIC_CLASSIFICATION_KEY"
        self.now = datetime(2026, 10, 11, 9, tzinfo=timezone.utc)
        self.sample = {"schema_version": 1, "status": "SAMPLE", "tweets": [{
            "id": identifier, "author": "thsottiaux", "author_id": probe.TARGET_USER_ID,
            "text": "完整原文，不能截断。" + identifier, "created_at": "2026-10-11T07:00:00Z",
            "url": "https://x.com/thsottiaux/status/" + identifier,
            "context": {"quoted_tweet": {"text": "完整引用上下文", "id": "999",
                                          "created_at": "2026-10-10T23:00:00Z",
                                          "author": {"username": "CONTEXT_AUTHOR"}, "like_count": 314},
                        "poll": {"options": ["yes", "no"], "counts_are_final": False},
                        "is_reply": False, "like_count": 99, "provenance": "LOCAL_PROVENANCE"},
        } for identifier in probe.INPUT_IDS]}
        self.sample_raw = json.dumps(self.sample, ensure_ascii=False).encode("utf-8")
        self.stack.enter_context(patch.object(probe, "SAMPLE_SHA256", hashlib.sha256(self.sample_raw).hexdigest()))
        self.transport = FakeTransport(FakeResponse(response_document()))

    def cleanup_workspace(self):
        resolved = self.workspace.resolve()
        if not resolved.is_relative_to(TEST_ROOT) or resolved == TEST_ROOT:
            raise AssertionError("UNSAFE_TEST_CLEANUP_PATH")
        shutil.rmtree(resolved)

    def run_probe(self, args=None, **overrides):
        options = dict(output=self.output, hidden_input=lambda _: self.key, confirm=lambda _: probe.CONFIRMATION,
                       clock=lambda: self.now, directory=self.directory, transport=self.transport,
                       sample_loader=lambda: self.sample_raw, model_budget=FakeApprovedBudget())
        options.update(overrides)
        return probe.main(["--analyze-one-batch"] if args is None else args, **options)

    def assert_no_secret(self):
        self.assertNotIn(self.key, self.output.getvalue())
        if self.directory.exists():
            for path in self.directory.iterdir():
                self.assertNotIn(self.key.encode(), path.read_bytes())
                self.assertNotIn(b"PRIVATE_PROVIDER_EXTRA", path.read_bytes())

    def test_preview_no_sample_secret_env_state_or_network(self):
        with patch.object(probe, "_directory", side_effect=AssertionError("NO_ENV")), \
             patch.object(probe, "_consumed", side_effect=AssertionError("NO_STATE")), \
             patch.object(probe, "_build_transport", side_effect=AssertionError("NO_TRANSPORT")):
            for args in ([], ["--preview"], ["--help"]):
                self.assertEqual(self.run_probe(args, sample_loader=lambda: self.fail("NO_SAMPLE"),
                                                hidden_input=lambda _: self.fail("NO_SECRET")), 0)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.transport.calls, [])

    def test_invalid_args_tty_expiry_cancel_echo_fallback_are_closed(self):
        self.assertEqual(self.run_probe([self.key]), 2)
        for stream in ("stdin", "stderr"):
            with patch.object(probe.sys, stream, SimpleNamespace(isatty=lambda: False)):
                self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.run_probe(clock=lambda: probe.EXPIRES_AT, hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.run_probe(confirm=lambda _: "NO", hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        def fallback(_):
            warnings.warn(self.key, getpass.GetPassWarning)
        self.assertEqual(self.run_probe(hidden_input=fallback), 2)
        self.assertEqual(self.transport.calls, [])
        self.assert_no_secret()

    def test_hash_schema_ids_and_oversize_block_before_secret_and_io(self):
        self.assertEqual(self.run_probe(sample_loader=lambda: b"{}", hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        for change in ("status", "id", "author", "large"):
            sample = deepcopy(self.sample)
            if change == "status":
                sample["status"] = "CAPTURED"
            elif change == "id":
                sample["tweets"][0]["id"] = sample["tweets"][1]["id"]
            elif change == "author":
                sample["tweets"][0]["author_id"] = "123"
            else:
                sample["tweets"][0]["text"] = "x" * 20000
            raw = json.dumps(sample).encode("utf-8")
            with patch.object(probe, "SAMPLE_SHA256", hashlib.sha256(raw).hexdigest()):
                self.assertEqual(self.run_probe(sample_loader=lambda: raw, hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.directory.exists())

    def test_one_post_analyzes_all_complete_three_and_reserves_before_wire(self):
        def before():
            attempt = json.loads(probe._path(self.directory, "attempt").read_text())
            self.assertEqual(attempt["attempts"], 1)
            self.assertEqual(attempt["reserved_maximum_cny"], "2.105344")
        self.transport.before = before
        self.assertEqual(self.run_probe(), 0)
        self.assertEqual(len(self.transport.calls), 1)
        request, timeout = self.transport.calls[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
        value = json.loads(request.data)
        self.assertEqual(value["model"], "deepseek-flash")
        self.assertEqual(value["max_tokens"], 1024)
        self.assertEqual(value["thinking"], {"type": "disabled"})
        self.assertEqual(value["response_format"], {"type": "json_object"})
        self.assertIs(value["stream"], False)
        submitted = json.loads(value["messages"][1]["content"])
        self.assertEqual(set(submitted), {"observed_at", "timezone", "tweets"})
        self.assertEqual(submitted["tweets"], [{
            "id": str(index), "created_at": row["created_at"], "text": row["text"],
            "quoted_tweet": {"text": row["context"]["quoted_tweet"]["text"],
                             "created_at": "2026-10-10T23:00:00Z"},
            "poll_options": ["yes", "no"],
        } for index, row in enumerate(self.sample["tweets"], 1)])
        self.assertNotIn("人工核对", value["messages"][0]["content"])
        for excluded in ("source_status", "source_current_three_user_verified", "author", "url",
                         "is_reply", "like_count", "provenance", "CONTEXT_AUTHOR", "LOCAL_PROVENANCE"):
            self.assertNotIn(excluded, value["messages"][1]["content"])
        self.assertLessEqual(len(request.data), 16 * 1024)
        result = json.loads(probe._path(self.directory, "result").read_text(encoding="utf-8"))
        self.assertEqual({row["id"] for row in result["results"]}, set(probe.INPUT_IDS))
        self.assertEqual(result["usage_cost_upper_bound_cny"], "0.00368")
        self.assertTrue(result["source_current_three_user_verified"])
        self.assertFalse(result["monitor_baseline_initialized"])
        self.assertFalse(result["notification_sent"])
        self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET"), sample_loader=lambda: self.fail("NO_SAMPLE")), 2)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIn("BATCH_ANALYZED n=3", self.output.getvalue())
        self.assert_no_secret()

    def test_original_tweet_ids_returned_by_model_are_rejected_without_retry(self):
        document = response_document()
        content = json.loads(document["choices"][0]["message"]["content"])
        for row, original in zip(content["results"], probe.INPUT_IDS):
            row["id"] = original
        document["choices"][0]["message"]["content"] = json.dumps(content)
        transport = FakeTransport(FakeResponse(document))
        self.assertEqual(self.run_probe(transport=transport), 1)
        self.assertFalse(probe._path(self.directory, "result").exists())
        receipt = json.loads(probe._path(self.directory, "receipt").read_text())
        self.assertEqual(receipt["error_code"], "CLASSIFICATION_RESULTS_IDS_INVALID")
        self.assertEqual(len(transport.calls), 1)

    def test_usage_missing_invalid_bool_context_output_and_total_rejected(self):
        for index, usage in enumerate((None, {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
                                      {"prompt_tokens": 1, "completion_tokens": 1025, "total_tokens": 1026},
                                      {"prompt_tokens": 1048576, "completion_tokens": 1, "total_tokens": 1048577},
                                      {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3})):
            document = response_document()
            document["usage"] = usage
            directory = self.workspace / ("usage-" + str(index))
            transport = FakeTransport(FakeResponse(document))
            self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
            self.assertFalse(probe._path(directory, "result").exists())
            self.assertEqual(len(transport.calls), 1)

    def test_result_ids_bool_short_reason_and_secret_reflection_rejected(self):
        for index, change in enumerate(("duplicate", "missing", "label", "reason", "secret")):
            document = response_document()
            content = json.loads(document["choices"][0]["message"]["content"])
            rows = content["results"]
            if change == "duplicate":
                rows[0]["id"] = rows[1]["id"]
            elif change == "missing":
                rows.pop()
            elif change == "label":
                rows[0]["label"] = True
            elif change == "reason":
                rows[0]["reason"] = "x" * 201
            else:
                rows[0]["reason"] = self.key
            document["choices"][0]["message"]["content"] = json.dumps(content)
            directory = self.workspace / ("result-" + str(index))
            transport = FakeTransport(FakeResponse(document))
            self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
            self.assertFalse(probe._path(directory, "result").exists())
            for path in directory.iterdir():
                self.assertNotIn(self.key.encode(), path.read_bytes())
        self.assert_no_secret()

    def test_http_network_uncertainty_and_failure_hold_without_retry(self):
        for index, (action, expected) in enumerate((
                (URLError(self.key), "UNKNOWN"), (FakeResponse({}, 503), "UNKNOWN"),
                (HTTPError(probe.URL, 408, self.key, {}, io.BytesIO(self.key.encode())), "UNKNOWN"),
                (HTTPError(probe.URL, 401, self.key, {}, io.BytesIO(self.key.encode())), "FAILED"))):
            directory = self.workspace / ("network-" + str(index))
            transport = FakeTransport(action)
            self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
            receipt = json.loads(probe._path(directory, "receipt").read_text())
            self.assertEqual(receipt["status"], expected)
            self.assertEqual(self.run_probe(directory=directory, transport=transport,
                                            hidden_input=lambda _: self.fail("NO_SECRET")), 2)
            self.assertEqual(len(transport.calls), 1)
            for path in directory.iterdir():
                self.assertNotIn(self.key.encode(), path.read_bytes())

    def test_reservation_write_failure_no_io_and_result_write_failure_no_repeat(self):
        with patch.object(probe, "_save", side_effect=OSError(self.key)):
            self.assertEqual(self.run_probe(), 2)
        self.assertEqual(self.transport.calls, [])
        original = probe._save
        def fail_result(path, record, **kwargs):
            if path == probe._path(self.directory, "result"):
                raise OSError(self.key)
            return original(path, record, **kwargs)
        with patch.object(probe, "_save", side_effect=fail_result):
            self.assertEqual(self.run_probe(), 1)
        self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(len(self.transport.calls), 1)
        self.assert_no_secret()

    def test_rejected_usage_preserves_bounded_assistant_candidate_not_provider_body(self):
        document = response_document()
        document.pop("usage")
        self.assertEqual(self.run_probe(transport=FakeTransport(FakeResponse(document))), 1)
        candidate = json.loads(probe._path(self.directory, "candidate").read_text(encoding="utf-8"))
        self.assertEqual(candidate["status"], "REJECTED")
        self.assertEqual(candidate["message_content"], document["choices"][0]["message"]["content"])
        self.assertFalse(candidate["automatic_notification_allowed"])
        self.assertFalse(candidate["monitor_baseline_initialized"])
        self.assertNotIn("usage", candidate)
        receipt = json.loads(probe._path(self.directory, "receipt").read_text())
        self.assertEqual(receipt["error_code"], "CLASSIFICATION_USAGE_INVALID")
        self.assertTrue(receipt["rejected_candidate_saved"])
        self.assertIn("assistant_content_bytes", receipt["diagnostics"])
        self.assert_no_secret()

    def test_malformed_content_and_escaped_secret_have_fixed_codes_without_candidate(self):
        cases = (("not json", "CLASSIFICATION_CONTENT_JSON_INVALID"),
                 ('{"results":[{"reason":"' + ''.join('\\u%04x' % ord(char) for char in self.key) + '"}]}',
                  "CLASSIFICATION_CONTENT_TOKEN_REFLECTION"))
        for index, (content, code) in enumerate(cases):
            directory = self.workspace / ("content-" + str(index))
            document = response_document()
            document["choices"][0]["message"]["content"] = content
            self.assertEqual(self.run_probe(directory=directory, transport=FakeTransport(FakeResponse(document))), 1)
            receipt = json.loads(probe._path(directory, "receipt").read_text())
            self.assertEqual(receipt["error_code"], code)
            self.assertFalse(receipt["rejected_candidate_saved"])
            self.assertFalse(probe._path(directory, "candidate").exists())
            for path in directory.iterdir():
                self.assertNotIn(self.key.encode(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
