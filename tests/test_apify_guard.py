"""Exact reviewed Apify operations and evidence expiry at the wire boundary."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from monitor.apify import ACTOR, ApifyFetcher, ApifyPolicy
from monitor.batch import BatchLedger, NoExternalCall
from monitor.locking import ProcessLock
from monitor.state import StateStore, initial_state
from monitor.validation import GuardedApifyTransport
from tests.test_validation import (NOW, RUN_ID, DATASET_ID, FakeHTTP,
                                   response, source_cycle, validation_facts)


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime/test-tmp").resolve()


class ApifyGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = TEST_ROOT / ("apify-guard-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.now = NOW
        self.facts = validation_facts(self.directory / "live-state.json")
        self.auth = self.facts["authorization"]
        self.ledger = BatchLedger(self.auth.ledger_path, self.auth, clock=lambda: self.now)
        self.ledger.initialize()
        self.http = FakeHTTP()
        self.guard = GuardedApifyTransport(self.http, self.ledger,
            source_catalog=self.facts["source_catalog"], apify_cost_proofs=self.facts["apify_cost_proofs"],
            clock=lambda: self.now)
        self.headers = {"Authorization": "Bearer synthetic-secret-never-recorded"}
        for target in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(target, side_effect=AssertionError("REAL_EXTERNAL_CALL_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)

    def cleanup(self):
        target, root = self.directory.resolve(), TEST_ROOT.resolve()
        if target == root or target.parent != root:
            raise RuntimeError("UNSAFE_CLEANUP")
        shutil.rmtree(target)

    def options(self):
        contract = self.facts["source_catalog"][self.auth.source_contract_evidence_id]
        return {"maxTotalChargeUsd": "0.001", "restartOnError": "false", "waitForFinish": "0", "build": contract.build_number}

    def post_url(self, options=None):
        return f"https://api.apify.com/v2/acts/{ACTOR}/runs?" + urlencode(options if options is not None else self.options())

    def body(self):
        return json.dumps({"profiles": [self.auth.target_username], "maxResults": 3}).encode()

    def dataset_options(self):
        return {"format": "json", "offset": "0", "limit": "3", "desc": "false", "clean": "false",
                "skipEmpty": "false", "skipHidden": "false"}

    def dataset_url(self, options=None):
        return f"https://api.apify.com/v2/datasets/{DATASET_ID}/items?" + urlencode(options if options is not None else self.dataset_options())

    def request(self, method="POST", url=None, body=b"USE_DEFAULT_BODY"):
        if body == b"USE_DEFAULT_BODY":
            body = self.body()
        return self.guard.request(method, url or self.post_url(), headers=self.headers, body=body, timeout=30)

    def denied_without_reservation(self, call):
        before = self.ledger.path.read_bytes()
        with self.assertRaises(NoExternalCall):
            call()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_production_empty_evidence_catalog_blocks_before_reservation_and_wire(self):
        guarded = GuardedApifyTransport(self.http, self.ledger, clock=lambda: self.now)
        self.denied_without_reservation(lambda: guarded.request("POST", self.post_url(), headers=self.headers,
                                                                body=self.body(), timeout=30))

    def test_actual_fetcher_exact_build_and_queries_pass_with_real_persistent_ledger(self):
        self.http.replies.extend(source_cycle([100, 99, 98]))
        fetcher = ApifyFetcher(api_token="synthetic-apify-secret", transport=self.guard,
            policy=ApifyPolicy(**self.facts["settings"]["apify_policy"], approved=True,
                              pricing_verified=True, source_contract_verified=True),
            target=self.auth.target_username, target_user_id=self.auth.target_user_id,
            contract_id=self.auth.source_contract_evidence_id, contract_catalog=self.facts["source_catalog"],
            clock=lambda: self.now)
        store = StateStore(self.auth.state_path)
        state = initial_state(self.auth.target_username, execution_mode="LIVE", target_user_id=self.auth.target_user_id)
        with ProcessLock(str(store.path) + ".lock"):
            fetcher.bind_state(state, store.save)
            snapshot = fetcher.fetch_latest(limit=3)
        self.assertEqual(len(snapshot.tweets), 3)
        self.assertEqual([call[0] for call in self.http.calls], ["POST", "GET", "GET"])
        counts = self.ledger.validate_ready()["counts"]
        self.assertEqual((counts["apify_start"], counts["apify_poll"], counts["apify_dataset"]), (1, 1, 1))
        query = parse_qs(self.http.calls[0][1].split("?", 1)[1])
        self.assertEqual(query["build"], ["0.0.123"])

    def test_post_requires_reviewed_build_fixed_flags_and_single_allowlisted_query_values(self):
        mutations = []
        for field in self.options():
            options = self.options()
            del options[field]
            mutations.append(options)
        for field, value in (("build", "latest"), ("build", "0.0.124"), ("build", "B" * 17),
                             ("restartOnError", "true"), ("waitForFinish", "1"),
                             ("token", "synthetic-secret"), ("buildId", "B" * 17)):
            mutations.append(dict(self.options(), **{field: value}))
        for options in mutations:
            with self.subTest(options=options):
                self.denied_without_reservation(lambda: self.request(url=self.post_url(options)))
        for field, value in self.options().items():
            url = self.post_url() + "&" + urlencode({field: value})
            self.denied_without_reservation(lambda: self.request(url=url))

    def test_run_cap_requires_single_finite_positive_decimal_within_approved_bound(self):
        for value in ("0", "-1", "NaN", "Infinity", "-Infinity", "1e-3", "", " ", "0.010000000000000001",
                      "00.001", "1", "0.0000000000000000001"):
            with self.subTest(value=value):
                self.denied_without_reservation(lambda: self.request(url=self.post_url(dict(self.options(), maxTotalChargeUsd=value))))

    def test_post_body_rejects_wrong_types_duplicate_keys_extra_fields_and_truncation(self):
        bodies = [None, "not bytes", b"not JSON", b"\xff", b"{}", b"[]", self.body() + b" "]
        bodies.pop()  # JSON trailing whitespace is semantically harmless.
        for change in ({"maxResults": 3.0}, {"maxResults": True}, {"maxResults": 4}, {"maxResults": "3"},
                       {"profiles": ["other"]}, {"profiles": "thsottiaux"}, {"profiles": ["thsottiaux", "other"]},
                       {"extra": "synthetic-secret"}):
            bodies.append(json.dumps(dict(profiles=[self.auth.target_username], maxResults=3, **change)).encode()
                          if "maxResults" not in change and "profiles" not in change else
                          json.dumps({**{"profiles": [self.auth.target_username], "maxResults": 3}, **change}).encode())
        bodies.extend((b'{"profiles":["thsottiaux"],"maxResults":2,"maxResults":3}',
                       b'{"profiles":["thsottiaux"],"maxResults":NaN}', b" " * 1_048_577))
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                self.denied_without_reservation(lambda: self.request(body=body))

    def test_poll_allows_no_query_or_body_and_only_reviewed_operation_shape(self):
        base = f"https://api.apify.com/v2/actor-runs/{RUN_ID}"
        for suffix in ("?token=synthetic-secret", "?build=0.0.123", "?waitForFinish=1", "?a=1&a=2"):
            self.denied_without_reservation(lambda: self.request("GET", base + suffix, None))
        self.denied_without_reservation(lambda: self.request("GET", base, b"{}"))
        self.denied_without_reservation(lambda: self.request("POST", base, b"{}"))
        self.http.replies.append(response(200, {}))
        self.request("GET", base, None)
        self.assertEqual(self.ledger.validate_ready()["counts"]["apify_poll"], 1)

    def test_dataset_query_rejects_filters_projections_reordering_and_out_of_three_range(self):
        for field, value in (("format", "csv"), ("desc", "true"), ("clean", "true"), ("skipEmpty", "true"),
                             ("skipHidden", "true"), ("fields", "text"), ("omit", "authorId"),
                             ("limit", "4"), ("limit", "0"), ("limit", "1.0"), ("limit", "01"),
                             ("offset", "3"), ("offset", "-1"), ("offset", "0.0"), ("offset", "00")):
            with self.subTest(field=field, value=value):
                options = dict(self.dataset_options(), **{field: value})
                self.denied_without_reservation(lambda: self.request("GET", self.dataset_url(options), None))
        for field in self.dataset_options():
            options = self.dataset_options()
            del options[field]
            self.denied_without_reservation(lambda: self.request("GET", self.dataset_url(options), None))
            self.denied_without_reservation(lambda: self.request("GET", self.dataset_url() + "&" + urlencode({field: ""}), None))
        self.denied_without_reservation(lambda: self.request("GET", self.dataset_url(dict(self.dataset_options(), offset="2", limit="2")), None))
        self.http.replies.append(response(200, []))
        self.request("GET", self.dataset_url(dict(self.dataset_options(), offset="2", limit="1")), None)
        self.assertEqual(self.ledger.validate_ready()["counts"]["apify_dataset"], 1)

    def test_url_controls_credentials_fragments_and_alternate_endpoints_are_refused(self):
        valid = self.post_url()
        for url in (valid.replace("https://", "http://"), valid.replace("api.apify.com", "example.com"),
                    valid.replace("api.apify.com", "api.apify.com:443"), valid.replace("api.apify.com", "user" + "@api.apify.com"),
                    valid + "#fragment", valid.replace("api.apify.com", "api.\napify.com"),
                    valid.replace("/runs?", "/runs/last?"), valid.replace("/v2/", "/v1/")):
            self.denied_without_reservation(lambda: self.request(url=url))

    def expire_during_reservation(self, *, source=False):
        if source:
            catalog = self.facts["source_catalog"]
            key = self.auth.source_contract_evidence_id
            catalog[key] = replace(catalog[key], expires_at=(self.now + timedelta(seconds=1)).isoformat())
        else:
            catalog = self.facts["apify_cost_proofs"]
            key = self.auth.apify["bound_evidence_id"]
            catalog[key] = replace(catalog[key], expires_at=self.now + timedelta(seconds=1))
        def advance(event, _snapshot):
            if event == "after_replace":
                self.now += timedelta(seconds=2)
        self.ledger.fault_hook = advance
        with self.assertRaises(NoExternalCall):
            self.request()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.ledger.validate_ready()["counts"]["apify_start"], 1)
        self.assertEqual(self.ledger.validate_ready()["reserved"]["apify"]["amount"], "0.01")

    def test_cost_proof_expiring_after_reservation_keeps_quota_and_never_calls_wire(self):
        self.expire_during_reservation()

    def test_source_proof_expiring_after_reservation_keeps_quota_and_never_calls_wire(self):
        self.expire_during_reservation(source=True)

    def test_reviewed_catalog_change_after_reservation_is_refused_without_refund(self):
        catalog = self.facts["source_catalog"]
        key = self.auth.source_contract_evidence_id
        def change(event, _snapshot):
            if event == "after_replace":
                catalog[key] = replace(catalog[key], build_number="0.0.124")
        self.ledger.fault_hook = change
        with self.assertRaises(NoExternalCall):
            self.request()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.ledger.validate_ready()["counts"]["apify_start"], 1)


if __name__ == "__main__":
    unittest.main()
