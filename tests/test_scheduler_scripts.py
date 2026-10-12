"""Offline PowerShell acceptance: scheduler commands are always trapped/mocked."""

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGER = PROJECT_ROOT / "scripts" / "Manage-ResetMonitorTask.ps1"
RUNNER = PROJECT_ROOT / "scripts" / "Run-ResetMonitor.ps1"
TEST_TEMP_ROOT = Path(os.environ.get("RESET_MONITOR_INTEGRATION_TEST_ROOT") or
                      os.environ.get("RESET_MONITOR_TEST_ROOT") or
                      PROJECT_ROOT / "runtime" / "test-tmp").resolve()
WINDOWS_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
POWERSHELL = WINDOWS_ROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

SENSITIVE_NAMES = (
    "TWITTER_API_KEY", "APIFY_TOKEN", "DEEPSEEK_API_KEY", "SMTP_PASSWORD", "SMTP_USER",
    "EMAIL_TO", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID",
    "AZURE_CLIENT_SECRET", "CUSTOM_API_KEY", "CUSTOM_TOKEN", "CUSTOM_PASSWORD", "CUSTOM_SECRET",
    "TARGET_USERNAME", "TARGET_USER_ID", "INCLUDE_REPLIES", "EXCLUDE_RETWEETS", "STATE_FILE",
)


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def ps_parameters(values):
    def literal(value):
        return ('$true' if value else '$false') if isinstance(value, bool) else ps_quote(value)
    return "@{" + "; ".join(f"{key}={literal(value)}" for key, value in values.items()) + "}"


