"""Default offline CLI with an explicit, fail-closed finite validation entry."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from config import Config, ConfigError
from monitor.engine import Monitor
from monitor.fakes import FakeClassifier, FakeFetcher, FakeNotifier
from monitor.notifier import DeliveryResult
from monitor.state import StateError, StateStore

PROJECT_ROOT = Path(__file__).resolve().parent


class SafeArgumentParser(argparse.ArgumentParser):
    """Argument errors must never repeat supplied values or unknown flags."""

    _PUBLIC_ERRORS = frozenset({
        "Live adapters are disabled. Supply --fixture, or use --dry-run.",
        "--dry-run cannot alter state; omit --recover-state and --retry-paused.",
        "Live validation arguments require explicit --live.",
    })

    def error(self, message):
        public = message if message in self._PUBLIC_ERRORS else "CLI_ARGUMENT_ERROR"
        self.exit(2, f"ResetMonitor: error: {public}\n")


def load_fixture(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        fixture = json.load(stream)
    allowed = {"tweets", "classifications", "complete", "notifier_outcomes", "upgrades"}
    if not isinstance(fixture, dict) or set(fixture) - allowed:
        raise ValueError("Invalid offline fixture")
    if not isinstance(fixture.get("tweets"), list) or not isinstance(fixture.get("classifications", {}), dict):
        raise ValueError("Fixture requires tweets and classification mapping")
    if type(fixture.get("complete", True)) is not bool:
        raise ValueError("Invalid completeness marker")
    outcomes = fixture.get("notifier_outcomes", [])
    if not isinstance(outcomes, list) or any(v not in {"SENT", "FAILED", "UNKNOWN"} for v in outcomes):
        raise ValueError("Invalid fake delivery outcomes")
    if not isinstance(fixture.get("upgrades", []), list):
        raise ValueError("Invalid upgrade requests")
    return fixture


def main(argv=None, *, live_dependencies=None, project_root=None):
    parser = SafeArgumentParser(prog="ResetMonitor", description="Codex Reset Monitor: default offline; live validation requires all public gates")
    parser.add_argument("--fixture", type=Path, help="Explicit local JSON input; all service interfaces are fake")
    parser.add_argument("--state-file", help="State path, relative to the project root unless absolute")
    parser.add_argument("--dry-run", action="store_true", help="Read-only inspection; no fetch/classify/send, lock or writes")
    parser.add_argument("--recover-state", action="store_true", help="Restore a valid backup into a persistent review hold")
    parser.add_argument("--retry-paused", action="store_true", help="Explicitly retry confirmed permanent failures; never resets UNKNOWN budgets or backup review holds")
    parser.add_argument("--live", action="store_true", help="Explicit finite validation; public evidence gates must pass before credentials or transports")
    parser.add_argument("--scheduled-validation", action="store_true", help="Finite scheduler candidate: stored credentials and shared CNY 1 guard; does not install a task")
    parser.add_argument("--authorization", type=Path, help="Public finite-batch authorization JSON; contains no credentials")
    parser.add_argument("--live-config", type=Path, help="Public SMTP and Apify settings JSON; contains no credentials")
    parser.add_argument("--credential-source", choices=("env", "prompt", "stored"), help="Explicit project-only env, hidden prompt or Windows stored input, read after public gates")
    parser.add_argument("--init-validation-batch", action="store_true", help="Explicit local ledger initialization only; never reads credentials or contacts services")
    args = parser.parse_args(argv)
    root = PROJECT_ROOT if project_root is None else Path(project_root).resolve()
    if args.live:
        from monitor.live_cli import run_live
        output, exit_code = run_live(args, project_root=root, dependencies=live_dependencies)
        print(json.dumps(output, ensure_ascii=False))
        return exit_code
    if args.authorization or args.live_config or args.credential_source or args.init_validation_batch or args.scheduled_validation:
        parser.error("Live validation arguments require explicit --live.")
    if not args.fixture and not args.dry_run:
        parser.error("Live adapters are disabled. Supply --fixture, or use --dry-run.")
    if args.dry_run and (args.recover_state or args.retry_paused):
        parser.error("--dry-run cannot alter state; omit --recover-state and --retry-paused.")
    try:
        env = dict(os.environ)
        if args.state_file:
            env["STATE_FILE"] = args.state_file
        elif "STATE_FILE" not in env:
            env["STATE_FILE"] = "runtime/offline-state.json"
        config = Config.from_env(env, root)
        fixture = load_fixture(args.fixture) if args.fixture and not args.dry_run else {"tweets": []}
        monitor = Monitor(config, StateStore(config.state_path),
            FakeFetcher(fixture["tweets"], complete=fixture.get("complete", True)),
            FakeClassifier(fixture.get("classifications", {})),
            FakeNotifier(outcomes=[DeliveryResult(v) for v in fixture.get("notifier_outcomes", [])]),
            # No sleeping is needed to simulate backoff in an offline CLI demo.
            sleep=lambda seconds: None)
        result = monitor.run(dry_run=args.dry_run, recover_state=args.recover_state,
                             upgrade_requests=fixture.get("upgrades", []), resume_paused=args.retry_paused)
    except (ConfigError, StateError, ValueError, OSError, TypeError) as exc:
        # Never dump fixture contents, provider exceptions, email addresses or keys.
        print(json.dumps({"mode": "OFFLINE", "status": "ERROR", "error": type(exc).__name__}))
        return 2
    output = asdict(result)
    output["fake_deliveries"] = output.pop("sent")
    output["mode"] = "READ_ONLY" if args.dry_run else "OFFLINE_FAKE"
    output["real_external_calls"] = 0
    print(json.dumps(output, ensure_ascii=False))
    return {"OK": 0, "DRY_RUN": 0, "LOCKED": 0, "DEGRADED": 1, "RECOVERY_REQUIRED": 2}[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
