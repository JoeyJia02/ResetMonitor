"""Subprocess acceptance tests for the offline CLI, with sockets disabled."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN = PROJECT_ROOT / "main.py"
TEST_TEMP_ROOT = Path(
    os.environ.get("RESET_MONITOR_TEST_ROOT") or PROJECT_ROOT / "runtime" / "test-tmp"
).resolve()

# run_path preserves the real __main__/argparse entry point. Disable network
# constructors before it runs, so an accidental adapter cannot contact a server.
_CHILD_BOOTSTRAP = """
import os
from pathlib import Path
import runpy
import smtplib
import socket
import sys

# Independently check the child environment, rather than trusting the parent's
# filtering implementation. Only a fixed public recipient is allowed here.
if os.environ.get('MONITOR_TEST_VERIFY_CREDENTIAL_ISOLATION') == '1':
    absent = (
        'TWITTER_API_KEY', 'APIFY_TOKEN', 'DEEPSEEK_API_KEY', 'SMTP_PASSWORD',
        'SMTP_USER', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AWS_ACCESS_KEY_ID',
        'AZURE_CLIENT_SECRET', 'CUSTOM_API_KEY', 'CUSTOM_TOKEN',
        'CUSTOM_PASSWORD', 'CUSTOM_SECRET',
    )
    if (any(name in os.environ for name in absent)
            or os.environ.get('EMAIL_TO') != 'owner@example.invalid'
            or os.environ.get('MONITOR_TEST_PUBLIC_CONTROL') != 'permitted-offline-control'
            or any('RESET_MONITOR_TEST_SENTINEL_' in value for value in os.environ.values())):
        sys.stderr.write('CREDENTIAL_ISOLATION_FAILED\\n')
        raise SystemExit(93)
    sys.stderr.write('CREDENTIAL_ISOLATION_VERIFIED\\n')

def forbidden_network(*args, **kwargs):
    sys.stderr.write('FORBIDDEN_NETWORK_ATTEMPT\\n')
    raise RuntimeError('Network disabled by offline CLI acceptance test')

socket.socket = forbidden_network
socket.create_connection = forbidden_network
smtplib.SMTP = forbidden_network
smtplib.SMTP_SSL = forbidden_network
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
guard = os.environ.get('MONITOR_TEST_SERVICE_GUARD')
if guard:
    from monitor.fakes import FakeFetcher, FakeClassifier, FakeNotifier
    def forbidden_service(*args, **kwargs):
        sys.stderr.write('FORBIDDEN_SERVICE_ATTEMPT\\n')
        raise RuntimeError('Service disabled by read-only/recovery test')
    FakeClassifier.classify = forbidden_service
    FakeNotifier.send = forbidden_service
    if guard == 'ALL':
        FakeFetcher.fetch_latest = forbidden_service