_PS_PREFIX = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
"""

# Even querying/importing ScheduledTasks in Preview is forbidden. The manager
# sees these functions through PowerShell scope lookup, never the real cmdlets.
_SCHEDULER_TRAPS = """
function Import-Module { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function Get-ScheduledTask { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function Register-ScheduledTask { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function Unregister-ScheduledTask { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function New-ScheduledTaskAction { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function New-ScheduledTaskTrigger { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function New-ScheduledTaskSettingsSet { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
function New-ScheduledTaskPrincipal { throw 'SCHEDULER_COMMAND_FORBIDDEN' }
"""

# Install/Uninstall are exercised ONLY with every reachable ScheduledTasks
# command shadowed here, including Import-Module. No actual module is imported.
_SCHEDULER_MOCKS = """
$global:monitorTestCalls = @()
$global:monitorTestExisting = $null
$global:monitorTestRegistered = $null
$global:monitorTestUnregistered = $null
function Import-Module {
    [CmdletBinding()] param([string]$Name)
    if ($Name -ne 'ScheduledTasks') { throw 'UNEXPECTED_MODULE' }
    $global:monitorTestCalls += 'Import-Module'
}
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
    if ($TaskName -ne 'CodexResetMonitor' -or $TaskPath -ne '\\') { throw 'UNEXPECTED_TASK' }
    $global:monitorTestCalls += 'Get-ScheduledTask'
    return $global:monitorTestExisting
}
function New-ScheduledTaskAction {
    [CmdletBinding()] param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    $global:monitorTestCalls += 'New-ScheduledTaskAction'
    return [pscustomobject]@{ execute=$Execute; arguments=$Argument; working_directory=$WorkingDirectory }
}
function New-ScheduledTaskTrigger {
    [CmdletBinding()] param([switch]$Daily, [int]$DaysInterval, [DateTime]$At)
    $global:monitorTestCalls += 'New-ScheduledTaskTrigger'
    return [pscustomobject]@{ daily=[bool]$Daily; days_interval=$DaysInterval; StartBoundary=$At.ToString('s') }
}
function New-ScheduledTaskSettingsSet {
    [CmdletBinding()] param([string]$MultipleInstances, [switch]$StartWhenAvailable, [TimeSpan]$ExecutionTimeLimit)
    $global:monitorTestCalls += 'New-ScheduledTaskSettingsSet'
    return [pscustomobject]@{ instances=$MultipleInstances; start_when_available=[bool]$StartWhenAvailable; limit_minutes=$ExecutionTimeLimit.TotalMinutes }
}
function New-ScheduledTaskPrincipal {
    [CmdletBinding()] param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    $global:monitorTestCalls += 'New-ScheduledTaskPrincipal'
    # Only record presence: do not disclose the machine account name.
    return [pscustomobject]@{ user_id_set=[bool]$UserId; logon_type=$LogonType; run_level=$RunLevel }
}
function Register-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, $Action, $Trigger, $Settings, $Principal, [string]$Description)
    $global:monitorTestCalls += 'Register-ScheduledTask'
    $global:monitorTestRegistered = [ordered]@{ task_name=$TaskName; task_path=$TaskPath; action=$Action; trigger=$Trigger; settings=$Settings; principal=$Principal; description=$Description }
    return [pscustomobject]@{ registered_mock=$true }
}
function Unregister-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, [switch]$Confirm)
    $global:monitorTestCalls += 'Unregister-ScheduledTask'
    $global:monitorTestUnregistered = [ordered]@{ task_name=$TaskName; task_path=$TaskPath; confirm=[bool]$Confirm }
}
"""

_SITE_GUARD = """
import json
import os
from pathlib import Path
import smtplib
import socket
import sys

absent = {sensitive_names!r}
if (any(name in os.environ for name in absent)
        or any('RESET_MONITOR_TEST_SENTINEL_' in value for value in os.environ.values())
        or 'CHECK_INTERVAL_HOURS' in os.environ
        or os.environ.get('SCHEDULE_TIMEZONE') != 'Asia/Shanghai'
        or os.environ.get('SCHEDULE_TIMES') != '08:00,13:00,20:00'
        or os.environ.get('FETCH_LIMIT') != '3'
        or os.environ.get('MONITOR_TEST_PUBLIC_CONTROL') != 'permitted-offline-control'):
    os._exit(93)

def forbidden_network(*args, **kwargs):
    Path(os.environ['MONITOR_TEST_NETWORK_MARKER']).write_text('FORBIDDEN', encoding='utf-8')
    os._exit(94)

socket.socket = forbidden_network
socket.create_connection = forbidden_network
smtplib.SMTP = forbidden_network
smtplib.SMTP_SSL = forbidden_network
from types import ModuleType
vault_guard = ModuleType('monitor.credential_store')
vault_guard.load_credentials = forbidden_network
sys.modules['monitor.credential_store'] = vault_guard
Path(os.environ['MONITOR_TEST_RECEIPT']).write_text(json.dumps({{
    'credential_isolation': True, 'cwd': os.getcwd(), 'main': sys.argv[0],
    'fetch_limit': os.environ['FETCH_LIMIT'], 'schedule_timezone': os.environ['SCHEDULE_TIMEZONE'],
    'schedule_times': os.environ['SCHEDULE_TIMES'],
}}), encoding='utf-8')
"""


class SchedulerScriptTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.case_root = TEST_TEMP_ROOT / ("scheduler-" + uuid4().hex)
        self.case_root.mkdir()
        self.addCleanup(self.remove_case_directory)
        self.state = self.case_root / "state with spaces" / "状态.json"
        self.log = self.case_root / "log with spaces" / "执行.log"
        self.fixture = self.case_root / "fixture with spaces.json"
        self.fixture.write_text(json.dumps({"tweets": []}), encoding="utf-8")
        self.receipt = self.case_root / "child-receipt.json"
        self.network_marker = self.case_root / "forbidden-network.txt"
        guard = self.case_root / "python guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(_SITE_GUARD.format(sensitive_names=SENSITIVE_NAMES), encoding="utf-8")
        # Build the child environment from an allowlist; never copy account keys.
        self.env = {
            "SystemRoot": str(WINDOWS_ROOT), "WINDIR": str(WINDOWS_ROOT),
            "SystemDrive": WINDOWS_ROOT.drive,
            "ComSpec": str(WINDOWS_ROOT / "System32" / "cmd.exe"),
            "PATH": str(WINDOWS_ROOT / "System32"), "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": str(self.case_root), "TMP": str(self.case_root),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(guard), "MONITOR_TEST_PUBLIC_CONTROL": "permitted-offline-control",
            "MONITOR_TEST_RECEIPT": str(self.receipt), "MONITOR_TEST_NETWORK_MARKER": str(self.network_marker),
            "CHECK_INTERVAL_HOURS": "invalid-parent-value", "FETCH_LIMIT": "invalid-parent-value",
            "SCHEDULE_TIMEZONE": "invalid-parent-value", "SCHEDULE_TIMES": "invalid-parent-value",
        }
        self.sentinels = {name: "RESET_MONITOR_TEST_SENTINEL_" + uuid4().hex for name in SENSITIVE_NAMES}
        self.env.update(self.sentinels)

    def remove_case_directory(self):
        base, target = TEST_TEMP_ROOT.resolve(), self.case_root.resolve()
        if target == base or not target.is_relative_to(base):
            raise AssertionError("Refusing cleanup outside the test directory")
        if target.exists():
            shutil.rmtree(target)

    def run_powershell(self, code):
        encoded = base64.b64encode((_PS_PREFIX + code).encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            cwd=self.case_root, env=dict(self.env), capture_output=True, text=True,
            encoding="utf-8", timeout=30, check=False,
        )
        combined = result.stdout + result.stderr
        self.assertNotIn("SCHEDULER_COMMAND_FORBIDDEN", combined)
        self.assertFalse(self.network_marker.exists(), "Runner attempted a forbidden network connection")
        for sentinel in self.sentinels.values():
            self.assertNotIn(sentinel, combined)
        return result

    def parameters(self, **changes):
        values = dict(ProjectPath=PROJECT_ROOT, PythonPath=sys.executable, StatePath=self.state,
                      LogPath=self.log, FixturePath=self.fixture)
        values.update(changes)
        return values

    def preview(self, values=None):
        params = ps_parameters(values or {})
        return self.run_powershell(_SCHEDULER_TRAPS + f"\n$parameters = {params}\n& {ps_quote(MANAGER)} @parameters\n")

    def runner(self, validation_guard=False, **changes):
        params = ps_parameters(self.parameters(**changes))
        guard = """
function New-Object {
    [Console]::Out.Write('VALIDATION_BYPASSED')
    throw 'Child process creation forbidden in validation tests'
}
""" if validation_guard else ""
        # -EncodedCommand otherwise normalizes an invoked script's nonzero exit
        # to 1; explicitly preserve the runner's native -File exit contract.
        return self.run_powershell(guard + f"$parameters = {params}\n& {ps_quote(RUNNER)} @parameters\nexit $LASTEXITCODE\n")

    def json_result(self, result, expected_code=0):
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def assert_no_runtime_outputs(self):
        self.assertFalse(self.state.parent.exists())
        self.assertFalse(self.log.parent.exists())
        self.assertFalse(self.receipt.exists())

    def tree_snapshot(self):
        return {str(path.relative_to(self.case_root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.case_root.rglob("*") if path.is_file()}

    def mock_manager(self, action, existing="none", **changes):
        parameters = ps_parameters(self.parameters(**changes))
        setup = ""
        if existing != "none":
            description = "foreign task" if existing == "foreign" else "ResetMonitor managed task v2; offline only; fixed Shanghai schedule"
            arguments = ("unrelated-runner" if existing == "wrong_runner" else
                         ('-NoProfile -NonInteractive -File "C:\\other.ps1" -Unused "' + str(RUNNER) + '"')
                         if existing == "spoofed_runner" else "$preview.arguments")
            if arguments != "$preview.arguments":
                arguments = ps_quote(arguments)
            setup = f"""
$global:monitorTestExisting = [pscustomobject]@{{ Description={ps_quote(description)}; Actions=@(
    [pscustomobject]@{{ Execute=$preview.executable; Arguments={arguments}; WorkingDirectory=$preview.working_directory }}) }}
"""
        code = _SCHEDULER_MOCKS + f"""
$parameters = {parameters}
$previewText = & {ps_quote(MANAGER)} @parameters
$preview = ($previewText -join "`n") | ConvertFrom-Json
{setup}
try {{
    $parameters['Action'] = {ps_quote(action)}
    $text = & {ps_quote(MANAGER)} @parameters
    $plan = ($text -join "`n") | ConvertFrom-Json
    [ordered]@{{ plan=$plan; calls=@($global:monitorTestCalls); registered=$global:monitorTestRegistered; unregistered=$global:monitorTestUnregistered; error=$null }} | ConvertTo-Json -Depth 8
}} catch {{
    [ordered]@{{ plan=$null; calls=@($global:monitorTestCalls); registered=$global:monitorTestRegistered; unregistered=$global:monitorTestUnregistered; error=$_.Exception.Message }} | ConvertTo-Json -Depth 8
}}
"""
        return self.json_result(self.run_powershell(code))

    def test_default_Preview_is_read_only_without_import_or_query(self):
        before = self.tree_snapshot()
        plan = self.json_result(self.preview())
        self.assertEqual(plan["action"], "Preview")
        self.assertEqual(plan["mode"], "Offline")
        self.assertFalse(plan["mutation"])
        self.assertFalse(plan["can_install"])
        self.assertFalse(plan["credentials_in_task"])
        self.assertEqual(plan["state_path"], str(PROJECT_ROOT / "runtime" / "scheduled-offline-state.json"))
        self.assertEqual(plan["log_path"], str(PROJECT_ROOT / "runtime" / "scheduled-offline.log"))
        self.assertEqual(before, self.tree_snapshot())
        self.assert_no_runtime_outputs()

    def test_configured_Preview_has_three_fixed_Shanghai_slots_and_safe_principal(self):
        before = self.tree_snapshot()
        plan = self.json_result(self.preview(self.parameters()))
        self.assertEqual(plan["task_name"], "CodexResetMonitor")
        self.assertEqual(plan["working_directory"], str(PROJECT_ROOT))
        self.assertEqual(plan["state_path"], str(self.state))
        self.assertEqual(plan["log_path"], str(self.log))
        self.assertEqual(plan["schedule_timezone"], "Asia/Shanghai")
        self.assertEqual(plan["schedule_times"], ["08:00", "13:00", "20:00"])
        self.assertEqual((plan["rounds_per_day"], plan["fetch_limit"], plan["max_items_per_day"]), (3, 3, 9))
        self.assertEqual(plan["missed_runs"], "Skip")
        self.assertNotIn("interval_hours", plan)
        self.assertEqual(len(plan["start_boundaries"]), 3)
        instants = [datetime.fromisoformat(value) for value in plan["start_boundaries"]]
        self.assertTrue(all(value.utcoffset() == timedelta(hours=8) for value in instants))
        self.assertEqual([value.astimezone(timezone.utc).hour for value in instants], [0, 5, 12])
        self.assertEqual(plan["multiple_instances"], "IgnoreNew")
        self.assertFalse(plan["start_when_available"])
        self.assertEqual(plan["run_level"], "Limited")
        self.assertEqual(plan["logon_type"], "Interactive")
        self.assertNotIn("repetition_days", plan)
        self.assertTrue(plan["can_install"])
        self.assertEqual(plan["blocked_reasons"], [])
        self.assertFalse(plan["mutation"])
        for path in (RUNNER, PROJECT_ROOT, self.state, self.log, self.fixture, Path(sys.executable)):
            self.assertIn('"' + str(path) + '"', plan["arguments"])
        self.assertEqual(before, self.tree_snapshot())
        self.assert_no_runtime_outputs()

    def test_Live_Preview_blocked_and_mocked_Install_never_registers(self):
        plan = self.json_result(self.preview(self.parameters(Mode="Live")))
        self.assertFalse(plan["can_install"])
        self.assertTrue(any("Live" in reason for reason in plan["blocked_reasons"]))
        result = self.mock_manager("Install", Mode="Live")
        self.assertIn("Live", result["error"])
        self.assertNotIn("Register-ScheduledTask", result["calls"])
        self.assertIsNone(result["registered"])
        self.assert_no_runtime_outputs()

    def test_boundaries_keep_China_date_and_UTC_instants_across_offsets_and_seasons(self):
        examples = ["2026-01-15T15:59:59+00:00", "2026-01-15T16:00:00+00:00",
                    "2026-07-15T16:00:00+00:00", "2026-07-16T06:00:00+14:00",
                    "2026-07-15T06:00:00-10:00"]
        literals = ",".join(ps_quote(value) for value in examples)
        code = _SCHEDULER_TRAPS + f"""
$parameters = {ps_parameters(self.parameters())}
. {ps_quote(MANAGER)} @parameters | Out-Null
$rows = @(foreach ($time in @({literals})) {{
    [ordered]@{{ input=$time; boundaries=@(Get-ResetMonitorStartBoundaries ([DateTimeOffset]::Parse($time))) }}
}})
ConvertTo-Json -InputObject $rows -Depth 5
"""
        rows = self.json_result(self.run_powershell(code))
        self.assertEqual(len(rows), len(examples))
        for row, original in zip(rows, examples):
            expected_date = datetime.fromisoformat(original).astimezone(timezone(timedelta(hours=8))).date()
            instants = [datetime.fromisoformat(value) for value in row["boundaries"]]
            self.assertEqual(len(instants), 3)
            self.assertTrue(all(value.date() == expected_date and value.utcoffset() == timedelta(hours=8)
                                for value in instants))
            self.assertEqual([value.astimezone(timezone.utc).hour for value in instants], [0, 5, 12])
        self.assertEqual(rows[2]["boundaries"], rows[3]["boundaries"])
        self.assertEqual(rows[2]["boundaries"], rows[4]["boundaries"])
        self.assert_no_runtime_outputs()

    def test_legacy_repetition_interval_parameter_is_rejected(self):
        result = self.preview(self.parameters(IntervalHours=2))
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_runtime_outputs()

    def test_mocked_Install_records_only_fixed_own_task_without_real_module(self):
        before = self.tree_snapshot()
        result = self.mock_manager("Install")
        self.assertIsNone(result["error"])
        self.assertTrue(result["plan"]["mutation"])
        registered = result["registered"]
        self.assertEqual(registered["task_name"], "CodexResetMonitor")
        self.assertEqual(registered["task_path"], "\\")
        self.assertEqual(registered["description"], "ResetMonitor managed task v2; offline only; fixed Shanghai schedule")
        self.assertEqual(registered["action"]["working_directory"], str(PROJECT_ROOT))
        self.assertEqual(len(registered["trigger"]), 3)
        for trigger, boundary in zip(registered["trigger"], result["plan"]["start_boundaries"]):
            self.assertEqual(trigger, {"daily": True, "days_interval": 1, "StartBoundary": boundary})
            self.assertTrue(boundary.endswith("+08:00"))
        self.assertEqual(registered["settings"], {"instances": "IgnoreNew", "start_when_available": False, "limit_minutes": 10})
        self.assertEqual(result["calls"].count("New-ScheduledTaskTrigger"), 3)
        self.assertEqual(registered["principal"], {"user_id_set": True, "logon_type": "Interactive", "run_level": "Limited"})
        self.assertEqual(result["calls"].count("Register-ScheduledTask"), 1)
        self.assertIsNone(result["unregistered"])
        self.assertEqual(before, self.tree_snapshot())

    def test_mocked_Install_does_not_overwrite_existing_task(self):
        result = self.mock_manager("Install", existing="own")
        self.assertIn("already exists", result["error"])
        self.assertNotIn("Register-ScheduledTask", result["calls"])
        self.assertIsNone(result["registered"])

    def test_mocked_Uninstall_refuses_foreign_or_wrong_runner_and_preserves_files(self):
        for existing in ("foreign", "wrong_runner", "spoofed_runner"):
            with self.subTest(existing=existing):
                before = self.tree_snapshot()
                result = self.mock_manager("Uninstall", existing=existing)
                self.assertIn("Refusing", result["error"])
                self.assertNotIn("Unregister-ScheduledTask", result["calls"])
                self.assertIsNone(result["unregistered"])
                self.assertEqual(before, self.tree_snapshot())

    def test_mocked_Uninstall_targets_only_owned_task_and_keeps_state_logs_fixture(self):
        self.state.parent.mkdir()
        self.log.parent.mkdir()
        self.state.write_bytes(b"synthetic state bytes")
        self.log.write_bytes(b"synthetic log bytes")
        before = self.tree_snapshot()
        result = self.mock_manager("Uninstall", existing="own")
        self.assertIsNone(result["error"])
        self.assertTrue(result["plan"]["mutation"])
        self.assertEqual(result["unregistered"], {"task_name": "CodexResetMonitor", "task_path": "\\", "confirm": False})
        self.assertEqual(result["calls"].count("Unregister-ScheduledTask"), 1)
        self.assertEqual(before, self.tree_snapshot())
        absent = self.mock_manager("Uninstall")
        self.assertIsNone(absent["error"])
        self.assertFalse(absent["plan"]["mutation"])
        self.assertIsNone(absent["unregistered"])

    def test_nonabsolute_and_collision_paths_are_rejected(self):
        for changes in ({"ProjectPath": "relative-project"}, {"StatePath": "C:relative-state.json"},
                        {"LogPath": r"\root-relative-log.txt"}, {"FixturePath": r"\\server\share\fixture.json"},
                        {"StatePath": self.log}, {"StatePath": self.fixture}):
            with self.subTest(fields=tuple(changes)):
                result = self.preview(self.parameters(**changes))
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_runtime_outputs()

    def test_runner_real_D_entry_from_C_cwd_creates_only_C_state_log_and_filters_parent(self):
        self.fixture.write_text(json.dumps({"tweets": [{
            "id": "100", "author": "thsottiaux", "text": "synthetic baseline",
            "url": "https://x.com/thsottiaux/status/100", "created_at": "2026-10-10T00:00:00Z", "context": None,
        }]}), encoding="utf-8")
        output = self.json_result(self.runner())
        self.assertEqual((output["status"], output["mode"], output["fake_deliveries"], output["real_external_calls"]),
                         ("OK", "OFFLINE_FAKE", 0, 0))
        self.assertTrue(self.state.is_file())
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["initial_baseline_id"], "100")
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertTrue(receipt["credential_isolation"])
        self.assertEqual(Path(receipt["cwd"]).resolve(), PROJECT_ROOT)
        self.assertEqual(Path(receipt["main"]).resolve(), PROJECT_ROOT / "main.py")
        self.assertEqual((receipt["fetch_limit"], receipt["schedule_timezone"], receipt["schedule_times"]),
                         ("3", "Asia/Shanghai", "08:00,13:00,20:00"))
        log = self.log.read_text(encoding="utf-8")
        self.assertIn(" exit=0\n", log)
        self.assertIn('"real_external_calls": 0', log)
        for sentinel in self.sentinels.values():
            self.assertNotIn(sentinel, log)
            self.assertNotIn(sentinel, self.state.read_text(encoding="utf-8"))
        self.assertFalse((self.case_root / "runtime").exists())

    def test_runner_preserves_CLI_degraded_and_input_error_exit_codes(self):
        self.fixture.write_text(json.dumps({"tweets": [], "complete": False}), encoding="utf-8")
        degraded = self.json_result(self.runner(), expected_code=1)
        self.assertEqual(degraded["status"], "DEGRADED")
        self.assertIn(" exit=1\n", self.log.read_text(encoding="utf-8"))
        self.fixture.write_text('{"bad_json":', encoding="utf-8")
        invalid = self.json_result(self.runner(), expected_code=2)
        self.assertEqual(invalid["status"], "ERROR")
        self.assertIn(" exit=2\n", self.log.read_text(encoding="utf-8"))

    def test_runner_preserves_arbitrary_child_exit_and_logs_both_streams(self):
        project = self.case_root / "synthetic project"
        project.mkdir()
        (project / "main.py").write_text("import sys\nprint('PROBE_STDOUT')\nprint('PROBE_STDERR', file=sys.stderr)\nraise SystemExit(17)\n", encoding="utf-8")
        result = self.runner(ProjectPath=project)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "PROBE_STDOUT\n")
        self.assertEqual(result.stderr, "")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn(" exit=17\n", log)
        self.assertIn("PROBE_STDOUT\n", log)
        self.assertIn("PROBE_STDERR\n", log)
        self.assertFalse(self.state.exists())
        self.assertTrue(self.receipt.exists())

    def test_runner_Live_and_missing_input_fail_before_child_or_outputs(self):
        for changes in ({"Mode": "Live"}, {"FixturePath": self.case_root / "missing.json"},
                        {"StatePath": "C:relative-state.json"}, {"LogPath": r"\root-relative-log.txt"},
                        {"StatePath": self.fixture}):
            with self.subTest(fields=tuple(changes)):
                # Trap process-info construction even if validation regresses;
                # no root-relative path can ever become a write attempt.
                result = self.runner(validation_guard=True, **changes)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "LIVE_RUNNER_ERROR\n" if changes.get('Mode') == 'Live' else "OFFLINE_RUNNER_ERROR\n")
                self.assert_no_runtime_outputs()

    def live_parameters(self, **changes):
        from tests.test_live_cli import synthetic_live_inputs
        authorization = self.case_root / 'public authorization.json'
        settings = self.case_root / 'public configuration.json'
        raw, public, *_ = synthetic_live_inputs(self.case_root)
        raw['state_path'] = str(self.state)
        current = datetime.now(timezone.utc)
        raw['approved_at'] = current.isoformat()
        raw['expires_at'] = (current + timedelta(hours=1)).isoformat()
        authorization.write_text(json.dumps(raw), encoding='utf-8')
        settings.write_text(json.dumps(public), encoding='utf-8')
        values = self.parameters(Mode='Live', FixturePath='', EnableLiveValidation=True,
                                 AuthorizationPath=authorization, LiveConfigPath=settings)
        values.update(changes)
        return values

    def fake_live_project(self):
        project = self.case_root / 'synthetic Live project with spaces'
        project.mkdir()
        (project / 'main.py').write_text('''import json, sys
from pathlib import Path
path = Path(__file__).parent / 'observed-public-argv.json'
rows = json.loads(path.read_text()) if path.exists() else []
rows.append(sys.argv[1:])
path.write_text(json.dumps(rows), encoding='utf-8')
preflight = '--dry-run' in sys.argv
value = dict(mode='LIVE_VALIDATION', status='PUBLIC_PREFLIGHT_OK' if preflight else 'DEGRADED',
             arbitrary_provider_text='SYNTHETIC_PROVIDER_PRIVATE_DATA')
if not preflight:
    value.update(ingested=3, classified=3, sent=2)
print(json.dumps(value))
print('SYNTHETIC_PROVIDER_PRIVATE_STDERR', file=sys.stderr)
raise SystemExit(0 if preflight else 1)
''', encoding='utf-8')
        return project

    def test_Live_candidate_preview_prepares_fixed_arguments_but_never_installs(self):
        values = self.live_parameters()
        before = self.tree_snapshot()
        plan = self.json_result(self.preview(values))
        self.assertEqual(plan['mode'], 'Live')
        self.assertFalse(plan['can_install'])
        self.assertTrue(plan['public_preflight_required'])
        self.assertTrue(plan['finite_validation_only'])
        self.assertEqual(plan['credential_source'], 'stored')
        self.assertFalse(plan['automatic_ledger_initialization'])
        self.assertEqual(plan['schedule_times'], ['08:00', '13:00', '20:00'])
        self.assertEqual(plan['multiple_instances'], 'IgnoreNew')
        for option in ('-Mode Live', '-EnableLiveValidation', '-AuthorizationPath', '-LiveConfigPath'):
            self.assertIn(option, plan['arguments'])
        self.assertEqual(before, self.tree_snapshot())
        result = self.mock_manager('Install', **{key: value for key, value in values.items()
                                                if key not in ('ProjectPath','PythonPath','StatePath','LogPath')})
        self.assertIn('Live task installation is disabled', result['error'])
        self.assertEqual(result['calls'], [])
        self.assert_no_runtime_outputs()

    def test_real_Live_runner_from_isolated_cwd_refuses_before_vault_network_or_output_files(self):
        values = self.live_parameters()
        result = self.runner(**values)
        public = self.json_result(result, expected_code=2)
        self.assertEqual(public, dict(mode='LIVE_VALIDATION', status='REFUSED',
                                     issue_codes=['LIVE_RUNNER_PUBLIC_PREFLIGHT_REFUSED']))
        self.assertTrue(self.receipt.exists())  # The actual D: main.py was invoked.
        receipt = json.loads(self.receipt.read_text(encoding='utf-8'))
        self.assertEqual(Path(receipt['main']).resolve(), PROJECT_ROOT / 'main.py')
        self.assertFalse(self.state.parent.exists())
        self.assertFalse(self.log.parent.exists())

    def test_Live_preflight_only_passes_public_arguments_without_stored_source_or_logs(self):
        project = self.fake_live_project()
        result = self.runner(**self.live_parameters(ProjectPath=project, PreflightOnly=True))
        self.assertEqual(self.json_result(result), dict(mode='LIVE_VALIDATION', status='PUBLIC_PREFLIGHT_OK'))
        rows = json.loads((project / 'observed-public-argv.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 1)
        self.assertIn('--live', rows[0])
        self.assertIn('--scheduled-validation', rows[0])
        self.assertIn('--dry-run', rows[0])
        self.assertNotIn('--credential-source', rows[0])
        self.assertNotIn('--init-validation-batch', rows[0])
        self.assertFalse(self.log.parent.exists())
        self.assertFalse(self.state.parent.exists())

    def test_Live_runner_checks_public_gate_then_fixed_stored_source_and_sanitizes_logs(self):
        project = self.fake_live_project()
        values = self.live_parameters(ProjectPath=project)
        result = self.runner(**values)
        public = self.json_result(result, expected_code=1)
        self.assertEqual(public, dict(mode='LIVE_VALIDATION', status='DEGRADED', ingested=3, classified=3, sent=2))
        rows = json.loads((project / 'observed-public-argv.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 2)
        self.assertIn('--dry-run', rows[0])
        self.assertNotIn('--credential-source', rows[0])
        self.assertNotIn('--dry-run', rows[1])
        self.assertEqual(rows[1][-2:], ['--credential-source', 'stored'])
        for arguments in rows:
            self.assertIn('--scheduled-validation', arguments)
            self.assertEqual(arguments[arguments.index('--authorization') + 1], str(values['AuthorizationPath']))
            self.assertEqual(arguments[arguments.index('--live-config') + 1], str(values['LiveConfigPath']))
            self.assertNotIn('--init-validation-batch', arguments)
        log = self.log.read_text(encoding='utf-8')
        self.assertIn(' exit=1\n', log)
        self.assertNotIn('SYNTHETIC_PROVIDER_PRIVATE', log + result.stdout + result.stderr)
        self.assertNotIn('arbitrary_provider_text', log)
        self.assertFalse(self.state.parent.exists())


if __name__ == "__main__":
    unittest.main()
