"""Explicit finite validation CLI with public gates before credential access.

There is no .env loader, credential argument, proof-installation JSON field or
automatic ledger initialization. The ordinary CLI remains OFFLINE. Production
evidence registries are empty until independently reviewed source, pricing and
complete-tokenizer contracts can be installed in code.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import time

from config import Config
from .batch import BatchAuthorization, BatchLedger, NoExternalCall
from .credentials import CredentialInputError, ProjectCredentials, read_project_credentials
from .http_transport import StdlibHTTPTransport
from .smtp_transport import stdlib_smtp_factory
from .state import StateError, StateStore
from .manual_model_budget import ManualModelBudget, ModelBudgetError, LIMIT_CNY


_SETTINGS_FIELDS = {"schema_version", "smtp_host", "smtp_port", "smtp_security", "apify_policy"}
_APIFY_FIELDS = {"available_balance_usd", "max_total_charge_usd", "minimum_charge_cap_usd", "per_result_usd"}


@dataclass(frozen=True)
class LiveDependencies:
    """Python-only injection seam; never populated by a public JSON document."""

    clock: object = None
    credential_reader: object = read_project_credentials
    apify_transport_factory: object = StdlibHTTPTransport
    deepseek_transport_factory: object = StdlibHTTPTransport
    smtp_factory: object = stdlib_smtp_factory
    source_catalog: object = None
    token_proofs: object = None
    apify_cost_proofs: object = None
    sleep: object = time.sleep
    monotonic: object = time.monotonic
    trusted_ledger_path: object = None
    model_budget_factory: object = ManualModelBudget


def _output(status, issues=(), **fields):
    # Only fixed local codes and bounded operational counts leave this module.
    return {"mode": "LIVE_VALIDATION", "status": status,
            "issue_codes": list(dict.fromkeys(issues)), **fields}


def _load_public(path, *, max_bytes, code):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    def invalid_constant(_value):
        raise ValueError
    try:
        if path is None:
            raise ValueError
        with Path(path).open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if not 0 < len(data) <= max_bytes:
            raise ValueError
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid_constant)
        if type(value) is not dict:
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise NoExternalCall(code) from None


def _settings(path):
    value = _load_public(path, max_bytes=16_384, code="LIVE_SETTINGS_READ_INVALID")
    try:
        if set(value) != _SETTINGS_FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if (not isinstance(value["smtp_host"], str) or not 0 < len(value["smtp_host"]) <= 253
                or not value["smtp_host"].isascii()
                or any(ord(character) < 33 or ord(character) == 127 for character in value["smtp_host"])
                or type(value["smtp_port"]) is not int or not 1 <= value["smtp_port"] <= 65535
                or value["smtp_security"] not in ("SSL", "STARTTLS")):
            raise ValueError
        policy = value["apify_policy"]
        if type(policy) is not dict or set(policy) != _APIFY_FIELDS:
            raise ValueError
        for name, text in policy.items():
            if not isinstance(text, str) or not 0 < len(text) <= 64:
                raise ValueError
            amount = Decimal(text)
            if not amount.is_finite() or amount < 0 or name != "available_balance_usd" and amount == 0:
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise NoExternalCall("LIVE_SETTINGS_SCHEMA_INVALID") from None


def _artifacts(path):
    path = Path(path).resolve()
    return tuple(path.with_name(path.name + suffix) for suffix in ("", ".bak", ".lock", ".guard"))


def _trusted_ledger_path(deps):
    # LOCALAPPDATA is a public OS location, not a credential. Tests always
    # inject a temporary path; no authorization/CLI option can select this root.
    path = deps.trusted_ledger_path
    if path is None:
        directory = os.environ.get("LOCALAPPDATA")
        if not isinstance(directory, str) or not directory:
            raise NoExternalCall("LIVE_TRUSTED_LEDGER_LOCATION_REQUIRED")
        base = Path(directory)
        if not base.is_absolute():
            raise NoExternalCall("LIVE_TRUSTED_LEDGER_LOCATION_REQUIRED")
        path = base / "CodexResetMonitor" / "validation-ledger.json"
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise NoExternalCall("LIVE_TRUSTED_LEDGER_LOCATION_REQUIRED")
    return path.resolve()


def _overlap(left, right):
    for candidate in left:
        for existing in right:
            if candidate == existing:
                return True
            try:
                if candidate.exists() and existing.exists() and candidate.samefile(existing):
                    return True
            except OSError:
                raise NoExternalCall("LIVE_STATE_ALIAS_CHECK_FAILED") from None
    return False


def _inspect_state(config, project_root, ledger_path, input_paths):
    """Resolve filesystem aliases and inspect origin without locks or writes."""
    root = Path(project_root).resolve()
    state = config.state_path.resolve()
    protected = [root / "state.json", root / "runtime" / "offline-state.json",
                 root / "runtime" / "scheduled-offline-state.json", root / "runtime" / "demo-state.json",
                 root / "runtime" / "demo.json", root / "runtime" / "scheduled-state.json", ledger_path]
    protected.extend(Path(path).resolve() for path in input_paths if path is not None)
    if state.suffix.lower() != ".json" or state.is_dir():
        raise NoExternalCall("LIVE_STATE_PATH_INVALID")
    if any(_overlap(_artifacts(state), _artifacts(path)) for path in protected):
        raise NoExternalCall("LIVE_STATE_PATH_CONFLICT")
    for directory in (root / "examples", root / "monitor", root / "tests", root / "scripts", root / ".git", root / ".codex"):
        if state.is_relative_to(directory.resolve()):
            raise NoExternalCall("LIVE_STATE_PATH_CONFLICT")
    snapshot = StateStore(state).load(recover=False)
    if snapshot is not None:
        if snapshot.get("execution_mode") != "LIVE":
            raise NoExternalCall("LIVE_STATE_ORIGIN_UNSAFE")
        if snapshot.get("target") != config.target_username or snapshot.get("target_user_id") != config.target_user_id:
            raise NoExternalCall("LIVE_STATE_IDENTITY_MISMATCH")
        if snapshot.get("recovery_required") or snapshot.get("recovered_from_backup"):
            raise NoExternalCall("LIVE_STATE_RECOVERY_REVIEW_REQUIRED")
    return snapshot


def run_live(args, *, project_root, dependencies=None):
    """Return public output and exit status; never print provider exceptions."""
    deps = LiveDependencies() if dependencies is None else dependencies
    if not isinstance(deps, LiveDependencies):
        return _output("REFUSED", ("LIVE_DEPENDENCIES_INVALID",)), 2
    required = (deps.credential_reader, deps.apify_transport_factory, deps.deepseek_transport_factory,
                deps.smtp_factory, deps.sleep, deps.monotonic, deps.model_budget_factory)
    if any(not callable(value) for value in required) or deps.clock is not None and not callable(deps.clock):
        return _output("REFUSED", ("LIVE_DEPENDENCIES_INVALID",)), 2
    # These checks also protect direct Python invocation outside argparse.
    if any(getattr(args, name, None) for name in ("fixture", "state_file", "recover_state", "retry_paused")):
        return _output("REFUSED", ("LIVE_OFFLINE_ARGUMENT_CONFLICT",)), 2
    if getattr(args, "dry_run", False) and getattr(args, "init_validation_batch", False):
        return _output("REFUSED", ("LIVE_READ_ONLY_ARGUMENT_CONFLICT",)), 2
    scheduled = getattr(args, 'scheduled_validation', False)
    if scheduled and getattr(args, 'init_validation_batch', False):
        return _output('REFUSED', ('LIVE_SCHEDULE_INITIALIZATION_FORBIDDEN',)), 2
    if scheduled and not getattr(args, 'dry_run', False) and getattr(args, 'credential_source', None) != 'stored':
        return _output('REFUSED', ('LIVE_SCHEDULE_STORED_CREDENTIALS_REQUIRED',)), 2
    clock = deps.clock or (lambda: datetime.now(timezone.utc))
    try:
        now = clock()
        authorization_path = getattr(args, "authorization", None)
        settings_path = getattr(args, "live_config", None)
        missing = []
        if authorization_path is None:
            missing.append("LIVE_AUTHORIZATION_FILE_REQUIRED")
        if settings_path is None:
            missing.append("LIVE_SETTINGS_FILE_REQUIRED")
        if missing:
            return _output("REFUSED", missing), 2
        raw = _load_public(authorization_path, max_bytes=65_536, code="LIVE_AUTHORIZATION_READ_INVALID")
        authorization = BatchAuthorization.from_dict(raw, now=now)
        settings = _settings(settings_path)
        config = Config(state_path=authorization.state_path, target_username=authorization.target_username,
                        target_user_id=authorization.target_user_id, email_to=authorization.recipient,
                        execution_mode="LIVE")
        root = Path(project_root).resolve()
        ledger_path = _trusted_ledger_path(deps)
        if authorization.ledger_path.resolve() != ledger_path.resolve():
            raise NoExternalCall("LIVE_LEDGER_PATH_BINDING_MISMATCH")
        if any(_overlap(_artifacts(ledger_path), _artifacts(path)) for path in (authorization_path, settings_path)):
            raise NoExternalCall("LIVE_LEDGER_PUBLIC_PATH_CONFLICT")
        _inspect_state(config, root, ledger_path, (authorization_path, settings_path))
        from .validation import validation_preflight, assemble_validation_monitor
        issues = validation_preflight(config, settings, authorization, now=now,
                                      source_catalog=deps.source_catalog, token_proofs=deps.token_proofs,
                                      apify_cost_proofs=deps.apify_cost_proofs)
        if issues:
            return _output("REFUSED", issues), 2
        if (authorization.deepseek['currency'] != 'CNY'
                or Decimal(authorization.deepseek['amount']) > LIMIT_CNY):
            raise NoExternalCall('LIVE_CNY_BUDGET_REQUIRED')
        shared_budget = deps.model_budget_factory()
        shared_budget.inspect(Decimal(authorization.deepseek['amount']))
        if getattr(args, "dry_run", False):
            return _output("PUBLIC_PREFLIGHT_OK", ledger_inspected=False, real_external_calls=0), 0
        ledger = BatchLedger(ledger_path, authorization, clock=clock)
        if getattr(args, "init_validation_batch", False):
            ledger.initialize()
            return _output("BATCH_INITIALIZED", real_external_calls=0), 0
        source = getattr(args, "credential_source", None)
        if source not in ("env", "prompt", "stored"):
            return _output("REFUSED", ("LIVE_CREDENTIAL_SOURCE_REQUIRED",)), 2
        ledger.validate_ready()
        credentials = deps.credential_reader(source)
        if not isinstance(credentials, ProjectCredentials):
            raise CredentialInputError()
        apify_transport = deps.apify_transport_factory()
        deepseek_transport = deps.deepseek_transport_factory()
        monitor = assemble_validation_monitor(config, settings, authorization, credentials, ledger,
                                             apify_transport=apify_transport, deepseek_transport=deepseek_transport,
                                             smtp_factory=deps.smtp_factory, source_catalog=deps.source_catalog,
                                             token_proofs=deps.token_proofs, apify_cost_proofs=deps.apify_cost_proofs,
                                             clock=clock, sleep=deps.sleep, monotonic=deps.monotonic,
                                             shared_model_budget=shared_budget)
        result = monitor.run()
        # Provider request counts live in the durable batch ledger. Do not
        # invent a real-network count from a successful local Monitor result.
        allowed = {name: getattr(result, name) for name in ("ingested", "classified", "sent")
                   if type(getattr(result, name, None)) is int and getattr(result, name) >= 0}
        output = _output(result.status, **allowed)
        return output, {"OK": 0, "DRY_RUN": 0, "LOCKED": 0, "DEGRADED": 1, "RECOVERY_REQUIRED": 2}.get(result.status, 2)
    except NoExternalCall as error:
        return _output("REFUSED", (error.code,)), 2
    except ModelBudgetError as error:
        return _output('REFUSED', (error.code,)), 2
    except CredentialInputError:
        return _output("REFUSED", ("PROJECT_CREDENTIAL_INPUT_REQUIRED",)), 2
    except StateError:
        return _output("REFUSED", ("LIVE_STATE_READ_INVALID",)), 2
    except (Exception, KeyboardInterrupt):
        return _output("REFUSED", ("LIVE_VALIDATION_FAILED",)), 2
