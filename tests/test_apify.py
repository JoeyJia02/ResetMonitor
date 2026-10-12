"""Paid-source recovery tests: fake HTTP only, no account or environment keys."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from monitor.apify import ACTOR, ApifyError, ApifyFetcher, ApifyPolicy, validate_active_fetch
from config import Config
from monitor.engine import Monitor, RunResult
from monitor.fakes import FakeClassifier, FakeNotifier
from monitor.http_transport import HttpResponse, StdlibHTTPTransport
from monitor.models import FetchSnapshot, StageFailure
from monitor.state import StateError, StateStore, initial_state
from monitor.source_contract import SourceContract


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime" / "test-tmp").resolve()
NOW = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
RUN_ID, DATASET_ID = "R" * 17, "D" * 17
TOKEN = "offline-fake-apify-token"


def response(status, payload, headers=None):
    return HttpResponse(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers or {})


def started():
    return response(201, {"data": {"id": RUN_ID, "status": "RUNNING", "defaultDatasetId": DATASET_ID}})


def run(status="SUCCEEDED", **changes):
    data = {"id": RUN_ID, "status": status, "defaultDatasetId": DATASET_ID}
    data.update(changes)
    return response(200, {"data": data})


def tweet(identifier=101, **changes):
    row = {
        "inputType": "profile", "inputValue": "thsottiaux", "normalizedInput": "thsottiaux",
        "success": True, "status": "success", "tweetId": str(identifier),
        "text": f"Original tweet {identifier}", "createdAt": f"2026-10-10T08:{identifier % 100:02d}:00Z",
        "url": f"https://x.com/thsottiaux/status/{identifier}", "authorUsername": "thsottiaux",
        "author": {"username": "thsottiaux", "id": "12345"},
        "isReply": False, "isRetweet": False, "retweetedTweet": None, "quotedTweet": None,
    }
    row.update(changes)
    return row


def page(rows, *, offset=0, total=None, limit=3, **header_changes):
    headers = {
        "X-Apify-Pagination-Offset": str(offset), "X-Apify-Pagination-Limit": str(limit),
        "X-Apify-Pagination-Count": str(len(rows)), "X-Apify-Pagination-Total": str(len(rows) if total is None else total),
        "X-Apify-Pagination-Desc": "false",
    }
    headers.update(header_changes)
    return response(200, rows, headers)


def approved_policy():
    # Explicit artificial acceptance facts, not a verified production contract.
    return ApifyPolicy(approved=True, available_balance_usd=Decimal("1"),
                       max_total_charge_usd=Decimal("0.001"), minimum_charge_cap_usd=Decimal("0.001"),
                       per_result_usd=Decimal("0.0003"), pricing_verified=True, source_contract_verified=True)


class FakeHTTP:
    def __init__(self, *actions):
        self.actions, self.calls = list(actions), []

    def request(self, method, url, *, headers, body, timeout):
        call = {"method": method, "url": url, "headers": deepcopy(headers), "body": body, "timeout": timeout}
        self.calls.append(call)
        if not self.actions:
            raise AssertionError("Unexpected fake HTTP request")
        action = self.actions.pop(0)
        if callable(action):
            return action(call)
        if isinstance(action, BaseException):
            raise action
        return action


class Crash(BaseException):
    pass


class ApifyTests(unittest.TestCase):
    def setUp(self):
        self.directory = TEST_ROOT / ("apify-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.store = StateStore(self.directory / "state.json")
        self.state = initial_state("thsottiaux")
        self.stdlib_request = StdlibHTTPTransport.request
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("Network forbidden")).start()
        patch("socket.socket", side_effect=AssertionError("Network forbidden")).start()
        patch("monitor.http_transport.StdlibHTTPTransport.request", side_effect=AssertionError("Real HTTP forbidden")).start()

    def cleanup(self):
        path, intended = self.directory.resolve(), TEST_ROOT.resolve()
        if path == intended or path.parent != intended:
            raise RuntimeError("Refusing cleanup outside the isolated test root")
        shutil.rmtree(path)

    def fetcher(self, http, *, state=None, persist=None, policy=None):
        fetcher = ApifyFetcher(api_token=TOKEN, transport=http, policy=policy or approved_policy(), clock=lambda: NOW)
        fetcher.bind_state(self.state if state is None else state, persist or self.store.save)
        return fetcher

    def happy(self, rows=None):
        return FakeHTTP(started(), run(), page([tweet(103), tweet(102), tweet(101)] if rows is None else rows))

    def test_default_unapproved_policy_never_starts_http(self):
        http = FakeHTTP()
        fetcher = ApifyFetcher(api_token=TOKEN, transport=http)
        fetcher.bind_state(self.state, self.store.save)
        with self.assertRaises(ApifyError) as caught:
            fetcher.fetch_latest(limit=3)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(http.calls, [])
        self.assertIsNone(self.state["active_fetch"])
        self.assertFalse(self.store.path.exists())

    def test_unknown_or_insufficient_budget_and_source_facts_block_post(self):
        cases = (
            {"available_balance_usd": None}, {"minimum_charge_cap_usd": None}, {"per_result_usd": None},
            {"pricing_verified": False}, {"source_contract_verified": False},
            {"max_total_charge_usd": "0.0008"}, {"minimum_charge_cap_usd": "0.002"},
            {"available_balance_usd": "0.0009"}, {"max_total_charge_usd": True},
            {"available_balance_usd": float("nan")}, {"per_result_usd": "Infinity"},
        )
        for changes in cases:
            http = FakeHTTP()
            fetcher = self.fetcher(http, policy=replace(approved_policy(), **changes))
            with self.subTest(changes=changes), self.assertRaises(ApifyError):
                fetcher.fetch_latest(limit=3)
            self.assertEqual(http.calls, [])
            self.assertIsNone(self.state["active_fetch"])

    def test_binding_and_three_result_limit_are_mandatory(self):
        http = FakeHTTP()
        fetcher = ApifyFetcher(api_token=TOKEN, transport=http, policy=approved_policy())
        with self.assertRaises(ApifyError):
            fetcher.fetch_latest(limit=3)
        fetcher.bind_state(self.state, self.store.save)
        for limit in (20, 1, True, 3.0):
            with self.subTest(limit=limit), self.assertRaises(ApifyError):
                fetcher.fetch_latest(limit=limit)
        self.assertEqual(http.calls, [])

    def test_token_target_and_timeout_errors_do_not_echo_credentials(self):
        for changes in ({"api_token": "fake\r\nsecret"}, {"api_token": ""}, {"target": "wrong/target"}, {"timeout": 61}, {"timeout": True}):
            kwargs = {"api_token": TOKEN, "transport": FakeHTTP(), **changes}
            with self.subTest(fields=list(changes)), self.assertRaises(ApifyError) as caught:
                ApifyFetcher(**kwargs)
            self.assertNotIn("secret", str(caught.exception))

    def test_starting_is_durable_before_post_and_token_only_in_header(self):
        def before_post(call):
            durable = self.store.load()["active_fetch"]
            self.assertEqual(durable["phase"], "STARTING")
            self.assertIsNone(durable["run_id"])
            self.assertEqual(json.loads(call["body"]), {"profiles": ["thsottiaux"], "maxResults": 3})
            return started()
        http = FakeHTTP(before_post, run(), page([tweet()]))
        snapshot = self.fetcher(http).fetch_latest(limit=3)
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.tweets[0]["id"], "101")
        for call in http.calls:
            self.assertEqual(urlsplit(call["url"]).hostname, "api.apify.com")
            self.assertNotIn(TOKEN, call["url"])
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        query = parse_qs(urlsplit(http.calls[0]["url"]).query)
        self.assertEqual(query["maxTotalChargeUsd"], ["0.001"])
        self.assertEqual(query["restartOnError"], ["false"])
        self.assertNotIn(TOKEN, self.store.path.read_text(encoding="utf-8"))

    def test_starting_persistence_failure_prevents_post(self):
        http = FakeHTTP()
        def reject(_state):
            raise StateError("Injected persistence failure")
        fetcher = self.fetcher(http, persist=reject)
        with self.assertRaises(StateError):
            fetcher.fetch_latest(limit=3)
        self.assertEqual(http.calls, [])

    def test_post_ambiguity_persists_unknown_and_never_reposts(self):
        cases = (
            TimeoutError(), response(500, {"error": {"message": "sensitive fake server detail"}}),
            response(200, {"error": {"type": "failed"}}), response(201, {"data": {"id": "bad/id", "status": "RUNNING"}}),
            HttpResponse(201, b"not json"), response(201, {"data": {"id": RUN_ID}}),
        )
        for action in cases:
            with self.subTest(kind=type(action).__name__):
                state = initial_state("thsottiaux")
                durable = []
                http = FakeHTTP(action)
                fetcher = self.fetcher(http, state=state, persist=lambda value: durable.append(deepcopy(value)))
                with self.assertRaises(ApifyError) as caught:
                    fetcher.fetch_latest(limit=3)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(state["active_fetch"]["phase"], "FETCH_UNKNOWN")
                restored, resumed_http = deepcopy(durable[-1]), FakeHTTP()
                resumed = self.fetcher(resumed_http, state=restored, persist=lambda _state: None)
                with self.assertRaises(ApifyError):
                    resumed.fetch_latest(limit=3)
                self.assertEqual(resumed_http.calls, [])

    def test_crash_after_remote_post_before_run_id_save_becomes_unknown(self):
        http = FakeHTTP(Crash())
        with self.assertRaises(Crash):
            self.fetcher(http).fetch_latest(limit=3)
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "STARTING")
        resumed_http = FakeHTTP()
        with self.assertRaises(ApifyError):
            self.fetcher(resumed_http, state=self.store.load()).fetch_latest(limit=3)
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "FETCH_UNKNOWN")
        self.assertEqual(resumed_http.calls, [])

    def test_known_run_restart_performs_get_only(self):
        def save_and_crash(state):
            self.store.save(state)
            if state["active_fetch"]["phase"] == "RUNNING":
                raise Crash()
        original_http = FakeHTTP(started())
        with self.assertRaises(Crash):
            self.fetcher(original_http, persist=save_and_crash).fetch_latest(limit=3)
        resumed_http = FakeHTTP(run(), page([tweet()]))
        snapshot = self.fetcher(resumed_http, state=self.store.load(), policy=ApifyPolicy()).fetch_latest(limit=3)
        self.assertEqual(snapshot.tweets[0]["id"], "101")
        self.assertEqual([call["method"] for call in resumed_http.calls], ["GET", "GET"])

    def test_pending_run_and_transient_get_recover_without_new_post(self):
        http = FakeHTTP(started(), run("RUNNING"), response(503, {"error": {"type": "temporary"}}), run(), page([tweet()]))
        fetcher = self.fetcher(http)
        for _ in range(2):
            with self.assertRaises(ApifyError) as caught:
                fetcher.fetch_latest(limit=3)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(self.store.load()["active_fetch"]["phase"], "RUNNING")
        self.assertEqual(fetcher.fetch_latest(limit=3).tweets[0]["id"], "101")
        self.assertEqual(sum(call["method"] == "POST" for call in http.calls), 1)

    def test_permanent_get_transport_and_configuration_errors_pause_known_run(self):
        cases = (StageFailure(retryable=False), ValueError("sensitive-fake-detail"), TypeError("sensitive-fake-detail"))
        for failure in cases:
            state, durable = initial_state("thsottiaux"), []
            http = FakeHTTP(started(), failure)
            fetcher = self.fetcher(http, state=state, persist=lambda value: durable.append(deepcopy(value)))
            with self.subTest(failure=type(failure).__name__), self.assertRaises(ApifyError) as caught:
                fetcher.fetch_latest(limit=3)
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("sensitive-fake-detail", str(caught.exception))
            self.assertEqual((durable[-1]["active_fetch"]["phase"], durable[-1]["active_fetch"]["run_id"]), ("PAUSED", RUN_ID))
            resumed_http = FakeHTTP()
            with self.assertRaises(ApifyError):
                self.fetcher(resumed_http, state=deepcopy(durable[-1]), persist=lambda _state: None).fetch_latest(limit=3)
            self.assertEqual(resumed_http.calls, [])

    def test_retryable_get_stage_failure_keeps_known_run(self):
        http = FakeHTTP(started(), StageFailure(retryable=True, retry_after_seconds=23), run(), page([tweet()]))
        fetcher = self.fetcher(http)
        with self.assertRaises(ApifyError) as caught:
            fetcher.fetch_latest(limit=3)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after_seconds, 23)
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "RUNNING")
        self.assertEqual(len(fetcher.fetch_latest(limit=3).tweets), 1)
        self.assertEqual(sum(call["method"] == "POST" for call in http.calls), 1)

    def test_get_retry_after_preserves_seconds_dates_and_long_waits(self):
        for status in (429, 503):
            for value, expected in (("0", 0.0), ("35", 35.0), ("10800", 10800.0),
                                    ("999999999999999", 999999999999999.0),
                                    ("Sat, 10 Oct 2026 08:00:35 GMT", 35.0),
                                    ("Sat, 10 Oct 2026 07:00:00 GMT", 0.0)):
                state = initial_state("thsottiaux")
                http = FakeHTTP(started(), response(status, {}, {"rEtRy-AfTeR": value}))
                with self.subTest(status=status, value=value), self.assertRaises(ApifyError) as caught:
                    self.fetcher(http, state=state, persist=lambda _state: None).fetch_latest(limit=3)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.retry_after_seconds, expected)
                self.assertEqual((state["active_fetch"]["phase"], state["active_fetch"]["run_id"]), ("RUNNING", RUN_ID))
                self.assertEqual([call["method"] for call in http.calls], ["POST", "GET"])

    def test_get_retry_after_ignores_invalid_ambiguous_and_unrelated_headers(self):
        cases = (
            {}, {"Retry-After": "-1"}, {"Retry-After": "+1"}, {"Retry-After": "1.5"},
            {"Retry-After": "NaN"}, {"Retry-After": "Infinity"}, {"Retry-After": ""},
            {"Retry-After": "fake-sensitive-detail\r\nInjected: value"},
            {"Retry-After": "10", "retry-after": "20"}, {"Retry-After": 10},
            {"Retry-After": "Sat, 99 Oct 2026 08:00:00 GMT"},
        )
        for headers in cases:
            state = initial_state("thsottiaux")
            http = FakeHTTP(started(), response(429, {}, headers))
            with self.subTest(headers=headers), self.assertRaises(ApifyError) as caught:
                self.fetcher(http, state=state, persist=lambda _state: None).fetch_latest(limit=3)
            self.assertTrue(caught.exception.retryable)
            self.assertIsNone(caught.exception.retry_after_seconds)
            self.assertNotIn("fake-sensitive-detail", str(caught.exception))
        state = initial_state("thsottiaux")
        with self.assertRaises(ApifyError) as caught:
            self.fetcher(FakeHTTP(started(), response(500, {}, {"Retry-After": "30"})),
                         state=state, persist=lambda _state: None).fetch_latest(limit=3)
        self.assertIsNone(caught.exception.retry_after_seconds)

    def test_stdlib_oversized_get_response_permanently_pauses_without_network(self):
        with self.assertRaises(ApifyError):
            self.fetcher(FakeHTTP(started(), run("RUNNING"))).fetch_latest(limit=3)
        calls = []
        class OversizedResponse:
            status = 200
            def getheaders(self):
                return []
            def read(self, _size):
                return b"x" * 65
        class Connection:
            def request(self, method, path, **_kwargs):
                calls.append((method, path))
            def getresponse(self):
                return OversizedResponse()
            def close(self):
                pass
        stdlib = StdlibHTTPTransport(connection_factory=lambda *_args, **_kwargs: Connection(), max_response_bytes=64)
        original = self.stdlib_request
        class InjectedStdlib:
            def request(self, *args, **kwargs):
                return original(stdlib, *args, **kwargs)
        resumed = self.fetcher(InjectedStdlib(), state=self.store.load())
        with patch("monitor.http_transport.ssl.create_default_context", return_value=object()), self.assertRaises(ApifyError) as caught:
            resumed.fetch_latest(limit=3)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "PAUSED")
        self.assertEqual(calls, [("GET", "/v2/actor-runs/" + RUN_ID)])

    def test_failed_run_is_preserved_and_permanently_paused(self):
        http = FakeHTTP(started(), run("FAILED"))
        fetcher = self.fetcher(http)
        with self.assertRaises(ApifyError) as caught:
            fetcher.fetch_latest(limit=3)
        self.assertFalse(caught.exception.retryable)
        active = self.store.load()["active_fetch"]
        self.assertEqual((active["phase"], active["run_id"]), ("PAUSED", RUN_ID))
        with self.assertRaises(ApifyError):
            fetcher.fetch_latest(limit=3)
        self.assertEqual(len(http.calls), 2)

    def test_wrong_run_dataset_status_and_business_error_pause_known_run(self):
        cases = (
            run(id="X" * 17), run(status="UNRECOGNIZED"), run(defaultDatasetId="X" * 17),
            response(200, {"error": {"type": "business-error"}}), response(404, {"error": {"type": "not-found"}}),
        )
        for action in cases:
            state, persisted = initial_state("thsottiaux"), []
            http = FakeHTTP(started(), action)
            fetcher = self.fetcher(http, state=state, persist=lambda value: persisted.append(deepcopy(value)))
            with self.subTest(action=action.status), self.assertRaises(ApifyError):
                fetcher.fetch_latest(limit=3)
            self.assertEqual(state["active_fetch"]["phase"], "PAUSED")
            self.assertEqual(state["active_fetch"]["run_id"], RUN_ID)
            with self.assertRaises(ApifyError):
                fetcher.fetch_latest(limit=3)
            self.assertEqual(len(http.calls), 2)

    def test_dataset_pages_are_complete_and_use_exact_offsets_without_filters(self):
        http = FakeHTTP(started(), run(), page([tweet(103), tweet(102)], total=3), page([tweet(101)], offset=2, total=3, limit=1))
        snapshot = self.fetcher(http).fetch_latest(limit=3)
        self.assertEqual([item["id"] for item in snapshot.tweets], ["101", "102", "103"])
        first, second = (parse_qs(urlsplit(call["url"]).query) for call in http.calls[-2:])
        self.assertEqual((first["offset"], second["offset"]), (["0"], ["2"]))
        self.assertEqual((first["limit"], second["limit"]), (["3"], ["1"]))
        for query in (first, second):
            self.assertEqual((query["clean"], query["skipEmpty"], query["skipHidden"]), (["false"], ["false"], ["false"]))
            self.assertFalse(set(query) & {"fields", "omit", "unwind", "view"})

    def test_page_read_failure_keeps_run_and_retry_reads_same_dataset(self):
        http = FakeHTTP(started(), run(), page([tweet(103)], total=2), TimeoutError(), run(), page([tweet(103), tweet(102)]))
        fetcher = self.fetcher(http)
        with self.assertRaises(ApifyError) as caught:
            fetcher.fetch_latest(limit=3)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "RUNNING")
        self.assertEqual(len(fetcher.fetch_latest(limit=3).tweets), 2)
        self.assertEqual(sum(call["method"] == "POST" for call in http.calls), 1)
        self.assertTrue(all(DATASET_ID in call["url"] for call in http.calls if "/datasets/" in call["url"]))

    def test_bad_pagination_never_returns_partial_or_slices_to_three(self):
        cases = (
            page([tweet()], total=4), page([tweet()], **{"X-Apify-Pagination-Offset": "1"}),
            page([tweet()], **{"X-Apify-Pagination-Count": "0"}), page([tweet()], **{"X-Apify-Pagination-Desc": "true"}),
            page([tweet()], **{"X-Apify-Pagination-Limit": "20"}), response(200, [tweet()], {}),
            page([], total=2), page([], total=0), page([tweet()] * 4, total=4),
        )
        for action in cases:
            state = initial_state("thsottiaux")
            http = FakeHTTP(started(), run(), action)
            fetcher = self.fetcher(http, state=state, persist=lambda _state: None)
            with self.subTest(headers=action.headers), self.assertRaises(ApifyError):
                fetcher.fetch_latest(limit=3)
            self.assertEqual(state["active_fetch"]["phase"], "PAUSED")
            self.assertIsNone(state["active_fetch"]["snapshot"])

    def test_repeated_page_and_changed_total_are_rejected(self):
        for final in (page([tweet(103)], offset=0, total=2, limit=1), page([tweet(102)], offset=1, total=3, limit=1)):
            state = initial_state("thsottiaux")
            http = FakeHTTP(started(), run(), page([tweet(103)], total=2), final)
            with self.subTest(headers=final.headers), self.assertRaises(ApifyError):
                self.fetcher(http, state=state, persist=lambda _state: None).fetch_latest(limit=3)
            self.assertEqual(state["active_fetch"]["phase"], "PAUSED")

    def test_reply_and_quote_context_are_preserved_without_extra_requests(self):
        row = tweet(isReply=True, inReplyToTweetId="100", inReplyToUsername="thsottiaux", quotedTweet={"text": "Quoted context"})
        http = self.happy([row])
        snapshot = self.fetcher(http).fetch_latest(limit=3)
        self.assertEqual(snapshot.tweets[0]["text"], row["text"])
        self.assertIn("Quoted context", snapshot.tweets[0]["context"])
        self.assertIn("inReplyToTweetId", snapshot.tweets[0]["context"])
        self.assertEqual(len(http.calls), 3)

    def test_invalid_tweets_errors_retweets_and_duplicates_pause_without_filtering(self):
        cases = (
            [tweet(success=False, error={"code": "FAILED"})], [tweet(status="error")], [tweet(isRetweet=True)],
            [tweet(authorUsername="wrong")], [tweet(author={"username": "wrong"})],
            [tweet(url="https://evil.example/thsottiaux/status/101")], [tweet(createdAt="2026-10-10T08:00:00+00:60")],
            [tweet(text="")], [tweet(isReply=None)], [tweet(normalizedInput=123)],
            [tweet(101), tweet(101)], [tweet(101), tweet(102)], [tweet(inputType="search")],
            [tweet(tweetId=str(1 << 64))],
        )
        for rows in cases:
            state = initial_state("thsottiaux")
            http = self.happy(rows)
            with self.subTest(case=cases.index(rows)), self.assertRaises(ApifyError):
                self.fetcher(http, state=state, persist=lambda _state: None).fetch_latest(limit=3)
            self.assertEqual(state["active_fetch"]["phase"], "PAUSED")

    def test_explicit_no_data_record_becomes_durable_empty_snapshot(self):
        row = {"success": True, "status": "success", "code": "NO_DATA", "inputType": "profile", "normalizedInput": "thsottiaux", "tweetId": None, "text": "", "url": "", "createdAt": None}
        http = self.happy([row])
        fetcher = self.fetcher(http)
        self.assertEqual(fetcher.fetch_latest(limit=3).tweets, [])
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "READY")
        self.assertEqual(fetcher.fetch_latest(limit=3).tweets, [])
        self.assertEqual(len(http.calls), 3)

    def test_ready_snapshot_is_immutable_and_replayed_without_http(self):
        http = self.happy()
        fetcher = self.fetcher(http)
        snapshot = fetcher.fetch_latest(limit=3)
        self.assertEqual(self.store.load()["active_fetch"]["snapshot"], snapshot.tweets)
        snapshot.tweets[0]["text"] = "caller mutation"
        replay_http = FakeHTTP()
        replay = self.fetcher(replay_http, state=self.store.load()).fetch_latest(limit=3)
        self.assertNotEqual(replay.tweets[0]["text"], "caller mutation")
        self.assertEqual(replay_http.calls, [])

    def test_ready_persistence_failure_recovers_known_run_without_post(self):
        def save_unless_ready(state):
            if state["active_fetch"]["phase"] == "READY":
                raise Crash()
            self.store.save(state)
        with self.assertRaises(Crash):
            self.fetcher(self.happy(), persist=save_unless_ready).fetch_latest(limit=3)
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "RUNNING")
        http = FakeHTTP(run(), page([tweet(103), tweet(102), tweet(101)]))
        self.assertEqual(len(self.fetcher(http, state=self.store.load()).fetch_latest(limit=3).tweets), 3)
        self.assertTrue(all(call["method"] == "GET" for call in http.calls))

    def test_prepare_ingest_rejects_mismatched_snapshot_and_does_not_save(self):
        fetcher = self.fetcher(self.happy())
        snapshot = fetcher.fetch_latest(limit=3)
        before = self.store.path.read_bytes()
        bad = deepcopy(snapshot)
        bad.tweets[0]["text"] = "modified"
        with self.assertRaises(ApifyError):
            fetcher.prepare_ingest(bad)
        self.assertEqual(self.state["active_fetch"]["phase"], "READY")
        fetcher.prepare_ingest(snapshot)
        self.assertIsNone(self.state["active_fetch"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_raw_watermark_and_run_clear_are_one_main_state_commit(self):
        self.state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        self.store.save(self.state)
        http = FakeHTTP(started(), run(usageTotalUsd="0.0009", chargedEventCounts={"tweet-result": 3}), page([tweet(103), tweet(102), tweet(101)]))
        fetcher = self.fetcher(http)
        snapshot = fetcher.fetch_latest(limit=3)
        for item in snapshot.tweets:
            self.state["inbox"][item["id"]] = {"tweet": item, "attempts": 0, "next_retry_at": None, "last_error": None}
        self.state["last_seen_id"] = "103"
        self.state["last_completed_fetch"] = deepcopy(self.state["active_fetch"])
        fetcher.prepare_ingest(snapshot)
        original_replace = os.replace
        def fail_main(source, destination):
            if Path(destination) == self.store.path:
                raise OSError("Injected main-state commit failure")
            return original_replace(source, destination)
        with patch("monitor.state.os.replace", side_effect=fail_main), self.assertRaises(StateError):
            self.store.save(self.state)
        old = self.store.load()
        self.assertEqual(old["active_fetch"]["phase"], "READY")
        self.assertIsNone(old["last_completed_fetch"])
        self.assertEqual((old["last_seen_id"], old["inbox"]), ("100", {}))
        resumed_http = FakeHTTP()
        resumed = self.fetcher(resumed_http, state=old)
        snapshot = resumed.fetch_latest(limit=3)
        for item in snapshot.tweets:
            old["inbox"][item["id"]] = {"tweet": item, "attempts": 0, "next_retry_at": None, "last_error": None}
        old["last_seen_id"] = "103"
        old["last_completed_fetch"] = deepcopy(old["active_fetch"])
        resumed.prepare_ingest(snapshot)
        self.store.save(old)
        committed = self.store.load()
        self.assertIsNone(committed["active_fetch"])
        self.assertEqual(committed["last_seen_id"], "103")
        self.assertEqual(set(committed["inbox"]), {"101", "102", "103"})
        receipt = committed["last_completed_fetch"]
        self.assertEqual((receipt["phase"], receipt["run_id"], receipt["dataset_id"]), ("READY", RUN_ID, DATASET_ID))
        self.assertEqual(receipt["billing"], {"usage_total_usd": "0.0009", "charged_results": 3})
        self.assertEqual(receipt["budget"]["approved_cap_usd"], "0.001")
        self.assertEqual(resumed_http.calls, [])

    def test_monitor_ingest_preserves_receipt_in_raw_watermark_commit(self):
        self.state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        self.store.save(self.state)
        fetcher = self.fetcher(FakeHTTP(started(), run(usageTotalUsd="0.0003", chargedEventCounts={"tweet-result": 1}), page([tweet()])))
        snapshot = fetcher.fetch_latest(limit=3)
        config = Config.from_env({"STATE_FILE": str(self.store.path)}, ROOT)
        monitor = Monitor(config, self.store, fetcher, FakeClassifier(), FakeNotifier(), clock=lambda: NOW)
        monitor._ingest(self.state, snapshot.tweets, RunResult("OK"), snapshot)
        committed = self.store.load()
        self.assertEqual((committed["last_seen_id"], set(committed["inbox"])), ("101", {"101"}))
        self.assertIsNone(committed["active_fetch"])
        self.assertEqual(committed["last_completed_fetch"]["run_id"], RUN_ID)
        self.assertEqual(committed["last_completed_fetch"]["billing"], {"usage_total_usd": "0.0003", "charged_results": 1})

    def test_monitor_round_binds_fetcher_classifies_all_three_and_preserves_receipt(self):
        self.state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        self.store.save(self.state)
        http = FakeHTTP(started(), run(usageTotalUsd="0.0009", chargedEventCounts={"tweet-result": 3}), page([tweet(103), tweet(102), tweet(101)]))
        fetcher = ApifyFetcher(api_token=TOKEN, transport=http, policy=approved_policy(), clock=lambda: NOW)
        irrelevant = {"label": "IRRELEVANT", "confidence": 0.95, "reset_time": None, "reason": "Offline evidence."}
        classifier = FakeClassifier({str(identifier): irrelevant for identifier in (101, 102, 103)})
        notifier = FakeNotifier()
        config = Config.from_env({"STATE_FILE": str(self.store.path)}, ROOT)
        result = Monitor(config, self.store, fetcher, classifier, notifier, clock=lambda: NOW, sleep=lambda _seconds: None).run()
        self.assertEqual((result.status, result.ingested, result.classified, result.sent), ("OK", 3, 3, 0))
        self.assertEqual([item["id"] for item in classifier.calls], ["101", "102", "103"])
        self.assertEqual(notifier.calls, [])
        committed = self.store.load()
        self.assertIsNone(committed["active_fetch"])
        self.assertEqual(committed["last_seen_id"], "103")
        self.assertEqual(set(committed["processed_ids"]), {"101", "102", "103"})
        self.assertEqual(committed["last_completed_fetch"]["run_id"], RUN_ID)
        self.assertEqual(committed["last_completed_fetch"]["billing"], {"usage_total_usd": "0.0009", "charged_results": 3})

    def test_monitor_retry_paused_never_reposts_unknown_and_recovers_old_queue(self):
        self.state.update(initialized=True, initial_baseline_id="100", last_seen_id="101")
        queued = {"id": "101", "author": "thsottiaux", "text": "Already durable original.",
                  "url": "https://x.com/thsottiaux/status/101", "created_at": "2026-10-10T08:01:00Z"}
        self.state["inbox"]["101"] = {"tweet": queued, "attempts": 0, "retryable": True, "next_retry_at": None, "last_error": None}
        self.store.save(self.state)
        config = Config.from_env({"STATE_FILE": str(self.store.path)}, ROOT)
        original_http = FakeHTTP(TimeoutError())
        original_fetcher = ApifyFetcher(api_token=TOKEN, transport=original_http, policy=approved_policy(), clock=lambda: NOW)
        first = Monitor(config, self.store, original_fetcher, FakeClassifier(outcomes=[StageFailure(retryable=False)]),
                        FakeNotifier(), clock=lambda: NOW, sleep=lambda _seconds: None).run()
        self.assertEqual((first.status, first.classified), ("DEGRADED", 0))
        durable = self.store.load()
        self.assertEqual(durable["active_fetch"]["phase"], "FETCH_UNKNOWN")
        self.assertIn("101", durable["inbox"])
        self.assertFalse(durable["failed_items"]["fetch"]["retryable"])
        self.assertEqual([call["method"] for call in original_http.calls], ["POST"])
        resumed_http = FakeHTTP()
        resumed_fetcher = ApifyFetcher(api_token=TOKEN, transport=resumed_http, clock=lambda: NOW)
        recovered_classifier = FakeClassifier({"101": {"label": "IRRELEVANT", "confidence": 0.95, "reset_time": None, "reason": "Offline recovery."}})
        recovered_notifier = FakeNotifier()
        second = Monitor(config, self.store, resumed_fetcher, recovered_classifier, recovered_notifier,
                         clock=lambda: NOW, sleep=lambda _seconds: None).run(resume_paused=True)
        self.assertEqual((second.status, second.classified), ("DEGRADED", 1))
        committed = self.store.load()
        self.assertEqual(committed["active_fetch"], durable["active_fetch"])
        self.assertEqual(committed["inbox"], {})
        self.assertEqual(committed["processed_ids"]["101"]["tweet"]["text"], queued["text"])
        self.assertEqual(resumed_http.calls, [])
        self.assertEqual(recovered_notifier.calls, [])

    def test_corrupt_journal_is_rejected_and_never_reinitialized(self):
        fetcher = self.fetcher(self.happy())
        fetcher.fetch_latest(limit=3)
        damaged = deepcopy(self.state)
        damaged["active_fetch"]["snapshot_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_active_fetch(damaged["active_fetch"])
        with self.assertRaises(StateError):
            self.store.save(damaged)
        http = FakeHTTP()
        with self.assertRaises(ApifyError):
            self.fetcher(http, state=damaged)
        self.assertEqual(http.calls, [])
        self.assertEqual(self.store.load()["active_fetch"]["phase"], "READY")

    def test_observed_billing_or_cap_changes_pause_instead_of_raising_cap(self):
        cases = (
            {"usageTotalUsd": "0.002"}, {"chargedEventCounts": {"tweet-result": 4}},
            {"chargedEventCounts": {"tweet-result": 1, "actor-start": 1}},
            {"options": {"maxTotalChargeUsd": "0.002"}},
            {"pricingInfo": {"minimalMaxTotalChargeUsd": "0.002"}},
        )
        for facts in cases:
            state = initial_state("thsottiaux")
            http = FakeHTTP(started(), run(**facts))
            with self.subTest(facts=facts), self.assertRaises(ApifyError):
                self.fetcher(http, state=state, persist=lambda _state: None).fetch_latest(limit=3)
            self.assertEqual(state["active_fetch"]["phase"], "PAUSED")
            self.assertEqual(state["active_fetch"]["budget"]["approved_cap_usd"], "0.001")
            self.assertEqual(sum(call["method"] == "POST" for call in http.calls), 1)

    def test_observed_charge_evidence_is_saved_with_complete_snapshot(self):
        http = FakeHTTP(started(), run(usageTotalUsd="0.0009", chargedEventCounts={"tweet-result": 3}, options={"maxTotalChargeUsd": "0.001"}), page([tweet(103), tweet(102), tweet(101)]))
        self.fetcher(http).fetch_latest(limit=3)
        self.assertEqual(self.store.load()["active_fetch"]["billing"], {"usage_total_usd": "0.0009", "charged_results": 3})


def identity_contract():
    """Artificial reviewed evidence, injected only into offline tests."""
    return SourceContract(
        contract_id="synthetic-offline-contract", actor=ACTOR,
        build_id="B" * 17, build_number="0.0.123", target_username="thsottiaux",
        target_user_id="12345",
        official_evidence_urls=("https://apify.com/lance_api/x-tweet-scraper-api",),
        evidence_sha256="a" * 64,
        reviewed_at="2026-10-10T07:00:00Z", expires_at="2026-10-11T07:00:00Z",
    )


class ApifyIdentityTests(unittest.TestCase):
    setUp = ApifyTests.setUp
    cleanup = ApifyTests.cleanup

    def bound_state(self):
        state = initial_state("thsottiaux", execution_mode="LIVE", target_user_id="12345")
        state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        return state

    def fetcher(self, http, *, state=None, clock=None, catalog=None, persist=None, **changes):
        entry = identity_contract()
        kwargs = dict(api_token=TOKEN, transport=http, policy=approved_policy(),
                      target_user_id="12345", contract_id=entry.contract_id,
                      contract_catalog={entry.contract_id: entry} if catalog is None else catalog,
                      clock=clock or (lambda: NOW.replace(minute=30)))
        kwargs.update(changes)
        fetcher = ApifyFetcher(**kwargs)
        state = self.bound_state() if state is None else state
        fetcher.bind_state(state, persist or self.store.save)
        return fetcher

    def happy(self, rows=None):
        rows = [tweet(103, authorId="12345"), tweet(102, authorId="12345", isReply=True),
                tweet(101, authorId="12345")] if rows is None else rows
        start = response(201, {"data": {"id": RUN_ID, "status": "RUNNING",
                                       "defaultDatasetId": DATASET_ID, "buildId": "B" * 17}})
        return FakeHTTP(start, run(buildId="B" * 17), page(rows))

    def test_live_requires_stable_id_and_production_proof_even_with_caller_true(self):
        for bound in (False, True):
            http, state = FakeHTTP(), self.bound_state()
            kwargs = dict(api_token=TOKEN, transport=http, policy=approved_policy(), clock=lambda: NOW)
            if bound:
                kwargs.update(target_user_id="12345", contract_id=identity_contract().contract_id)
            fetcher = ApifyFetcher(**kwargs)
            with self.subTest(bound=bound), self.assertRaises(ApifyError):
                fetcher.bind_state(state, self.store.save)
            self.assertEqual(http.calls, [])
            self.assertIsNone(state["active_fetch"])
            self.assertFalse(self.store.path.exists())

    def test_bound_success_pins_actor_build_and_preserves_every_author_id(self):
        http = self.happy()
        snapshot = self.fetcher(http).fetch_latest(limit=3)
        self.assertEqual([item["id"] for item in snapshot.tweets], ["101", "102", "103"])
        self.assertTrue(all(item["author_id"] == "12345" for item in snapshot.tweets))
        self.assertEqual(parse_qs(urlsplit(http.calls[0]["url"]).query)["build"], ["0.0.123"])
        self.assertEqual(json.loads(http.calls[0]["body"]), {"profiles": ["thsottiaux"], "maxResults": 3})
        active = self.store.load()["active_fetch"]
        self.assertEqual((active["schema_version"], active["target_user_id"], active["source_contract_id"]),
                         (2, "12345", identity_contract().contract_id))
        self.assertEqual((active["build_id"], active["build_number"]), ("B" * 17, "0.0.123"))

    def test_missing_wrong_and_conflicting_identity_never_move_watermark(self):
        cases = (
            {"authorId": None}, {"authorId": 12345}, {"authorId": "54321"},
            {"author": None}, {"author": {"username": "thsottiaux"}},
            {"author": {"username": "thsottiaux", "id": "54321"}},
            {"author": {"username": "other", "id": "12345"}},
            {"authorUsername": "other"}, {"isRetweet": True},
            {"inputType": "search"}, {"createdAt": "2026-10-10T09:00:00Z"},
        )
        for changes in cases:
            state = self.bound_state()
            http = self.happy([tweet(101, authorId="12345", **changes)]) if "authorId" not in changes else self.happy([tweet(101, **changes)])
            with self.subTest(fields=tuple(changes)), self.assertRaises(ApifyError):
                self.fetcher(http, state=state, persist=lambda _state: None).fetch_latest(limit=3)
            self.assertEqual(state["active_fetch"]["phase"], "PAUSED")
            self.assertEqual((state["last_seen_id"], state["inbox"], state["processed_ids"]), ("100", {}, {}))

    def test_monitor_rejects_unbound_author_without_classification_or_notification(self):
        state = self.bound_state()
        self.store.save(state)
        http = self.happy([tweet(authorId="54321")])
        fetcher = self.fetcher(http, state=state)
        classifier, notifier = FakeClassifier(), FakeNotifier()
        config = replace(Config.from_env({"STATE_FILE": str(self.store.path)}, ROOT),
                         execution_mode="LIVE", target_user_id="12345")
        result = Monitor(config, self.store, fetcher, classifier, notifier,
                         clock=lambda: NOW.replace(minute=30), sleep=lambda _seconds: None).run()
        self.assertEqual((result.ingested, result.classified, result.sent), (0, 0, 0))
        self.assertEqual((classifier.calls, notifier.calls), ([], []))
        self.assertEqual(self.store.load()["last_seen_id"], "100")

    def test_no_data_without_stable_identity_is_not_a_valid_bound_snapshot(self):
        row = {"success": True, "status": "success", "code": "NO_DATA", "inputType": "profile",
               "normalizedInput": "thsottiaux", "tweetId": None, "text": "", "url": "", "createdAt": None}
        state = self.bound_state()
        with self.assertRaises(ApifyError):
            self.fetcher(self.happy([row]), state=state).fetch_latest(limit=3)
        self.assertEqual((state["active_fetch"]["phase"], state["last_seen_id"]), ("PAUSED", "100"))

    def test_latest_order_includes_id_tie_break_and_rejects_older_first(self):
        for rows in ([tweet(101, authorId="12345"), tweet(102, authorId="12345")],
                     [tweet(101, authorId="12345", createdAt="2026-10-10T08:00:00Z"),
                      tweet(102, authorId="12345", createdAt="2026-10-10T08:00:00Z")]):
            with self.subTest(ids=[row["tweetId"] for row in rows]), self.assertRaises(ApifyError):
                self.fetcher(self.happy(rows), persist=lambda _state: None).fetch_latest(limit=3)

    def test_wrong_post_build_is_unknown_and_never_reposted(self):
        http = FakeHTTP(started())  # No proof that the requested reviewed build ran.
        state = self.bound_state()
        with self.assertRaises(ApifyError):
            self.fetcher(http, state=state).fetch_latest(limit=3)
        self.assertEqual(state["active_fetch"]["phase"], "FETCH_UNKNOWN")
        restarted_http = FakeHTTP()
        with self.assertRaises(ApifyError):
            self.fetcher(restarted_http, state=self.store.load()).fetch_latest(limit=3)
        self.assertEqual(restarted_http.calls, [])

    def test_known_run_build_mismatch_pauses_before_dataset_read(self):
        http = self.happy()
        http.actions[1] = run(buildId="C" * 17)
        state = self.bound_state()
        with self.assertRaises(ApifyError):
            self.fetcher(http, state=state).fetch_latest(limit=3)
        self.assertEqual(state["active_fetch"]["phase"], "PAUSED")
        self.assertEqual([call["method"] for call in http.calls], ["POST", "GET"])

    def test_crash_after_known_run_save_recovers_same_bound_build_with_get_only(self):
        def save_and_crash(state):
            self.store.save(state)
            if state["active_fetch"]["phase"] == "RUNNING":
                raise Crash()
        http = self.happy()
        with self.assertRaises(Crash):
            self.fetcher(http, persist=save_and_crash).fetch_latest(limit=3)
        resumed = FakeHTTP(run(buildId="B" * 17), page([tweet(authorId="12345")]))
        snapshot = self.fetcher(resumed, state=self.store.load()).fetch_latest(limit=3)
        self.assertEqual(snapshot.tweets[0]["author_id"], "12345")
        self.assertEqual([call["method"] for call in resumed.calls], ["GET", "GET"])

    def test_legacy_journal_and_changed_binding_are_retained_without_http(self):
        self.fetcher(self.happy()).fetch_latest(limit=3)
        valid = self.store.load()
        cases = []
        legacy = deepcopy(valid)
        legacy["active_fetch"]["schema_version"] = 1
        for name in ("target_user_id", "source_contract_id", "build_id", "build_number"):
            legacy["active_fetch"].pop(name)
        cases.append(legacy)
        wrong_contract = deepcopy(valid)
        wrong_contract["active_fetch"]["source_contract_id"] = "another-reviewed-contract"
        cases.append(wrong_contract)
        wrong_build = deepcopy(valid)
        wrong_build["active_fetch"]["build_id"] = "C" * 17
        cases.append(wrong_build)
        for state in cases:
            before, http = deepcopy(state), FakeHTTP()
            with self.subTest(journal=state["active_fetch"]["schema_version"]), self.assertRaises(ApifyError):
                self.fetcher(http, state=state)
            self.assertEqual((state, http.calls), (before, []))

    def test_contract_expiry_blocks_ready_replay_without_state_change(self):
        clock, http, state = [NOW.replace(minute=30)], self.happy(), self.bound_state()
        fetcher = self.fetcher(http, state=state, clock=lambda: clock[0])
        fetcher.fetch_latest(limit=3)
        before, before_calls = deepcopy(state), len(http.calls)
        clock[0] = NOW.replace(day=11, hour=7)
        with self.assertRaises(ApifyError) as caught:
            fetcher.fetch_latest(limit=3)
        self.assertEqual(caught.exception.code, "APIFY_SOURCE_CONTRACT_EXPIRED")
        self.assertEqual((state, len(http.calls)), (before, before_calls))

    def test_contract_expiry_between_poll_and_page_prevents_page_attempt(self):
        clock = [NOW.replace(minute=30)]
        def expire_after_poll(_call):
            clock[0] = NOW.replace(day=11, hour=7)
            return run(buildId="B" * 17)
        http, state = self.happy(), self.bound_state()
        http.actions[1] = expire_after_poll
        with self.assertRaises(ApifyError):
            self.fetcher(http, state=state, clock=lambda: clock[0]).fetch_latest(limit=3)
        self.assertEqual([call["method"] for call in http.calls], ["POST", "GET"])
        self.assertEqual(state["active_fetch"]["phase"], "PAUSED")

    def test_pre_request_quota_rejection_pauses_start_instead_of_unknown(self):
        from monitor.batch import NoExternalCall
        test, attempted = self, []
        class RefusingBudget:
            def request(self, *_args, **_kwargs):
                attempted.append(test.store.load()["active_fetch"]["phase"])
                raise NoExternalCall("VALIDATION_BATCH_EXHAUSTED")
        state = self.bound_state()
        with self.assertRaises(ApifyError) as caught:
            self.fetcher(RefusingBudget(), state=state).fetch_latest(limit=3)
        self.assertEqual(caught.exception.code, "APIFY_CALL_NOT_RESERVED")
        self.assertEqual((state["active_fetch"]["phase"], state["active_fetch"]["run_id"]), ("PAUSED", None))
        self.assertEqual(attempted, ["STARTING"])

    def test_state_id_mismatch_is_rejected_before_run_start(self):
        state, http = self.bound_state(), FakeHTTP()
        state["target_user_id"] = "54321"
        with self.assertRaises(ApifyError):
            self.fetcher(http, state=state)
        self.assertEqual(http.calls, [])
        self.assertIsNone(state["active_fetch"])


if __name__ == "__main__":
    unittest.main()
