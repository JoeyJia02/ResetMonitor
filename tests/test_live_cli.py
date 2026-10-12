"""Explicit live entry exercised only with public synthetic inputs and fake IO."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from email import policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import shutil
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import main
from monitor.batch import BatchAuthorization, BatchLedger
from monitor.credentials import ProjectCredentials, read_project_credentials
from monitor.deepseek_budget import PRICE_SNAPSHOT_ID, PRICE_VERIFIED_AT, TokenBoundProof
from monitor.live_cli import LiveDependencies, run_live
from monitor.source_contract import SourceContract
from monitor.state import StateStore, initial_state
from monitor.validation import ApifyCostProof
from tests.test_apify import FakeHTTP, page, response, run, tweet
from tests.test_batch import synthetic_authorization
from tests.test_deepseek import QueueTransport, completion, response as deepseek_response
from tests.test_smtp_transport import FakeFactory
from tests.test_classification_setup_probe import FakeApprovedBudget


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("RESET_MONITOR_TEST_ROOT") or ROOT / "runtime" / "test-tmp").resolve()
NOW = PRICE_VERIFIED_AT + timedelta(minutes=1)
SECRET = "synthetic-cli-credential-never-output"


def synthetic_live_inputs(project_root):
    """Artificial reviewed capabilities; they establish no actual platform fact."""
    state_path = project_root / "runtime" / "live-state.json"
    raw = synthetic_authorization(state_path, now=NOW,
                                   ledger_path=project_root / "runtime" / "live-validation-ledger.json")
    raw["target"]["user_id"] = "12345"
    raw["deepseek"].update(model="deepseek-flash", currency="CNY", price_snapshot_id=PRICE_SNAPSHOT_ID)
    raw["apify"].update(amount="1")
    contract = SourceContract(raw["source_contract_evidence_id"], "lance_api~x-tweet-scraper-api", "B" * 17,
                              "0.0.123", "thsottiaux", "12345",
                              ("https://apify.com/lance_api/x-tweet-scraper-api",), "a" * 64,
                              (NOW - timedelta(minutes=1)).isoformat(), (NOW + timedelta(hours=2)).isoformat())
    token_proof = TokenBoundProof("tokenproof-synthetic-cli-full-input", "deepseek-flash",
                                  PRICE_VERIFIED_AT, PRICE_VERIFIED_AT + timedelta(hours=24), lambda _payload: 1000)
    cost_proof = ApifyCostProof(raw["apify"]["bound_evidence_id"], contract.contract_id, contract.build_id,
                                NOW - timedelta(minutes=1), NOW + timedelta(hours=2),
                                ("https://docs.apify.com/api/v2/dataset-items-get",), "b" * 64,
                                Decimal("0.01"), Decimal("0.001"), Decimal("0.002"))
    settings = {"schema_version": 1, "smtp_host": "smtp.example.invalid", "smtp_port": 465,
                "smtp_security": "SSL", "apify_policy": {"available_balance_usd": "1",
                    "max_total_charge_usd": "0.001", "minimum_charge_cap_usd": "0.001", "per_result_usd": "0.0003"}}
    return raw, settings, {contract.contract_id: contract}, {"deepseek-flash": token_proof}, {cost_proof.proof_id: cost_proof}


class LiveCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = TEST_ROOT / ("live-cli-" + uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.project_root = self.directory / "synthetic-project"
        self.project_root.mkdir()
        self.raw, self.settings, self.sources, self.tokens, self.costs = synthetic_live_inputs(self.project_root)
        self.trusted_ledger_path = Path(self.raw["ledger_path"])
        self.authorization_file = self.directory / "authorization.json"
        self.settings_file = self.directory / "public-config.json"
        self.write_inputs()
        self.effects = []
        for name in ("socket.socket", "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            guard = patch(name, side_effect=AssertionError("REAL_EXTERNAL_CALL_FORBIDDEN"))
            guard.start()
            self.addCleanup(guard.stop)

    def cleanup(self):
        if self.directory.resolve().parent != TEST_ROOT:
            raise AssertionError("Refusing cleanup outside isolated test root")
        shutil.rmtree(self.directory)

    def write_inputs(self):
        self.authorization_file.write_text(json.dumps(self.raw), encoding="utf-8")
        self.settings_file.write_text(json.dumps(self.settings), encoding="utf-8")

    def args(self, **updates):
        values = dict(live=True, authorization=self.authorization_file, live_config=self.settings_file,
                      credential_source="env", init_validation_batch=False, dry_run=False,
                      fixture=None, state_file=None, recover_state=False, retry_paused=False)
        values.update(updates)
        return SimpleNamespace(**values)

    def dependencies(self, *, reviewed=True, **updates):
        def forbidden(*_args, **_kwargs):
            self.effects.append("FORBIDDEN")
            raise AssertionError("Public preflight must finish first")
        values = dict(clock=lambda: NOW, credential_reader=forbidden,
                      apify_transport_factory=forbidden, deepseek_transport_factory=forbidden,
                      smtp_factory=forbidden, sleep=lambda _seconds: None, trusted_ledger_path=self.trusted_ledger_path,
                      model_budget_factory=FakeApprovedBudget)
        if reviewed:
            values.update(source_catalog=self.sources, token_proofs=self.tokens, apify_cost_proofs=self.costs)
        values.update(updates)
        return LiveDependencies(**values)

    def invoke(self, args=None, deps=None):
        output, code = run_live(self.args() if args is None else args, project_root=self.project_root,
                                dependencies=self.dependencies() if deps is None else deps)
        self.assertNotIn(SECRET, json.dumps(output))
        return output, code

    def initialized_ledger(self):
        authorization = BatchAuthorization.from_dict(self.raw, now=NOW)
        ledger = BatchLedger(self.trusted_ledger_path, authorization, clock=lambda: NOW)
        ledger.initialize()
        return ledger

    def test_missing_public_arguments_return_all_fixed_codes_before_any_inputs(self):
        output, code = self.invoke(self.args(authorization=None, live_config=None))
        self.assertEqual(code, 2)
        self.assertEqual(output["issue_codes"], ["LIVE_AUTHORIZATION_FILE_REQUIRED", "LIVE_SETTINGS_FILE_REQUIRED"])
        self.assertEqual(self.effects, [])
        self.assertFalse((self.project_root / "runtime").exists())

    def test_default_production_catalogs_block_even_all_synthetic_approval_flags_true(self):
        output, code = self.invoke(deps=self.dependencies(reviewed=False))
        self.assertEqual(code, 2)
        for expected in ("APIFY_SOURCE_CONTRACT_UNVERIFIED", "DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE", "APIFY_TOTAL_COST_BOUND_UNVERIFIED"):
            self.assertIn(expected, output["issue_codes"])
        self.assertEqual(self.effects, [])
        self.assertFalse((self.project_root / "runtime").exists())

    def test_read_only_preflight_never_creates_ledger_state_lock_or_credential_reader(self):
        before = sorted(str(path.relative_to(self.project_root)) for path in self.project_root.rglob("*"))
        output, code = self.invoke(self.args(dry_run=True))
        self.assertEqual((code, output["status"], output["ledger_inspected"]), (0, "PUBLIC_PREFLIGHT_OK", False))
        self.assertEqual(self.effects, [])
        self.assertEqual(before, sorted(str(path.relative_to(self.project_root)) for path in self.project_root.rglob("*")))

    def test_explicit_initialize_is_local_only_and_idempotent_without_secret_input(self):
        output, code = self.invoke(self.args(init_validation_batch=True, credential_source=None))
        self.assertEqual((code, output["status"]), (0, "BATCH_INITIALIZED"))
        path = self.project_root / "runtime" / "live-validation-ledger.json"
        before = path.read_bytes()
        self.invoke(self.args(init_validation_batch=True, credential_source="prompt"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.effects, [])
        self.assertFalse(Path(self.raw["state_path"]).exists())

    def test_copied_project_root_reuses_same_trusted_global_ledger_without_reset(self):
        ledger = self.initialized_ledger()
        ledger.reserve("deepseek_request", amount=Decimal("0.00060720"), currency="CNY",
                       metadata={"price_snapshot_id": PRICE_SNAPSHOT_ID, "model": "deepseek-flash",
                                 "token_proof_id": "tokenproof-synthetic-cli-full-input",
                                 "input_tokens_upper_bound": 1000, "output_tokens_upper_bound": 256})
        before = self.trusted_ledger_path.read_bytes()
        copied_root = self.directory / "copied-project"
        copied_root.mkdir()
        output, code = run_live(self.args(init_validation_batch=True), project_root=copied_root,
                                dependencies=self.dependencies())
        self.assertEqual((code, output["status"]), (0, "BATCH_INITIALIZED"))
        self.assertEqual(self.trusted_ledger_path.read_bytes(), before)
        self.assertEqual(ledger.validate_ready()["counts"]["deepseek_request"], 1)
        self.assertFalse((copied_root / "runtime").exists())
        self.assertEqual(self.effects, [])

    def test_authorization_cannot_choose_fresh_ledger_or_rebind_existing_approval(self):
        ledger = self.initialized_ledger()
        original = deepcopy(self.raw)
        before = self.trusted_ledger_path.read_bytes()
        self.raw["ledger_path"] = str(self.directory / "fresh-ledger.json")
        self.write_inputs()
        output, code = self.invoke(self.args(init_validation_batch=True))
        self.assertEqual((code, output["issue_codes"]), (2, ["LIVE_LEDGER_PATH_BINDING_MISMATCH"]))
        self.assertFalse((self.directory / "fresh-ledger.json").exists())
        for change in ("state_path", "amount"):
            self.raw = deepcopy(original)
            if change == "state_path":
                self.raw["state_path"] = str(self.directory / "another-live-state.json")
            else:
                self.raw["deepseek"]["amount"] = "0.29"
            self.write_inputs()
            output, code = self.invoke(self.args(init_validation_batch=True))
            self.assertEqual((code, output["issue_codes"]), (2, ["VALIDATION_AUTHORIZATION_CHANGED"]))
            self.assertEqual(self.trusted_ledger_path.read_bytes(), before)
        self.assertEqual(ledger.validate_ready()["counts"]["deepseek_request"], 0)
        self.assertEqual(self.effects, [])

    def test_default_global_location_resolver_uses_only_injected_public_os_path_read_only(self):
        public_os_root = self.directory / "synthetic-local-appdata"
        self.raw["ledger_path"] = str(public_os_root / "CodexResetMonitor" / "validation-ledger.json")
        self.write_inputs()
        with patch("monitor.live_cli.os.environ", {"LOCALAPPDATA": str(public_os_root)}):
            output, code = self.invoke(self.args(dry_run=True), deps=self.dependencies(trusted_ledger_path=None))
        self.assertEqual((code, output["status"]), (0, "PUBLIC_PREFLIGHT_OK"))
        self.assertFalse(public_os_root.exists())
        self.assertEqual(self.effects, [])

    def test_normal_live_requires_existing_ledger_and_explicit_credential_source(self):
        output, code = self.invoke()
        self.assertEqual((code, output["issue_codes"]), (2, ["VALIDATION_LEDGER_NOT_INITIALIZED"]))
        self.assertEqual(self.effects, [])
        output, code = self.invoke(self.args(credential_source=None))
        self.assertEqual((code, output["issue_codes"]), (2, ["LIVE_CREDENTIAL_SOURCE_REQUIRED"]))
        self.assertEqual(self.effects, [])

    def test_stored_source_passes_from_main_after_public_gates_and_ready_ledger(self):
        self.initialized_ledger()
        sources = []
        def credential_reader(source):
            sources.append(source)
            # Invalid synthetic return stops before every transport constructor.
            return None
        output = io.StringIO()
        with redirect_stdout(output):
            code = main.main(["--live", "--authorization", str(self.authorization_file),
                              "--live-config", str(self.settings_file), "--credential-source", "stored"],
                             live_dependencies=self.dependencies(credential_reader=credential_reader),
                             project_root=self.project_root)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["issue_codes"], ["PROJECT_CREDENTIAL_INPUT_REQUIRED"])
        self.assertEqual(sources, ["stored"])
        self.assertEqual(self.effects, [])

    def test_stored_store_never_read_on_failed_public_preflight_or_read_only_validation(self):
        module = ModuleType("monitor.credential_store")
        with patch.object(module, "load_credentials", side_effect=AssertionError("No store read allowed"), create=True) as loader, patch.dict(
                "sys.modules", {"monitor.credential_store": module}):
            output, code = self.invoke(self.args(credential_source="stored"),
                                       deps=self.dependencies(reviewed=False, credential_reader=read_project_credentials))
            self.assertEqual((code, output["status"]), (2, "REFUSED"))
            output, code = self.invoke(self.args(credential_source="stored", dry_run=True),
                                       deps=self.dependencies(credential_reader=read_project_credentials))
            self.assertEqual((code, output["status"]), (0, "PUBLIC_PREFLIGHT_OK"))
        loader.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_stored_missing_credentials_fail_before_transport_factories_without_prompt(self):
        self.initialized_ledger()
        module = ModuleType("monitor.credential_store")
        with patch.object(module, "load_credentials", return_value=None, create=True) as loader, patch.dict(
                "sys.modules", {"monitor.credential_store": module}), patch(
                "monitor.credentials.getpass.getpass", side_effect=AssertionError("No prompt allowed")) as prompt:
            output, code = self.invoke(self.args(credential_source="stored"),
                                       deps=self.dependencies(credential_reader=read_project_credentials))
        self.assertEqual((code, output["issue_codes"]), (2, ["PROJECT_CREDENTIAL_INPUT_REQUIRED"]))
        loader.assert_called_once_with()
        prompt.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_public_json_duplicates_unknown_keys_oversize_and_secrets_are_refused_without_echo(self):
        cases = ("{\"schema_version\":1,\"schema_version\":1}", "x" * 65_537,
                 json.dumps({**self.raw, "deepseek_api_key": SECRET}))
        for text in cases:
            self.authorization_file.write_text(text, encoding="utf-8")
            output, code = self.invoke()
            self.assertEqual(code, 2)
            self.assertEqual(self.effects, [])
            self.assertNotIn(SECRET, json.dumps(output))
        self.write_inputs()
        for mutate in (lambda value: value.update(password=SECRET), lambda value: value["apify_policy"].update(source_contract_verified=True),
                       lambda value: value.update(smtp_port=True), lambda value: value["apify_policy"].update(per_result_usd="NaN")):
            settings = deepcopy(self.settings)
            mutate(settings)
            self.settings_file.write_text(json.dumps(settings), encoding="utf-8")
            output, code = self.invoke()
            self.assertEqual((code, output["issue_codes"]), (2, ["LIVE_SETTINGS_SCHEMA_INVALID"]))
            self.assertEqual(self.effects, [])

    def test_expired_authorization_price_or_each_reviewed_contract_blocks_before_credentials(self):
        for instant in (NOW + timedelta(hours=3), NOW - timedelta(minutes=2)):
            output, code = self.invoke(deps=self.dependencies(clock=lambda instant=instant: instant))
            self.assertEqual(code, 2)
            self.assertEqual(self.effects, [])
        for change in ({"source_catalog": {}}, {"token_proofs": {}}, {"apify_cost_proofs": {}}):
            output, code = self.invoke(deps=self.dependencies(**change))
            self.assertEqual(code, 2)
            self.assertEqual(self.effects, [])

    def test_live_state_paths_cannot_alias_offline_scheduled_demo_ledger_or_public_inputs(self):
        candidates = (self.project_root / "state.json", self.project_root / "runtime" / "offline-state.json",
                      self.project_root / "runtime" / "scheduled-offline-state.json", self.project_root / "runtime" / "demo-state.json",
                      self.project_root / "runtime" / "live-validation-ledger.json", self.authorization_file, self.settings_file,
                      self.project_root / "examples" / "sample.json")
        for path in candidates:
            self.raw["state_path"] = str(path)
            self.write_inputs()
            output, code = self.invoke()
            expected = "VALIDATION_LEDGER_STATE_PATH_CONFLICT" if path == Path(self.raw["ledger_path"]) else "LIVE_STATE_PATH_CONFLICT"
            self.assertEqual((code, output["issue_codes"]), (2, [expected]))
            self.assertEqual(self.effects, [])

    def test_existing_offline_legacy_identity_mismatch_and_recovery_states_are_read_only_refused(self):
        state_path = Path(self.raw["state_path"])
        state_path.parent.mkdir(parents=True)
        for mode, identifier, hold, expected in (("OFFLINE", None, False, "LIVE_STATE_ORIGIN_UNSAFE"),
                                                  ("LIVE", None, False, "LIVE_STATE_IDENTITY_MISMATCH"),
                                                  ("LIVE", "99999", False, "LIVE_STATE_IDENTITY_MISMATCH"),
                                                  ("LIVE", "12345", True, "LIVE_STATE_RECOVERY_REVIEW_REQUIRED")):
            state = initial_state("thsottiaux", execution_mode=mode, target_user_id=identifier)
            if hold:
                state["recovery_required"] = True
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before = state_path.read_bytes()
            output, code = self.invoke(self.args(dry_run=True))
            self.assertEqual((code, output["issue_codes"]), (2, [expected]))
            self.assertEqual(state_path.read_bytes(), before)
            self.assertEqual(self.effects, [])
            self.assertFalse(state_path.with_name(state_path.name + ".lock").exists())

    def test_corrupt_or_missing_main_with_backup_is_not_recovered_by_live_entry(self):
        state_path = Path(self.raw["state_path"])
        state_path.parent.mkdir(parents=True)
        backup = state_path.with_name(state_path.name + ".bak")
        backup.write_text(json.dumps(initial_state("thsottiaux", execution_mode="LIVE", target_user_id="12345")), encoding="utf-8")
        output, code = self.invoke()
        self.assertEqual((code, output["issue_codes"]), (2, ["LIVE_STATE_READ_INVALID"]))
        self.assertFalse(state_path.exists())
        self.assertEqual(self.effects, [])

    def test_existing_hard_link_alias_of_offline_state_is_refused_before_credentials(self):
        state_path = Path(self.raw["state_path"])
        state_path.parent.mkdir(parents=True)
        offline = self.project_root / "runtime" / "offline-state.json"
        offline.write_text(json.dumps(initial_state("thsottiaux")), encoding="utf-8")
        state_path.hardlink_to(offline)
        before = offline.read_bytes()
        output, code = self.invoke()
        self.assertEqual((code, output["issue_codes"]), (2, ["LIVE_STATE_PATH_CONFLICT"]))
        self.assertEqual(offline.read_bytes(), before)
        self.assertEqual(self.effects, [])

    def test_monitor_rechecks_state_origin_under_lock_after_credential_input_race(self):
        ledger = self.initialized_ledger()
        state_path = Path(self.raw["state_path"])
        store = StateStore(state_path)
        store.save(initial_state("thsottiaux", execution_mode="LIVE", target_user_id="12345"))
        apify, deepseek, smtp = FakeHTTP(), QueueTransport(), FakeFactory()
        def credential_reader(_source):
            changed = json.loads(state_path.read_text(encoding="utf-8"))
            changed["execution_mode"] = "OFFLINE"
            state_path.write_text(json.dumps(changed), encoding="utf-8")
            return ProjectCredentials(apify_token=SECRET, deepseek_api_key=SECRET, smtp_user=SECRET, smtp_password=SECRET)
        output, code = self.invoke(deps=self.dependencies(credential_reader=credential_reader,
                                  apify_transport_factory=lambda: apify, deepseek_transport_factory=lambda: deepseek,
                                  smtp_factory=smtp))
        self.assertEqual((code, output["issue_codes"]), (2, ["LIVE_STATE_READ_INVALID"]))
        self.assertEqual((apify.calls, deepseek.calls, smtp.calls), ([], [], []))
        self.assertEqual(ledger.validate_ready()["counts"]["apify_start"], 0)
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["execution_mode"], "OFFLINE")

    def test_credential_failure_returns_fixed_code_before_any_transport_factory(self):
        self.initialized_ledger()
        def invalid(_source):
            self.effects.append("credential-read")
            return {"secret": SECRET}
        output, code = self.invoke(deps=self.dependencies(credential_reader=invalid))
        self.assertEqual((code, output["issue_codes"]), (2, ["PROJECT_CREDENTIAL_INPUT_REQUIRED"]))
        self.assertEqual(self.effects, ["credential-read"])

    def test_main_explicit_live_argument_and_default_offline_boundaries(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main.main(["--live"], live_dependencies=self.dependencies(), project_root=self.project_root)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["mode"], "LIVE_VALIDATION")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.main(["--authorization", str(self.authorization_file)], project_root=self.project_root)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.main(["--live", "--deepseek-api-key", SECRET], project_root=self.project_root)
        self.assertEqual(self.effects, [])

    def test_argparse_never_echoes_unknown_secret_flags_or_invalid_option_values(self):
        for arguments in (["--live", "--credential-source", SECRET], ["--live", "--smtp-password", SECRET],
                          ["--" + SECRET, SECRET], ["--live", "--deepseek-api-key=" + SECRET]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                main.main(arguments, live_dependencies=self.dependencies(), project_root=self.project_root)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(stderr.getvalue(), "ResetMonitor: error: CLI_ARGUMENT_ERROR\n")
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(self.effects, [])

    def test_live_and_offline_mutation_flags_are_rejected_before_public_file_reads(self):
        for change in ({"fixture": Path("synthetic.json")}, {"state_file": "custom.json"}, {"recover_state": True},
                       {"retry_paused": True}, {"dry_run": True, "init_validation_batch": True}):
            output, code = self.invoke(self.args(**change))
            self.assertEqual(code, 2)
            self.assertEqual(self.effects, [])
            self.assertFalse((self.project_root / "runtime").exists())

    def test_main_actual_guarded_adapters_fake_wire_new_three_from_live_baseline_and_quota_stop(self):
        self.invoke(self.args(init_validation_batch=True))
        state = initial_state("thsottiaux", execution_mode="LIVE", target_user_id="12345")
        state.update(initialized=True, initial_baseline_id="100", last_seen_id="100")
        StateStore(Path(self.raw["state_path"])).save(state)
        transports = []
        def apify_factory():
            self.effects.append("apify-factory")
            rows = [tweet(identifier, authorId="12345") for identifier in (103, 102, 101)]
            run_id = ("R" if not transports else "S") * 17
            dataset_id = ("D" if not transports else "E") * 17
            start = response(201, {"data": {"id": run_id, "status": "RUNNING", "defaultDatasetId": dataset_id, "buildId": "B" * 17}})
            poll = response(200, {"data": {"id": run_id, "status": "SUCCEEDED", "defaultDatasetId": dataset_id, "buildId": "B" * 17}})
            transport = FakeHTTP(start, poll, page(rows))
            transports.append(transport)
            return transport
        deepseek = QueueTransport(deepseek_response(), deepseek_response(completion("IRRELEVANT")),
                                  deepseek_response(completion("POSSIBLE_RESET")))
        smtp = FakeFactory()
        def credentials(_source):
            self.effects.append("credential-read")
            return ProjectCredentials(apify_token=SECRET, deepseek_api_key=SECRET, smtp_user=SECRET, smtp_password=SECRET)
        deps = self.dependencies(credential_reader=credentials, apify_transport_factory=apify_factory,
                                 deepseek_transport_factory=lambda: deepseek, smtp_factory=smtp)
        def actual_main():
            output = io.StringIO()
            with redirect_stdout(output):
                code = main.main(["--live", "--authorization", str(self.authorization_file), "--live-config", str(self.settings_file),
                                  "--credential-source", "env"], live_dependencies=deps, project_root=self.project_root)
            self.assertNotIn(SECRET, output.getvalue())
            return json.loads(output.getvalue()), code
        new, new_exit = actual_main()
        self.assertEqual(new_exit, 0)
        self.assertEqual((new["classified"], new["sent"]), (3, 2))
        self.assertEqual(len(deepseek.calls), 3)
        self.assertEqual(len(smtp.calls), 2)
        self.assertEqual([call["method"] for transport in transports for call in transport.calls], ["POST", "GET", "GET"])
        for data in smtp.session.payloads:
            message = BytesParser(policy=policy.default).parsebytes(data)
            self.assertTrue(message["Subject"].startswith("【ResetMonitor 测试】"))
        ledger = self.initialized_ledger()
        entry = ledger.validate_ready()
        self.assertEqual(entry["counts"]["deepseek_request"], 3)
        self.assertEqual(entry["counts"]["smtp_attempt"], 2)
        self.assertEqual(entry["counts"]["apify_start"], 1)
        third, third_exit = actual_main()
        self.assertEqual(third_exit, 1)
        self.assertEqual(len(transports[-1].calls), 0, "Exhausted run-start allowance cannot POST")
        self.assertEqual(len(deepseek.calls), 3)
        self.assertEqual(len(smtp.calls), 2)


if __name__ == "__main__":
    unittest.main()
