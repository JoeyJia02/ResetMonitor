"""Offline candidate source boundary checks; no real token or service calls."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import Message
import getpass
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import warnings

from scripts import capture_first_three as probe


TEST_ROOT = (Path(__file__).resolve().parents[1] / "runtime" / "test-tmp").resolve()


def row(identifier, **changes):
    record = {
        "success": True, "status": "success", "inputType": "profile", "normalizedInput": "thsottiaux",
        "tweetId": str(identifier), "text": "Complete original text " + str(identifier) + "\n回复上下文不截断",
        "url": "https://x.com/thsottiaux/status/" + str(identifier),
        "createdAt": "2026-10-11T07:00:0" + str(identifier - 100) + "Z",
        "authorUsername": "thsottiaux", "authorId": probe.TARGET_USER_ID,
        "author": {"username": "thsottiaux", "id": probe.TARGET_USER_ID},
        "isReply": identifier == 102, "isRetweet": False, "retweetedTweet": None,
        "inReplyToTweetId": "99" if identifier == 102 else None,
        "inReplyToUsername": "someone" if identifier == 102 else None,
        "quotedTweet": {"text": "Complete quoted context"} if identifier == 102 else None,
    }
    record.update(changes)
    return record


class FakeResponse:
    def __init__(self, rows, *, total=None, status=201):
        self.body = rows if isinstance(rows, bytes) else json.dumps(rows, ensure_ascii=False).encode("utf-8")
        self.status = status
        self.headers = Message()
        self.headers["X-Apify-Pagination-Total"] = str(len(rows) if total is None else total)
        self.read_limits = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def getcode(self):
        return self.status

    def read(self, limit):
        self.read_limits.append(limit)
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


class SourceSetupProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket", "socket.create_connection"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("REAL_NETWORK_FORBIDDEN")))
        self.stack.enter_context(patch.object(probe.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
        self.stack.enter_context(patch.object(probe.sys, "stderr", SimpleNamespace(isatty=lambda: True)))
        self.workspace = TEST_ROOT / ("source-setup-" + uuid4().hex)
        self.workspace.mkdir(parents=True)
        self.addCleanup(self.cleanup_workspace)
        self.directory = self.workspace / "trial"
        self.secret = "SYNTHETIC_SOURCE_SETUP_TOKEN"
        self.output = io.StringIO()
        self.now = datetime(2026, 10, 11, 8, 30, tzinfo=timezone.utc)
        self.rows = [row(103), row(102), row(101)]
        self.response = FakeResponse(self.rows)
        self.transport = FakeTransport(self.response)

    def cleanup_workspace(self):
        resolved = self.workspace.resolve()
        if not resolved.is_relative_to(TEST_ROOT) or resolved == TEST_ROOT:
            raise AssertionError("UNSAFE_TEST_CLEANUP_PATH")
        shutil.rmtree(resolved)

    def run_probe(self, args=None, **overrides):
        options = dict(output=self.output, hidden_input=lambda _: self.secret,
                       confirm=lambda _: probe.CONFIRMATION, clock=lambda: self.now,
                       directory=self.directory, transport=self.transport)
        options.update(overrides)
        return probe.main(["--capture-one-run"] if args is None else args, **options)

    def assert_no_secret(self):
        self.assertNotIn(self.secret, self.output.getvalue())
        if self.directory.exists():
            for path in self.directory.iterdir():
                if path.is_file():
                    self.assertNotIn(self.secret.encode(), path.read_bytes())

    def test_preview_no_credentials_environment_state_files_or_network(self):
        with patch.object(probe, "_default_directory", side_effect=AssertionError("NO_ENV")), \
             patch.object(probe, "_consumed", side_effect=AssertionError("NO_STATE")), \
             patch.object(probe, "_build_transport", side_effect=AssertionError("NO_TRANSPORT")):
            for args in ([], ["--preview"], ["--help"]):
                self.assertEqual(self.run_probe(args, hidden_input=lambda _: self.fail("NO_SECRET")), 0)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.transport.calls, [])

    def test_invalid_args_non_tty_expiry_cancel_and_echo_fallback_never_send(self):
        self.assertEqual(self.run_probe([self.secret]), 2)
        for stream in ("stdin", "stderr"):
            with patch.object(probe.sys, stream, SimpleNamespace(isatty=lambda: False)):
                self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.run_probe(clock=lambda: probe.EXPIRES_AT, hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.run_probe(confirm=lambda _: "NO", hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        def fallback(_):
            warnings.warn(self.secret, getpass.GetPassWarning)
        self.assertEqual(self.run_probe(hidden_input=fallback), 2)
        self.assertEqual(self.run_probe(hidden_input=lambda _: "bad token\n"), 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.transport.calls, [])
        self.assert_no_secret()

    def test_fixed_request_reservation_before_wire_complete_snapshot_and_repeat_block(self):
        def before():
            attempt = json.loads(probe._path(self.directory, "attempt").read_text())
            self.assertEqual(attempt["reserved_attempts"], 1)
            self.assertEqual(attempt["provider_run_cap_usd"], "0.0009")
            self.assertFalse(attempt["full_chain_hard_cap_proven"])
        self.transport.before = before
        self.assertEqual(self.run_probe(), 0)
        self.assertEqual(len(self.transport.calls), 1)
        request, timeout = self.transport.calls[0]
        self.assertEqual(request.full_url, probe.URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"profiles": ["thsottiaux"], "maxResults": 3})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.secret)
        self.assertEqual(timeout, 90)
        route = urlsplit(request.full_url)
        self.assertEqual((route.scheme, route.netloc, route.path), (
            "https", "api.apify.com", "/v2/actors/dYOm3ceq9e9PBnyJA/run-sync-get-dataset-items"))
        self.assertEqual(parse_qs(route.query), {
            "build": ["1.0.29"], "maxTotalChargeUsd": ["0.0009"], "restartOnError": ["false"],
            "timeout": ["60"], "format": ["json"], "limit": ["3"], "offset": ["0"],
            "desc": ["false"], "clean": ["false"], "skipEmpty": ["false"], "skipHidden": ["false"],
        })
        snapshot = json.loads(probe._path(self.directory, "snapshot").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["source_order_latest_first"], ["103", "102", "101"])
        self.assertEqual([tweet["id"] for tweet in snapshot["tweets"]], ["101", "102", "103"])
        by_id = {tweet["id"]: tweet for tweet in snapshot["tweets"]}
        for source in self.rows:
            tweet = by_id[source["tweetId"]]
            self.assertEqual(tweet["text"], source["text"])
            self.assertEqual(tweet["author_id"], probe.TARGET_USER_ID)
            self.assertEqual(tweet["is_reply"], source["isReply"])
            self.assertFalse(tweet["is_retweet"])
        self.assertIn("Complete quoted context", by_id["102"]["context"])
        self.assertFalse(snapshot["monitor_baseline_initialized"])
        self.assertFalse(snapshot["production_source_contract_proven"])
        self.assertEqual(self.response.read_limits, [probe.MAX_RESPONSE_BYTES + 1])
        self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIn("CAPTURED n=3", self.output.getvalue())
        self.assert_no_secret()

    def test_total_above_three_or_mismatched_array_never_passes(self):
        for total in (4, 20, 2):
            with self.subTest(total=total):
                directory = self.workspace / ("invalid-total-" + str(total))
                transport = FakeTransport(FakeResponse(self.rows, total=total))
                self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
                receipt = json.loads(probe._path(directory, "receipt").read_text())
                self.assertEqual(receipt["status"], "FAILED")
                self.assertFalse(probe._path(directory, "snapshot").exists())
                self.assertFalse(probe._path(directory, "candidate").exists())
                self.assertEqual(len(transport.calls), 1)

    def test_retweet_identity_order_future_and_secret_reflection_are_rejected(self):
        cases = []
        for changes in ({"isRetweet": True}, {"authorId": "123"},
                        {"author": {"username": "thsottiaux", "id": "123"}},
                        {"authorUsername": "other"}, {"createdAt": "2026-10-11T09:00:00Z"},
                        {"text": self.secret}):
            rows = deepcopy(self.rows)
            rows[0].update(changes)
            cases.append(rows)
        cases.append(list(reversed(self.rows)))
        for index, rows in enumerate(cases):
            directory = self.workspace / ("rejected-" + str(index))
            transport = FakeTransport(FakeResponse(rows))
            self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
            self.assertFalse(probe._path(directory, "snapshot").exists())
            if rows[0].get("text") == self.secret:
                self.assertFalse(probe._path(directory, "candidate").exists())
            self.assertEqual(len(transport.calls), 1)
            for path in directory.iterdir():
                self.assertNotIn(self.secret.encode(), path.read_bytes())
        self.assert_no_secret()

    def test_rejected_schema_preserves_same_complete_bounded_rows_for_manual_review(self):
        rows = deepcopy(self.rows)
        rows[0].pop("authorId")
        transport = FakeTransport(FakeResponse(rows))
        self.assertEqual(self.run_probe(transport=transport), 1)
        candidate = json.loads(probe._path(self.directory, "candidate").read_text(encoding="utf-8"))
        self.assertEqual(candidate["status"], "REJECTED")
        self.assertEqual(candidate["candidate_rows"], rows)
        self.assertFalse(candidate["automatic_classification_allowed"])
        self.assertFalse(candidate["monitor_baseline_initialized"])
        self.assertFalse(candidate["production_source_contract_proven"])
        self.assertFalse(probe._path(self.directory, "snapshot").exists())
        receipt = json.loads(probe._path(self.directory, "receipt").read_text())
        self.assertEqual(receipt["status"], "FAILED")
        self.assertTrue(receipt["rejected_candidate_saved"])
        self.assertEqual(self.run_probe(transport=transport, hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(len(transport.calls), 1)
        self.assert_no_secret()

    def test_candidate_alone_blocks_repeated_run_and_malformed_json_is_not_saved(self):
        self.directory.mkdir()
        probe._path(self.directory, "candidate").write_text('{"status":"REJECTED"}', encoding="utf-8")
        self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.transport.calls, [])
        other = self.workspace / "malformed"
        transport = FakeTransport(FakeResponse(b"INVALID_SYNTHETIC_JSON", total=3))
        self.assertEqual(self.run_probe(directory=other, transport=transport), 1)
        self.assertFalse(probe._path(other, "candidate").exists())

    def test_fixed_rejection_codes_and_safe_numeric_diagnostics(self):
        missing = FakeResponse(self.rows)
        del missing.headers["X-Apify-Pagination-Total"]
        multiple = FakeResponse(self.rows)
        multiple.headers["X-Apify-Pagination-Total"] = self.secret
        invalid = FakeResponse(self.rows)
        invalid.headers.replace_header("X-Apify-Pagination-Total", self.secret)
        reflected = deepcopy(self.rows)
        reflected[0]["text"] = self.secret
        cases = (
            (missing, "TOTAL_HEADER_MISSING", {"total_header_present": False, "total_header_count": 0}),
            (multiple, "TOTAL_HEADER_MULTIPLE", {"total_header_count": 2}),
            (invalid, "TOTAL_HEADER_INVALID", {"total_header_count": 1}),
            (FakeResponse(self.rows, total=20), "TOTAL_EXCEEDS_THREE", {"parsed_total": 20}),
            (FakeResponse(b"x" * (probe.MAX_RESPONSE_BYTES + 1), total=3), "RESPONSE_TOO_LARGE",
             {"response_bytes_read": probe.MAX_RESPONSE_BYTES + 1}),
            (FakeResponse(b"INVALID_JSON", total=3), "JSON_INVALID", {"json_valid": False}),
            (FakeResponse({}, total=0), "JSON_ROOT_INVALID", {"json_root_type": "object"}),
            (FakeResponse(self.rows, total=2), "TOTAL_COUNT_MISMATCH", {"json_array_items": 3, "parsed_total": 2}),
            (FakeResponse(reflected), "TOKEN_REFLECTION", {"secret_reflection_detected": True}),
        )
        for index, (response, suffix, expected) in enumerate(cases):
            directory = self.workspace / ("diagnostics-" + str(index))
            transport = FakeTransport(response)
            self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
            receipt = json.loads(probe._path(directory, "receipt").read_text())
            self.assertEqual(receipt["error_code"], "SOURCE_TRIAL_" + suffix)
            self.assertEqual(receipt["diagnostics"]["http_status"], 201)
            for key, value in expected.items():
                self.assertEqual(receipt["diagnostics"][key], value)
            self.assertFalse(receipt["rejected_candidate_saved"])
            self.assertEqual(len(transport.calls), 1)
            for path in directory.iterdir():
                self.assertNotIn(self.secret.encode(), path.read_bytes())
        self.assert_no_secret()

    def test_observed_null_username_and_url_shape_is_semantic_rejection_with_candidate(self):
        rows = deepcopy(self.rows)
        rows[0].update(authorUsername=None, url=None, isQuote=True, quotedTweet=None)
        rows[0]["author"]["username"] = None
        self.assertEqual(self.run_probe(transport=FakeTransport(FakeResponse(rows))), 1)
        receipt = json.loads(probe._path(self.directory, "receipt").read_text())
        self.assertEqual(receipt["error_code"], "SOURCE_TRIAL_SOURCE_SCHEMA_REJECTED")
        self.assertFalse(receipt["diagnostics"]["source_schema_accepted"])
        self.assertTrue(receipt["rejected_candidate_saved"])
        self.assertFalse(probe._path(self.directory, "snapshot").exists())
        self.assert_no_secret()

    def test_network_unknown_408_5xx_and_explicit_4xx_never_retry(self):
        for index, (failure, status) in enumerate((
                (URLError(self.secret), "UNKNOWN"),
                (HTTPError(probe.URL, 408, self.secret, {}, io.BytesIO(self.secret.encode())), "UNKNOWN"),
                (FakeResponse({}, status=503), "UNKNOWN"),
                (HTTPError(probe.URL, 403, self.secret, {}, io.BytesIO(self.secret.encode())), "FAILED"))):
            directory = self.workspace / ("error-" + str(index))
            transport = FakeTransport(failure)
            self.assertEqual(self.run_probe(directory=directory, transport=transport), 1)
            receipt = json.loads(probe._path(directory, "receipt").read_text())
            self.assertEqual(receipt["status"], status)
            self.assertEqual(self.run_probe(directory=directory, transport=transport,
                                            hidden_input=lambda _: self.fail("NO_SECRET")), 2)
            self.assertEqual(len(transport.calls), 1)
        self.assert_no_secret()

    def test_empty_is_saved_without_identity_or_baseline_claim(self):
        self.assertEqual(self.run_probe(transport=FakeTransport(FakeResponse([]))), 0)
        snapshot = json.loads(probe._path(self.directory, "snapshot").read_text())
        self.assertEqual(snapshot["tweets"], [])
        self.assertFalse(snapshot["observed_author_identity"])
        self.assertFalse(snapshot["monitor_baseline_initialized"])
        self.assertIn("EMPTY n=0", self.output.getvalue())

    def test_reservation_and_snapshot_write_failure_keep_safe_hold(self):
        with patch.object(probe, "_reserve", side_effect=OSError(self.secret)):
            self.assertEqual(self.run_probe(), 2)
        self.assertEqual(self.transport.calls, [])
        with patch.object(probe, "_save_json", side_effect=OSError(self.secret)):
            self.assertEqual(self.run_probe(), 1)
        self.assertTrue(probe._path(self.directory, "attempt").exists())
        self.assertEqual(self.run_probe(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(len(self.transport.calls), 1)
        self.assert_no_secret()

    def test_expiry_after_reservation_and_oversize_response_never_pass(self):
        ticks = iter([self.now, self.now, probe.EXPIRES_AT + timedelta(seconds=1), self.now])
        self.assertEqual(self.run_probe(clock=lambda: next(ticks)), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(probe._path(self.directory, "attempt").exists())
        oversized = FakeResponse(b"x" * (probe.MAX_RESPONSE_BYTES + 1), total=3)
        directory = self.workspace / "oversized"
        self.assertEqual(self.run_probe(directory=directory, transport=FakeTransport(oversized)), 1)
        self.assertFalse(probe._path(directory, "snapshot").exists())
        self.assertFalse(probe._path(directory, "candidate").exists())
        self.assertEqual(oversized.read_limits, [probe.MAX_RESPONSE_BYTES + 1])


if __name__ == "__main__":
    unittest.main()
