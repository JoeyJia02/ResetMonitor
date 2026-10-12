"""Offline verification of the user-authorized one-entry full-chain test."""

from contextlib import ExitStack
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import getpass
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import warnings

from scripts import test_full_chain_once as probe
from tests.test_classification_setup_probe import FakeResponse, FakeTransport, FakeApprovedBudget
from tests.test_smtp_transport import FakeFactory, FakeSession
from tests.test_one_shot_source import Response, Transport, official_embed


class FullChainOnceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("REAL_NETWORK_FORBIDDEN")))
        for stream in ("stdin", "stderr"):
            self.stack.enter_context(patch.object(probe.sys, stream, SimpleNamespace(isatty=lambda: True)))
        self.test_root = Path(os.environ.get("RESET_MONITOR_TEST_ROOT",
                              str(Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"))).resolve()
        self.workspace = self.test_root / ("full-chain-" + uuid4().hex)
        self.workspace.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.directory = self.workspace / "once"
        self.output = io.StringIO()
        self.secrets = ("SYNTHETIC_APIFY_SECRET", "SYNTHETIC_DEEPSEEK_SECRET", "abcdefghijklmnop")
        self.now = datetime(2026, 10, 11, 10, tzinfo=timezone.utc)
        self.active_run = probe.PLAN_ID
        self.tweets = [{"id": str(2109152716954222850 + index), "author": "thsottiaux",
                        "author_id": "1953337039510003712", "created_at": "2026-10-11T07:00:00Z",
                        "text": "完整新原文" + str(index), "url": "https://x.com/thsottiaux/status/" + str(2109152716954222850 + index),
                        "context": {"quoted_tweet": {"text": "全部引用内容"}, "poll": {"options": ["A", "B"]}}}
                       for index in range(3)]
        self.source_calls = []
        self.factory = FakeFactory()
        self.transport = FakeTransport(FakeResponse(self.response()))
        self.saved = []

    def cleanup(self):
        root = self.test_root
        self.assertTrue(self.workspace.resolve().is_relative_to(root))
        self.assertNotEqual(self.workspace.resolve(), root)
        shutil.rmtree(self.workspace)

    def response(self):
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
            "results": [{"id": str(index), "label": "IRRELEVANT", "reset_time": None, "reason": "没有确认重置；时间不明。"}
                        for index, _ in enumerate(self.tweets, 1)]})}}],
                "usage": {"prompt_tokens": 1500, "completion_tokens": 150, "total_tokens": 1650}}

    def source(self, token, transport, clock):
        attempt = json.loads(probe._path(self.directory, "attempt", self.active_run).read_text())
        receipt = json.loads(probe._path(self.directory, "receipt", self.active_run).read_text())
        self.assertEqual(attempt["maximum_source_runs"], 1)
        self.assertEqual(receipt["source_attempts"], 1)
        self.assertEqual(token, self.secrets[0])
        self.source_calls.append(token)
        return self.tweets, {"source_posts": 1, "official_context_gets": len(self.tweets)}

    def run_once(self, args=None, **overrides):
        self.active_run = args[2] if args and len(args) >= 3 and args[:2] == ["--run", "--run-id"] else probe.PLAN_ID
        credentials = iter(self.secrets)
        options = dict(output=self.output, hidden_input=lambda _: next(credentials),
                       clock=lambda: self.now, directory=self.directory,
                       source_fetch=self.source, transport=self.transport, smtp_factory=self.factory,
                       credential_loader=lambda: None, credential_saver=self.saved.append,
                       model_budget=FakeApprovedBudget())
        options.update(overrides)
        return probe.main(["--run"] if args is None else args, **options)

    def assert_no_secrets(self):
        combined = self.output.getvalue()
        if self.directory.exists():
            combined += "".join(path.read_text(encoding="utf-8", errors="replace")
                                for path in self.directory.iterdir())
        for secret in self.secrets:
            self.assertNotIn(secret, combined)

    def test_preview_invalid_noninteractive_expired_no_secret_or_network(self):
        with patch.object(probe, "_directory", side_effect=AssertionError("NO_ENV")):
            for args in ([], ["--preview"], ["--help"]):
                self.assertEqual(self.run_once(args, hidden_input=lambda _: self.fail("NO_SECRET")), 0)
        self.assertEqual(self.run_once([self.secrets[0]]), 2)
        with patch.object(probe.sys, "stdin", SimpleNamespace(isatty=lambda: False)):
            self.assertEqual(self.run_once(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(self.run_once(clock=lambda: probe.batch.EXPIRES_AT,
                                      hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.transport.calls, [])
        self.assert_no_secrets()

    def test_dynamic_three_one_source_model_email_all_false_and_no_repeat(self):
        def model_before():
            receipt = json.loads(probe._path(self.directory, "receipt").read_text())
            self.assertEqual(receipt["model_attempts"], 1)
        self.transport.before = model_before
        original = self.factory
        def smtp_factory(*args, **kwargs):
            receipt = json.loads(probe._path(self.directory, "receipt").read_text())
            self.assertEqual(receipt["email_attempts"], 1)
            return original(*args, **kwargs)
        self.assertEqual(self.run_once(smtp_factory=smtp_factory), 0)
        self.assertEqual(len(self.source_calls), 1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.factory.calls), 1)
        request = self.transport.calls[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, probe.batch.URL)
        body = json.loads(request.data)
        submitted = json.loads(body["messages"][1]["content"])
        self.assertEqual(set(submitted), {"observed_at", "timezone", "tweets"})
        self.assertEqual(submitted["tweets"], [{
            "id": str(index), "created_at": row["created_at"], "text": row["text"],
            "quoted_tweet": {"text": "全部引用内容"}, "poll_options": ["A", "B"],
        } for index, row in enumerate(self.tweets, 1)])
        self.assertNotIn("source_current_three_user_verified", submitted)
        self.assertNotIn("source_status", submitted)
        self.assertNotIn("人工核对", body["messages"][0]["content"])
        message = BytesParser(policy=policy.default).parsebytes(self.factory.session.payloads[0][:-3])
        self.assertEqual(str(message["To"]), probe.RECIPIENT)
        self.assertIn("全链路测试", str(message["Subject"]))
        text = message.get_content()
        for row in self.tweets:
            self.assertIn(row["text"], text)
            self.assertIn(row["url"], text)
        receipt = json.loads(probe._path(self.directory, "receipt").read_text())
        self.assertEqual(receipt["status"], "FULL_CHAIN_OK")
        result = json.loads(probe._path(self.directory, "result").read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in result["results"]], [row["id"] for row in self.tweets])
        self.assertFalse(receipt["inbox_delivery_confirmed"])
        self.assertEqual(self.run_once(hidden_input=lambda _: self.fail("NO_SECRET")), 2)
        self.assertEqual(len(self.factory.calls), 1)
        self.assert_no_secrets()

    def test_collect_all_credentials_before_source_and_echo_fallback_closed(self):
        prompts = []
        def hidden(prompt):
            prompts.append(prompt)
            return self.secrets[len(prompts) - 1]
        def source(*args):
            self.assertEqual(len(prompts), 3)
            return self.source(*args)
        self.assertEqual(self.run_once(hidden_input=hidden, source_fetch=source), 0)
        self.assertEqual(len(self.saved), 1)
        self.assert_no_secrets()

    def test_saved_credentials_run_noninteractive_without_prompt_or_rewrite(self):
        from monitor.credentials import ProjectCredentials
        credentials = ProjectCredentials(apify_token=self.secrets[0], deepseek_api_key=self.secrets[1],
                                         smtp_user=probe.SENDER, smtp_password=self.secrets[2])
        with patch.object(probe.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
             patch.object(probe.sys, "stderr", SimpleNamespace(isatty=lambda: False)):
            self.assertEqual(self.run_once(credential_loader=lambda: credentials,
                                          hidden_input=lambda _: self.fail("NO_PROMPT"),
                                          credential_saver=lambda _: self.fail("NO_REWRITE")), 0)
        self.assertEqual(len(self.factory.calls), 1)
        self.assert_no_secrets()

    def test_store_write_failure_stops_before_paid_run_and_keeps_no_attempt(self):
        def fail(_):
            raise probe.CredentialStoreError("CREDENTIAL_STORE_WRITE_FAILED")
        self.assertEqual(self.run_once(credential_saver=fail), 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.source_calls, [])
        self.assertEqual(self.factory.calls, [])
        self.assert_no_secrets()

    def test_invalid_credentials_and_getpass_failure_no_reservation(self):
        self.assertEqual(self.run_once(hidden_input=lambda _: "bad"), 2)
        def fallback(_):
            warnings.warn(self.secrets[0], getpass.GetPassWarning)
        self.assertEqual(self.run_once(hidden_input=fallback), 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.source_calls, [])
        self.assert_no_secrets()

    def test_source_failure_does_not_model_or_send_and_hold_persists(self):
        def fail(*args):
            raise probe.OneShotSourceError("ONE_SHOT_SOURCE_ROW_FAILED")
        self.assertEqual(self.run_once(source_fetch=fail), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.factory.calls, [])
        self.assertEqual(self.run_once(), 2)
        self.assert_no_secrets()

    def test_model_wrong_ids_does_not_send_or_repeat(self):
        document = self.response()
        content = json.loads(document["choices"][0]["message"]["content"])
        content["results"][0]["id"] = "old-wrong-id"
        document["choices"][0]["message"]["content"] = json.dumps(content)
        self.assertEqual(self.run_once(transport=FakeTransport(FakeResponse(document))), 1)
        self.assertEqual(self.factory.calls, [])
        self.assertEqual(self.run_once(), 2)
        self.assert_no_secrets()

    def test_smtp_uncertainty_keeps_attempt_and_never_resends(self):
        self.factory = FakeFactory(FakeSession(send=OSError(self.secrets[2])))
        self.assertEqual(self.run_once(), 1)
        receipt = json.loads(probe._path(self.directory, "receipt").read_text())
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["smtp_status"], "UNKNOWN")
        self.assertEqual(self.run_once(), 2)
        self.assertEqual(len(self.factory.calls), 1)
        self.assert_no_secrets()

    def test_empty_source_sends_test_skips_model_does_not_claim_full_pass(self):
        self.tweets = []
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(len(self.factory.calls), 1)
        self.assertIn("FULL_CHAIN_EMPTY", self.output.getvalue())
        self.assert_no_secrets()

    def test_secret_reflection_and_oversize_body_block_model_and_email(self):
        self.tweets[0]["text"] = self.secrets[0]
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.factory.calls, [])
        self.assertFalse(probe._path(self.directory, "result").exists())
        self.assert_no_secrets()

    def test_reservation_write_failure_cannot_reach_source(self):
        with patch.object(probe.batch, "_save", side_effect=OSError(self.secrets[0])):
            self.assertEqual(self.run_once(), 2)
        self.assertEqual(self.source_calls, [])
        self.assertEqual(self.factory.calls, [])
        self.assert_no_secrets()

    def test_observed_source_shape_through_real_parser_batch_and_mail(self):
        root = Path(__file__).resolve().parents[1]
        rows = json.loads((root / "tests/fixtures/source-items.json").read_text(encoding="utf-8"))
        enriched = json.loads((root / "tests/fixtures/source-context.json").read_text(encoding="utf-8"))
        results = [{"id": str(index), "label": "IRRELEVANT", "reset_time": None, "reason": "测试分析；时间不明。"}
                   for index, _ in enumerate(rows, 1)]
        document = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"results": results})}}],
                    "usage": {"prompt_tokens": 1800, "completion_tokens": 180, "total_tokens": 1980}}
        transport = Transport([Response(rows, status=201)] +
                              [Response(official_embed(item)) for item in enriched["items"]] +
                              [FakeResponse(document)])
        self.assertEqual(self.run_once(source_fetch=probe.fetch_three, transport=transport), 0)
        methods = [call[0].get_method() for call in transport.calls]
        self.assertEqual(methods, ["POST", "GET", "GET", "GET", "POST"])
        submitted = json.loads(json.loads(transport.calls[-1][0].data)["messages"][1]["content"])
        self.assertEqual(len(submitted["tweets"]), 3)
        self.assertEqual(submitted["tweets"][2]["poll_options"],
                         ["Synthetic option A", "Synthetic option B", "Show test results"])
        self.assertEqual(set(submitted["tweets"][2]), {"id", "created_at", "text", "poll_options"})
        for row in submitted["tweets"][:2]:
            self.assertEqual(set(row), {"id", "created_at", "text", "quoted_tweet"})
            self.assertEqual(set(row["quoted_tweet"]), {"text", "created_at"})
        self.assertEqual(len(self.factory.calls), 1)
        self.assert_no_secrets()

    def test_new_run_preserves_legacy_records_and_separates_ids_results_and_messages(self):
        self.directory.mkdir(parents=True)
        legacy = {probe._path(self.directory, kind): ("historical-" + kind).encode()
                  for kind in ("attempt", "receipt", "result")}
        for path, raw in legacy.items():
            path.write_bytes(raw)
        identifiers = ["full-chain-" + uuid4().hex for _ in range(2)]
        for run_id in identifiers:
            self.assertEqual(self.run_once(["--run", "--run-id", run_id, "--confirmed"],
                transport=FakeTransport(FakeResponse(self.response())), smtp_factory=FakeFactory()), 0)
            result = json.loads(probe._path(self.directory, "result", run_id).read_text("utf-8"))
            self.assertEqual(result["plan_id"], run_id)
            self.assertEqual(result["notification"]["message_id"], "<" + run_id + "@qq.com>")
            before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
            self.assertEqual(self.run_once(["--run", "--run-id", run_id, "--confirmed"],
                credential_loader=lambda: self.fail("NO_CREDENTIAL_READ")), 2)
            self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})
        self.assertEqual(len(self.source_calls), 2)
        for path, raw in legacy.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assert_no_secrets()

    def test_new_run_without_confirmation_stops_before_budget_credentials_or_network(self):
        run_id = "full-chain-" + uuid4().hex
        self.assertEqual(self.run_once(["--run", "--run-id", run_id],
            model_budget=SimpleNamespace(preflight=lambda *_: self.fail("NO_BUDGET_CHANGE")),
            credential_loader=lambda: self.fail("NO_CREDENTIAL_READ")), 2)
        self.assertIn("FULL_CHAIN_CONFIRMATION_REQUIRED", self.output.getvalue())
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.source_calls + self.transport.calls + self.factory.calls, [])

    def test_new_run_plan_discloses_cost_gap_without_permanently_blocking_manual_tests(self):
        budget = SimpleNamespace(summary=lambda: {
            'limit': '1.00', 'max_requests': 1, 'committed_cny': '0.001676', 'requests': 1})
        plan = probe.new_run_plan(model_budget=budget, clock=lambda: self.now)
        self.assertTrue(plan['ready'])
        self.assertEqual(plan['approval_mode'], 'per_manual_run')
        self.assertIn('FULL_CHAIN_SOURCE_COST_UNVERIFIED', plan['warnings'])
        self.assertEqual(plan['reasons'], [])
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.source_calls + self.transport.calls + self.factory.calls, [])

    def test_failed_run_is_not_restarted_and_new_run_does_not_recover_its_source(self):
        failed_id, next_id = ["full-chain-" + uuid4().hex for _ in range(2)]
        def fail(*_):
            raise probe.OneShotSourceError("ONE_SHOT_SOURCE_ROW_FAILED")
        self.assertEqual(self.run_once(["--run", "--run-id", failed_id, "--confirmed"], source_fetch=fail), 1)
        old = {path: path.read_bytes() for path in self.directory.iterdir() if failed_id in path.name}
        self.assertEqual(self.run_once(["--run", "--run-id", failed_id, "--confirmed"],
            credential_loader=lambda: self.fail("NO_CREDENTIAL_READ")), 2)
        self.assertEqual(self.run_once(["--run", "--run-id", next_id, "--confirmed"]), 0)
        for path, raw in old.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(len(self.source_calls), 1)

    def test_global_test_lock_blocks_another_new_id_before_source(self):
        run_id = "full-chain-" + uuid4().hex
        with probe.ProcessLock(self.directory / "full-chain.lock"):
            self.assertEqual(self.run_once(["--run", "--run-id", run_id, "--confirmed"]), 2)
        self.assertIn("FULL_CHAIN_BUSY", self.output.getvalue())
        self.assertEqual(self.source_calls + self.transport.calls + self.factory.calls, [])
        self.assertFalse(probe._path(self.directory, "attempt", run_id).exists())

    def test_interrupted_new_run_attempt_remains_consumed_after_restart(self):
        run_id = "full-chain-" + uuid4().hex
        self.directory.mkdir(parents=True)
        marker = probe._path(self.directory, "attempt", run_id)
        marker.write_text('{"status":"ATTEMPT_RESERVED"}', encoding="utf-8")
        before = marker.read_bytes()
        self.assertEqual(self.run_once(["--run", "--run-id", run_id, "--confirmed"],
            credential_loader=lambda: self.fail("NO_CREDENTIAL_READ")), 2)
        self.assertEqual(marker.read_bytes(), before)
        self.assertIn("FULL_CHAIN_RUN_ALREADY_ATTEMPTED", self.output.getvalue())
        self.assertEqual(self.source_calls + self.transport.calls + self.factory.calls, [])


if __name__ == "__main__":
    unittest.main()
