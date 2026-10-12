"""DEV-03 public config, migration, HTTP, and retry contracts; no network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import shutil
import ssl
import unittest
from uuid import uuid4

from config import Config, ConfigError

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT
from monitor.classifier import ClassifierReply
from monitor.engine import Monitor
from monitor.fakes import FakeClassifier, FakeFetcher, FakeNotifier
from monitor.http_transport import StdlibHTTPTransport
from monitor.models import StageFailure
from monitor.state import StateError, StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime" / "test-tmp").resolve()
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def tweet(identifier=101):
    return {"id": str(identifier), "author": "thsottiaux", "text": "Synthetic unrelated prose.",
            "url": f"https://x.com/thsottiaux/status/{identifier}", "created_at": NOW.isoformat(), "context": None}


class CoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = TEST_ROOT / ("dev03-core-" + uuid4().hex)
        self.path.mkdir()
        self.addCleanup(self.cleanup)
        self.store = StateStore(self.path / "state.json")
        self.config = Config.from_env({"STATE_FILE": str(self.store.path)}, ROOT)

    def cleanup(self):
        target = self.path.resolve()
        if target == TEST_ROOT or not target.is_relative_to(TEST_ROOT):
            raise RuntimeError("Refusing cleanup outside test root")
        shutil.rmtree(target)

    def test_confirmed_public_defaults_and_case_normalization(self):
        config = Config.from_env({"TARGET_USERNAME": "@THSOTTIAUX"}, self.path)
        self.assertEqual(config.target_username, "thsottiaux")
        self.assertEqual(Config(self.store.path, target_username="@THSOTTIAUX").target_username, "thsottiaux")
        self.assertTrue(config.include_replies)
        self.assertTrue(config.exclude_retweets)
        self.assertEqual(config.email_to, LOCAL_RECIPIENT)
        self.assertIsNone(config.target_user_id)

    def test_reply_retweet_scope_and_numeric_identity_validation(self):
        for env in ({"INCLUDE_REPLIES": "false"}, {"EXCLUDE_RETWEETS": "false"},
                    {"INCLUDE_REPLIES": "yes"}, {"TARGET_USER_ID": "0"},
                    {"TARGET_USER_ID": "18446744073709551616"}, {"TARGET_USER_ID": "secret"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                Config.from_env(env, self.path)
        self.assertEqual(Config.from_env({"TARGET_USER_ID": "18446744073709551615"}, self.path).target_user_id, "18446744073709551615")

    def test_legacy_state_migrates_without_disk_write_and_uppercase_target_still_matches(self):
        old = initial_state("thsottiaux")
        old.pop("active_fetch")
        old["schema_version"] = 1
        old["target"] = "THSOTTIAUX"
        raw = json.dumps(old).encode()
        self.store.path.write_bytes(raw)
        loaded = self.store.load()
        self.assertEqual((loaded["schema_version"], loaded["target"], loaded["active_fetch"]), (3, "thsottiaux", None))
        self.assertEqual(self.store.path.read_bytes(), raw)
        config = Config.from_env({"TARGET_USERNAME": "@THSOTTIAUX", "STATE_FILE": str(self.store.path)}, ROOT)
        result = Monitor(config, self.store, FakeFetcher([tweet(100)]), FakeClassifier(), FakeNotifier()).run()
        self.assertEqual(result.status, "OK")
        self.assertEqual(self.store.load()["schema_version"], 3)

    def test_legacy_cannot_silently_smuggle_a_paid_run(self):
        state = initial_state("thsottiaux")
        state.update(schema_version=1, active_fetch={"phase": "RUNNING"})
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(StateError):
            self.store.load()

    def test_offline_state_cannot_be_used_in_live_mode_before_any_service_call(self):
        self.initialized()
        before = self.store.path.read_bytes()
        fetcher, classifier, notifier = FakeFetcher([tweet()]), FakeClassifier(), FakeNotifier()
        with self.assertRaises(StateError):
            Monitor(replace(self.config, execution_mode="LIVE"), self.store, fetcher, classifier, notifier).run()
        self.assertEqual((fetcher.calls, classifier.calls, notifier.calls), ([], [], []))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_live_state_cannot_be_replayed_by_offline_cli_core(self):
        state = initial_state("thsottiaux", execution_mode="LIVE")
        self.store.save(state)
        fetcher = FakeFetcher([tweet()])
        with self.assertRaises(StateError):
            Monitor(self.config, self.store, fetcher, FakeClassifier(), FakeNotifier()).run()
        self.assertEqual(fetcher.calls, [])
        result = Monitor(self.config, self.store, fetcher, FakeClassifier(), FakeNotifier()).run(dry_run=True)
        self.assertEqual(result.details["execution_mode"], "LIVE")

    def test_missing_old_origin_is_read_as_offline_and_environment_cannot_enable_live(self):
        state = initial_state("thsottiaux")
        state["schema_version"] = 2
        state.pop("execution_mode")
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.store.load()["execution_mode"], "OFFLINE")
        self.assertEqual(Config.from_env({"EXECUTION_MODE": "LIVE"}, self.path).execution_mode, "OFFLINE")

    def initialized(self):
        state = initial_state("thsottiaux")
        state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        self.store.save(state)

    def test_long_retry_after_is_persisted_without_sleep_or_duplicate_call(self):
        self.initialized()
        classifier = FakeClassifier(outcomes=[StageFailure(retry_after_seconds=10800)])
        sleeps = []
        result = Monitor(self.config, self.store, FakeFetcher([tweet()]), classifier,
                         FakeNotifier(), clock=lambda: NOW, sleep=sleeps.append, monotonic=lambda: 0).run()
        self.assertEqual(result.status, "DEGRADED")
        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(self.store.load()["inbox"]["101"]["next_retry_at"], (NOW + timedelta(hours=3)).isoformat())

    def test_retry_after_within_budget_is_honored_and_no_adapter_retry_multiplier(self):
        self.initialized()
        classifier = FakeClassifier(outcomes=[StageFailure(retry_after_seconds=10)] * 3)
        sleeps = []
        Monitor(self.config, self.store, FakeFetcher([tweet()]), classifier, FakeNotifier(),
                clock=lambda: NOW, sleep=sleeps.append, monotonic=lambda: 0).run()
        self.assertEqual(len(classifier.calls), 3)
        self.assertEqual(sleeps, [10, 15])

    def test_unrepresentable_retry_after_pauses_instead_of_retrying_early(self):
        self.initialized()
        classifier = FakeClassifier(outcomes=[StageFailure(retry_after_seconds=1e100)])
        Monitor(self.config, self.store, FakeFetcher([tweet()]), classifier, FakeNotifier(),
                clock=lambda: NOW, sleep=lambda _: self.fail("Must not sleep")).run()
        self.assertEqual(len(classifier.calls), 1)
        self.assertFalse(self.store.load()["inbox"]["101"]["retryable"])

    def test_retry_admission_deadline_stops_after_elapsed_time(self):
        self.initialized()
        time = [0]
        class SlowClassifier:
            calls = 0
            def classify(inner, *args, **kwargs):
                inner.calls += 1
                time[0] += 91
                raise StageFailure()
        classifier = SlowClassifier()
        Monitor(self.config, self.store, FakeFetcher([tweet()]), classifier, FakeNotifier(),
                clock=lambda: NOW, sleep=lambda _: self.fail("Past deadline"), monotonic=lambda: time[0]).run()
        self.assertEqual(classifier.calls, 1)

    def test_source_retry_after_blocks_early_fetch_but_recovers_old_local_work(self):
        self.initialized()
        state = self.store.load()
        state["inbox"]["101"] = {"tweet": tweet(), "attempts": 0, "retryable": True, "next_retry_at": None, "last_error": None}
        self.store.save(state)
        irrelevant = {"label": "IRRELEVANT", "confidence": 0.9, "reset_time": None, "reason": "Synthetic ordinary prose."}
        first = Monitor(self.config, self.store, FakeFetcher(outcomes=[StageFailure(retry_after_seconds=10800)]),
                        FakeClassifier({"101": irrelevant}), FakeNotifier(), clock=lambda: NOW).run()
        self.assertEqual(first.classified, 1)
        early = FakeFetcher([tweet(102)])
        blocked = Monitor(self.config, self.store, early, FakeClassifier(), FakeNotifier(),
                          clock=lambda: NOW + timedelta(hours=1)).run()
        self.assertEqual(blocked.errors, ["FETCH_COOLDOWN"])
        self.assertEqual(early.calls, [])
        due = FakeFetcher([tweet(102)])
        recovered = Monitor(self.config, self.store, due, FakeClassifier({"102": irrelevant}), FakeNotifier(),
                            clock=lambda: NOW + timedelta(hours=3)).run()
        self.assertEqual((recovered.status, recovered.classified), ("OK", 1))
        self.assertEqual(due.calls, [3])


class HTTPBoundaryTests(unittest.TestCase):
    def factory(self, *, status=200, data=b'{}', failure=None):
        calls = []
        class Response:
            def read(self, limit):
                calls.append(("read", limit))
                return data[:limit]
            def getheaders(self): return [("Retry-After", "5")]
        Response.status = status
        class Connection:
            def request(self, method, path, **kwargs):
                calls.append((method, path, kwargs))
                if failure: raise failure
            def getresponse(self): return Response()
            def close(self): calls.append(("close",))
        def create(host, port, *, timeout, context):
            calls.append(("connect", host, port, timeout, context.check_hostname, context.verify_mode))
            return Connection()
        return StdlibHTTPTransport(connection_factory=create, max_response_bytes=32), calls

    def test_one_tls_request_bounded_read_and_cleanup(self):
        transport, calls = self.factory()
        response = transport.request("POST", "https://api.deepseek.com/chat/completions", headers={"Authorization": "Bearer synthetic"}, body=b'{}', timeout=20)
        self.assertEqual(response.status, 200)
        self.assertEqual(calls[0], ("connect", "api.deepseek.com", 443, 20, True, ssl.CERT_REQUIRED))
        self.assertEqual(calls[-2:], [("read", 33), ("close",)])
        self.assertEqual(sum(c[0] == "POST" for c in calls), 1)

    def test_redirect_is_returned_without_following_and_no_proxy_lookup(self):
        transport, calls = self.factory(status=302)
        response = transport.request("GET", "https://api.apify.com/v2/actor-runs/id", headers={}, body=None, timeout=20)
        self.assertEqual(response.status, 302)
        self.assertEqual(sum(c[0] == "connect" for c in calls), 1)

    def test_oversized_response_stops_and_redacts_transport_exception(self):
        transport, calls = self.factory(data=b'x' * 100)
        with self.assertRaises(StageFailure) as error:
            transport.request("GET", "https://api.apify.com/v2/datasets/id/items", headers={}, body=None, timeout=20)
        self.assertFalse(error.exception.retryable)
        self.assertEqual(calls[-1], ("close",))
        transport, calls = self.factory(failure=OSError("SYNTHETIC_CREDENTIAL_IN_EXCEPTION"))
        with self.assertRaises(StageFailure) as error:
            transport.request("GET", "https://api.apify.com/v2/actor-runs/id", headers={}, body=None, timeout=20)
        self.assertNotIn("SYNTHETIC_CREDENTIAL", str(error.exception))
        self.assertEqual(calls[-1], ("close",))

    def test_bad_urls_methods_headers_and_timeouts_never_construct_connection(self):
        transport, calls = self.factory()
        for change in ({"url": "http://api.apify.com/x"}, {"url": "https://other.invalid/x"},
                       {"url": "https://secret" + "@api.apify.com/x"}, {"url": "https://api.apify.com:444/x"},
                       {"method": "DELETE"}, {"headers": {"Authorization": "secret\r\nX: bad"}},
                       {"timeout": float('nan')}, {"timeout": 0}, {"body": "wrong"}):
            args = dict(method="GET", url="https://api.apify.com/x", headers={}, body=None, timeout=20)
            args.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                transport.request(**args)
        self.assertEqual(calls, [])