runpy.run_path(sys.argv[0], run_name='__main__')
"""


def tweet(identifier: int) -> dict:
    return {
        "id": str(identifier), "author": "thsottiaux",
        "text": f"原文 {identifier}\n第二行完整保留。",
        "url": f"https://x.com/thsottiaux/status/{identifier}",
        "created_at": f"2026-10-10T08:{identifier - 100:02d}:00Z",
        "context": None,
    }


def classification(label: str) -> dict:
    return {"label": label, "confidence": 0.9, "reset_time": None, "reason": "离线人工标注样本。"}


class CLITests(unittest.TestCase):
    def test_cli_remains_offline_and_rejects_live_state_before_services(self):
        fixture = self.fixture("origin-baseline", [tweet(100)])
        completed = self.run_cli("--fixture", fixture, "--state-file", self.state_file,
                                 env_changes={"EXECUTION_MODE": "LIVE"})
        self.assertEqual(completed.returncode, 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["execution_mode"], "OFFLINE")
        state["execution_mode"] = "LIVE"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        before = self.state_file.read_bytes()
        rejected = self.run_cli("--fixture", fixture, "--state-file", self.state_file, service_guard="ALL")
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(self.state_file.read_bytes(), before)

    def setUp(self):
        # Python 3.14's tempfile directory ACL is unsuitable for this sandbox.
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.case_root = TEST_TEMP_ROOT / uuid4().hex
        self.case_root.mkdir()
        self.addCleanup(self._remove_case_directory)
        self.state_file = self.case_root / "state.json"

    def _remove_case_directory(self):
        base = TEST_TEMP_ROOT.resolve()
        target = self.case_root.resolve()
        if target == base or not target.is_relative_to(base):
            raise AssertionError("Refusing cleanup outside the test directory")
        if target.exists():
            shutil.rmtree(target)

    def fixture(self, name: str, tweets: list, **options) -> Path:
        path = self.case_root / f"{name}.json"
        path.write_text(json.dumps({"tweets": tweets, **options}, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, *args, env_changes=None, cwd=None, service_guard=None, main_path=None):
        main_path = Path(main_path or MAIN).resolve()
        env = dict(os.environ)
        # Child tests need no account credentials, including unrelated API keys.
        for key in list(env):
            upper = key.upper()
            if upper.startswith(("TWITTER_", "APIFY_", "DEEPSEEK_", "SMTP_", "EMAIL_", "OPENAI_", "ANTHROPIC_", "AWS_", "AZURE_")) or upper.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET")):
                env.pop(key, None)
        for key in ("STATE_FILE", "TARGET_USERNAME", "TARGET_USER_ID", "INCLUDE_REPLIES", "EXCLUDE_RETWEETS", "EMAIL_TO", "FETCH_LIMIT", "CHECK_INTERVAL_HOURS", "SCHEDULE_TIMEZONE", "SCHEDULE_TIMES", "MONITOR_TEST_SERVICE_GUARD"):
            env.pop(key, None)
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "EMAIL_TO": "owner@example.invalid", "FETCH_LIMIT": "3",
            "SCHEDULE_TIMEZONE": "Asia/Shanghai", "SCHEDULE_TIMES": "08:00,13:00,20:00", "TARGET_USERNAME": "thsottiaux",
        })
        env.update(env_changes or {})
        if service_guard:
            env["MONITOR_TEST_SERVICE_GUARD"] = service_guard
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD_BOOTSTRAP, str(main_path), *(str(arg) for arg in args)],
            cwd=cwd or main_path.parent, env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=30, check=False,
        )
        self.assertNotIn("FORBIDDEN_NETWORK_ATTEMPT", completed.stderr)
        self.assertNotIn("FORBIDDEN_SERVICE_ATTEMPT", completed.stderr)
        return completed

    def result(self, completed, expected_code=0):
        self.assertEqual(completed.returncode, expected_code, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def assert_no_state_artifacts(self, state_file=None):
        path = state_file or self.state_file
        for candidate in (path, Path(str(path) + ".bak"), Path(str(path) + ".lock")):
            self.assertFalse(candidate.exists(), str(candidate))

    def baseline(self):
        path = self.fixture("baseline", [tweet(100)], classifications={"100": classification("PRE_RESET")})
        return self.run_cli("--fixture", path, "--state-file", self.state_file, service_guard="PROCESSING")

    def test_missing_fixture_refuses_to_create_state(self):
        completed = self.run_cli("--state-file", self.state_file, service_guard="ALL")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("Live adapters are disabled", completed.stderr)
        self.assert_no_state_artifacts()

    def test_stored_credentials_require_explicit_live_without_changing_offline_default(self):
        completed = self.run_cli("--credential-source", "stored", "--state-file", self.state_file,
                                 service_guard="ALL")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("Live validation arguments require explicit --live.", completed.stderr)
        self.assert_no_state_artifacts()

    def test_baseline_then_latest_three_then_repeat_sends_zero_two_zero(self):
        first = self.result(self.baseline())
        self.assertEqual((first["ingested"], first["classified"], first["fake_deliveries"]), (0, 0, 0))
        latest = [tweet(103), tweet(101), tweet(102)]  # Provider order need not be trusted.
        path = self.fixture("latest", latest, classifications={
            "101": classification("PRE_RESET"), "102": classification("POST_RESET"),
            "103": classification("IRRELEVANT"),
        })
        second = self.result(self.run_cli("--fixture", path, "--state-file", self.state_file))
        self.assertEqual((second["ingested"], second["classified"], second["fake_deliveries"]), (3, 3, 2))
        repeated = self.fixture("repeated", latest, classifications={})
        third = self.result(self.run_cli(
            "--fixture", repeated, "--state-file", self.state_file, service_guard="PROCESSING",
        ))
        self.assertEqual((third["ingested"], third["classified"], third["fake_deliveries"]), (0, 0, 0))
        for output in (first, second, third):
            self.assertEqual(output["mode"], "OFFLINE_FAKE")
            self.assertEqual(output["real_external_calls"], 0)
            self.assertEqual(output["status"], "OK")
            self.assertNotIn("sent", output)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["initial_baseline_id"], "100")
        self.assertEqual(state["last_seen_id"], "103")
        self.assertEqual(set(state["processed_ids"]), {"101", "102", "103"})
        self.assertEqual(state["inbox"], {})
        self.assertEqual(set(state["notified_ids"]), {"101:PRE_RESET", "102:POST_RESET"})
        self.assertEqual(len(state["pending_notifications"]), 2)
        for notification in state["pending_notifications"].values():
            self.assertEqual(notification["status"], "SENT")
            self.assertEqual(notification["attempts"], 1)
            self.assertIn(state["processed_ids"][notification["tweet_id"]]["tweet"]["text"], notification["body"])

    def test_dry_run_without_fixture_creates_no_directory_lock_or_state(self):
        state = self.case_root / "absent-parent" / "state.json"
        before = set(self.case_root.iterdir())
        output = self.result(self.run_cli("--dry-run", "--state-file", state, service_guard="ALL"))
        self.assertEqual(output["status"], "DRY_RUN")
        self.assertEqual(output["mode"], "READ_ONLY")
        self.assertEqual(output["real_external_calls"], 0)
        self.assertEqual(output["fake_deliveries"], 0)
        self.assertFalse(output["details"]["side_effects"])
        self.assertFalse(output["details"]["initialized"])
        self.assertEqual(set(self.case_root.iterdir()), before)
        self.assertFalse(state.parent.exists())
        self.assert_no_state_artifacts(state)

    def test_dry_run_preserves_existing_state_backup_bytes_and_mtime(self):
        self.result(self.baseline())
        lock = Path(str(self.state_file) + ".lock")
        lock.unlink(missing_ok=True)
        backup = Path(str(self.state_file) + ".bak")
        self.assertTrue(backup.exists())
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (self.state_file, backup)
        }
        for extra_args in ((), ("--fixture", self.case_root / "must-not-be-opened.json")):
            with self.subTest(extra_args=extra_args):
                output = self.result(self.run_cli(
                    "--dry-run", "--state-file", self.state_file, *extra_args, service_guard="ALL",
                ))
                self.assertEqual(output["status"], "DRY_RUN")
                self.assertTrue(output["details"]["initialized"])
                self.assertEqual(output["fake_deliveries"], 0)
                self.assertFalse(lock.exists())
                for path, expected in before.items():
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)

    def test_recovery_restores_backup_into_hold_without_services(self):
        self.result(self.baseline())
        backup = Path(str(self.state_file) + ".bak")
        backup_bytes = backup.read_bytes()
        damaged = b'{"unfinished":'
        self.state_file.write_bytes(damaged)
        fixture = self.fixture("unused-recovery-input", [], complete=False)
        output = self.result(self.run_cli(
            "--recover-state", "--fixture", fixture, "--state-file", self.state_file,
            service_guard="ALL",
        ), expected_code=2)
        self.assertEqual(output["status"], "RECOVERY_REQUIRED")
        self.assertEqual(output["errors"], ["BACKUP_ROLLBACK_REVIEW_REQUIRED"])
        self.assertEqual((output["ingested"], output["classified"], output["fake_deliveries"]), (0, 0, 0))
        self.assertEqual(output["real_external_calls"], 0)
        recovered = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(recovered["recovery_required"])
        self.assertTrue(recovered["recovered_from_backup"])
        self.assertEqual(backup.read_bytes(), backup_bytes)
        archives = list(self.case_root.glob("state.json.corrupt-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), damaged)
        # The next ordinary offline run also stays held; recovery is not auto approval.
        again = self.result(self.run_cli(
            "--fixture", fixture, "--state-file", self.state_file, service_guard="ALL",
        ), expected_code=2)
        self.assertEqual(again["status"], "RECOVERY_REQUIRED")
        self.assertEqual(again["fake_deliveries"], 0)

    def test_config_rejects_multiple_recipients_crlf_and_bad_values_without_leaks(self):
        fixture = self.fixture("unused-config-input", [])
        cases = [
            ("EMAIL_TO", "privateRecipientSECRET@example.invalid,second@example.invalid"),
            ("EMAIL_TO", "privateRecipientSECRET@example.invalid;second@example.invalid"),
            ("EMAIL_TO", "privateRecipientSECRET@example.invalid\r\nBcc: stolen@example.invalid"),
            ("EMAIL_TO", "privateRecipientSECRET@example.invalid\n"),
            ("FETCH_LIMIT", "secretInvalidLimit20"),
            ("CHECK_INTERVAL_HOURS", "secretInvalidInterval"),
            ("SCHEDULE_TIMEZONE", "secretInvalidTimezone"),
            ("SCHEDULE_TIMES", "secretInvalidSchedule"),
            ("TARGET_USERNAME", "secretInvalidTarget!"),
        ]
        for key, supplied in cases:
            with self.subTest(key=key, case=cases.index((key, supplied))):
                completed = self.run_cli(
                    "--fixture", fixture, "--state-file", self.state_file,
                    env_changes={key: supplied}, service_guard="ALL",
                )
                output = self.result(completed, expected_code=2)
                self.assertEqual(output, {"mode": "OFFLINE", "status": "ERROR", "error": "ConfigError"})
                self.assertNotIn(supplied, completed.stdout + completed.stderr)
                self.assertNotIn("privateRecipientSECRET", completed.stdout + completed.stderr)
                self.assertNotIn("stolen@example.invalid", completed.stdout + completed.stderr)
                self.assert_no_state_artifacts()

    def test_relative_state_path_is_resolved_from_project_root_under_other_cwd(self):
        # The source checkout may be read-only and on a different drive from the
        # writable test root. Exercise the actual CLI in an isolated project copy.
        project = self.case_root / "isolated-project"
        project.mkdir()
        for filename in ("main.py", "config.py"):
            shutil.copy2(PROJECT_ROOT / filename, project / filename)
        copied_monitor = project / "monitor"
        copied_monitor.mkdir()
        for source in (PROJECT_ROOT / "monitor").glob("*.py"):
            shutil.copy2(source, copied_monitor / source.name)
        other_cwd = self.case_root / "other-cwd"
        other_cwd.mkdir()
        relative = Path("runtime") / "relative-state" / "state.json"
        expected = project / relative
        fixture = self.fixture("relative-baseline", [tweet(100)])
        output = self.result(self.run_cli(
            "--fixture", fixture, "--state-file", relative, cwd=other_cwd,
            service_guard="PROCESSING", main_path=project / "main.py",
        ))
        self.assertEqual(output["status"], "OK")
        self.assertTrue(expected.is_file())
        self.assertFalse((other_cwd / relative).exists())
        self.assertEqual(json.loads(expected.read_text(encoding="utf-8"))["initial_baseline_id"], "100")

    def test_parent_credential_sentinels_do_not_reach_the_cli_child(self):
        names = (
            "TWITTER_API_KEY", "APIFY_TOKEN", "DEEPSEEK_API_KEY", "SMTP_PASSWORD",
            "SMTP_USER", "EMAIL_TO", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID", "AZURE_CLIENT_SECRET", "CUSTOM_API_KEY",
            "CUSTOM_TOKEN", "CUSTOM_PASSWORD", "CUSTOM_SECRET",
        )
        sentinels = {name: f"RESET_MONITOR_TEST_SENTINEL_{uuid4().hex}" for name in names}
        parent_values = {**sentinels, "MONITOR_TEST_PUBLIC_CONTROL": "permitted-offline-control"}
        # Install dummy credentials in the real parent environment. The child's
        # independent assertions and fixed receipt prove that filtering occurred.
        with patch.dict(os.environ, parent_values, clear=False):
            self.assertTrue(all(os.environ[name] == value for name, value in sentinels.items()))
            completed = self.run_cli(
                "--dry-run", "--state-file", self.state_file, service_guard="ALL",
                env_changes={"MONITOR_TEST_VERIFY_CREDENTIAL_ISOLATION": "1"},
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "CREDENTIAL_ISOLATION_VERIFIED\n")
        output = json.loads(completed.stdout)
        self.assertEqual(output["status"], "DRY_RUN")
        self.assertEqual(output["mode"], "READ_ONLY")
        self.assertEqual(output["real_external_calls"], 0)
        for value in sentinels.values():
            self.assertNotIn(value, completed.stdout + completed.stderr)
        self.assert_no_state_artifacts()


if __name__ == "__main__":
    unittest.main()
