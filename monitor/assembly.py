"""Explicit integration assembly, exercised with synthetic credentials and IO.

This legacy entry point delegates to the same guarded finite-validation
assembly as the explicit live CLI. There is no default network transport,
environment lookup or state inspection here. In particular,
``state_inspected`` and ``state_paths_verified`` are attestations by the caller,
not filesystem checks performed by this module. The Monitor must independently
check the persisted execution mode under its process lock when it runs.

The tests label isolated, artificial state LIVE to exercise the integration
boundary; that label does not establish real service or spending authorization.
Explicitly injected transports/factories remain the caller's responsibility.
"""

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import inspect
import ntpath
from pathlib import Path
import time
from typing import Callable

from config import Config, SCHEDULE_TIMES, SCHEDULE_TIMEZONE
from .apify import ApifyError, ApifyPolicy
from .batch import BatchAuthorization, BatchLedger, NoExternalCall
from .credentials import ProjectCredentials
from .engine import Monitor
from .http_transport import HttpTransport
from .smtp_transport import SMTPConfig
from .state import StateError, StateStore, validate_state


AUTHORIZED_TARGET = "thsottiaux"
AUTHORIZED_RECIPIENT = LOCAL_RECIPIENT
# Local input-sanity ceiling only, not a recommended or approved budget, a
# provider cap, or an accounting mechanism for calls made after construction.
MAX_DECLARED_DEEPSEEK_TEST_BUDGET_USD = Decimal("1")
_APIFY_ISSUES = frozenset({
    "APIFY_BUDGET_NOT_APPROVED", "APIFY_SOURCE_CONTRACT_UNVERIFIED",
    "APIFY_BUDGET_FACTS_UNKNOWN", "APIFY_APPROVED_CAP_INSUFFICIENT",
    "APIFY_BALANCE_INSUFFICIENT",
})


@dataclass(frozen=True)
class IntegrationCredentials:
    """Explicit credential values; representations never include their contents."""

    apify_token: str = field(default="", repr=False)
    deepseek_api_key: str = field(default="", repr=False)
    smtp_user: str = field(default="", repr=False)
    smtp_password: str = field(default="", repr=False)


@dataclass(frozen=True)
class PreparedIntegration:
    """Untrusted preparation facts, not a grant of authorization.

    All gates default to unapproved. ``offline_state_paths`` must enumerate
    the offline and demo state files, including any additional caller-created
    variants. The caller checks filesystem aliases; pure preflight additionally
    rejects lexical overlap between their state/backup/lock paths.
    """

    config: Config
    credentials: IntegrationCredentials = field(default_factory=IntegrationCredentials, repr=False)
    apify_policy: ApifyPolicy = field(default_factory=ApifyPolicy)
    deepseek_credential_use_authorized: bool = False
    deepseek_test_budget_approved: bool = False
    deepseek_test_budget_usd: Decimal | str | float | None = None
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_sender: str = field(default="", repr=False)
    smtp_security: str = "SSL"
    recipient_authorized: bool = False
    sender_authorized: bool = False
    send_authorized: bool = False
    offline_state_paths: tuple[Path, ...] = ()
    state_inspected: bool = False
    state_paths_verified: bool = False
    existing_state: dict | None = field(default=None, repr=False)
    authorization: BatchAuthorization | None = field(default=None, repr=False)
    ledger: BatchLedger | None = field(default=None, repr=False)
    source_catalog: object = field(default=None, repr=False)
    token_proofs: object = field(default=None, repr=False)
    apify_cost_proofs: object = field(default=None, repr=False)
    checked_at: datetime | None = None


@dataclass(frozen=True)
class PreflightReport:
    issue_codes: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issue_codes


class PreflightError(ValueError):
    """Only fixed issue codes are exposed, never supplied values or exceptions."""

    def __init__(self, report: PreflightReport):
        self.report = report
        super().__init__("INTEGRATION_PREFLIGHT_FAILED: " + ",".join(report.issue_codes))


def _credential(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 4096
            and all(33 <= ord(character) <= 126 for character in value))


def _declared_test_budget(value: object) -> bool:
    """Check a caller attestation's amount, without inferring spending authority."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        return False
    try:
        text = str(value)
        if len(text) > 128:
            return False
        amount = Decimal(text)
        return amount.is_finite() and 0 < amount <= MAX_DECLARED_DEEPSEEK_TEST_BUDGET_USD
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return False


def _absolute_path(value: object) -> str | None:
    # No resolve(), exists(), stat() or file reads: these checks are lexical.
    if not isinstance(value, Path) or not value.is_absolute():
        return None
    text = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None
    if value.name in ("", ".", ".."):
        return None
    return ntpath.normcase(ntpath.normpath(text))


def _state_artifacts(path: str) -> set[str]:
    return {path, path + ".bak", path + ".lock", path + ".guard"}


def _smtp_config(plan: PreparedIntegration) -> SMTPConfig:
    credentials = plan.credentials
    return SMTPConfig(host=plan.smtp_host, port=plan.smtp_port,
                      sender=plan.smtp_sender, recipient=plan.config.email_to,
                      user=credentials.smtp_user, password=credentials.smtp_password,
                      security=plan.smtp_security)


def _settings(plan: PreparedIntegration) -> dict:
    """Public non-secret settings shared with the guarded validation path."""
    policy = plan.apify_policy
    return {"schema_version": 1, "smtp_host": plan.smtp_host, "smtp_port": plan.smtp_port,
            "smtp_security": plan.smtp_security, "apify_policy": {
                "available_balance_usd": policy.available_balance_usd,
                "max_total_charge_usd": policy.max_total_charge_usd,
                "minimum_charge_cap_usd": policy.minimum_charge_cap_usd,
                "per_result_usd": policy.per_result_usd,
            }}


def preflight(plan: PreparedIntegration) -> PreflightReport:
    """Validate explicit facts without constructing services or performing IO."""
    if not isinstance(plan, PreparedIntegration) or not isinstance(plan.config, Config):
        return PreflightReport(("INTEGRATION_PLAN_INVALID",))
    issues = []
    config = plan.config
    if config.target_username != AUTHORIZED_TARGET:
        issues.append("TARGET_NOT_AUTHORIZED")
    if config.target_user_id is None:
        issues.append("TARGET_ID_REQUIRED")
    if config.include_replies is not True or config.exclude_retweets is not True:
        issues.append("SOURCE_SCOPE_INVALID")
    if type(config.fetch_limit) is not int or config.fetch_limit != 3:
        issues.append("FETCH_LIMIT_MUST_BE_THREE")
    if (type(config.schedule_timezone) is not str or config.schedule_timezone != SCHEDULE_TIMEZONE
            or type(config.schedule_times) is not tuple or config.schedule_times != SCHEDULE_TIMES
            or any(type(item) is not str for item in config.schedule_times)):
        issues.append("FIXED_SHANGHAI_SCHEDULE_REQUIRED")
    retry_values = (
        (config.retry_attempts, 3), (config.failure_cooldown_seconds, 7200),
        (config.unknown_cooldown_seconds, 86400),
        (config.classification_retry_budget_seconds, 60),
    )
    if (any(type(value) is not int or value != expected for value, expected in retry_values)
            or type(config.retry_delays) is not tuple or config.retry_delays != (5, 15)
            or any(type(delay) is not int for delay in config.retry_delays)):
        # Direct Config construction does not validate these engine policies.
        # Never shorten the uncertainty cooldown or silently reset bad values.
        issues.append("RETRY_POLICY_INVALID")
    if config.email_to != AUTHORIZED_RECIPIENT:
        issues.append("RECIPIENT_NOT_AUTHORIZED")
    if plan.recipient_authorized is not True:
        issues.append("RECIPIENT_APPROVAL_REQUIRED")
    if plan.sender_authorized is not True:
        issues.append("SMTP_SENDER_APPROVAL_REQUIRED")
    if plan.send_authorized is not True:
        issues.append("SMTP_SEND_APPROVAL_REQUIRED")
    # Neither a syntactically valid key nor an Apify approval authorizes use of
    # this project's DeepSeek credential. These facts are independent and are
    # declarations only; this module does not track/enforce runtime expenditure.
    if plan.deepseek_credential_use_authorized is not True:
        issues.append("DEEPSEEK_CREDENTIAL_USE_APPROVAL_REQUIRED")
    if plan.deepseek_test_budget_approved is not True:
        issues.append("DEEPSEEK_TEST_BUDGET_APPROVAL_REQUIRED")
    if not _declared_test_budget(plan.deepseek_test_budget_usd):
        issues.append("DEEPSEEK_TEST_BUDGET_AMOUNT_INVALID")

    credentials = plan.credentials
    if not isinstance(credentials, IntegrationCredentials):
        issues.append("CREDENTIALS_INVALID")
    else:
        if not _credential(credentials.apify_token):
            issues.append("APIFY_TOKEN_INVALID")
        if not _credential(credentials.deepseek_api_key):
            issues.append("DEEPSEEK_KEY_INVALID")
        if not _credential(credentials.smtp_user) or not _credential(credentials.smtp_password):
            issues.append("SMTP_CONFIG_INVALID")
        try:
            _smtp_config(plan)  # A validated value object; never connects.
        except (ValueError, TypeError, AttributeError):
            issues.append("SMTP_CONFIG_INVALID")

    if not isinstance(plan.apify_policy, ApifyPolicy):
        issues.append("APIFY_POLICY_INVALID")
    else:
        try:
            plan.apify_policy.checked_budget()
        except ApifyError as error:
            issues.append(error.code if error.code in _APIFY_ISSUES else "APIFY_POLICY_INVALID")
        except (ValueError, TypeError, AttributeError, OverflowError):
            issues.append("APIFY_POLICY_INVALID")

    live_path = _absolute_path(config.state_path)
    if live_path is None:
        issues.append("LIVE_STATE_ABSOLUTE_PATH_REQUIRED")
    paths = plan.offline_state_paths
    if not isinstance(paths, tuple) or len(paths) < 2:
        issues.append("OFFLINE_DEMO_PATHS_REQUIRED")
    else:
        offline_paths = [_absolute_path(path) for path in paths]
        if None in offline_paths or len(set(offline_paths)) != len(offline_paths):
            issues.append("OFFLINE_DEMO_PATHS_INVALID")
        elif live_path is not None and any(
                _state_artifacts(live_path) & _state_artifacts(path) for path in offline_paths):
            issues.append("LIVE_OFFLINE_STATE_PATH_CONFLICT")
    if plan.state_paths_verified is not True:
        issues.append("STATE_PATH_ALIAS_REVIEW_REQUIRED")
    if plan.state_inspected is not True:
        issues.append("STATE_INSPECTION_REQUIRED")
    state = plan.existing_state
    if state is not None:
        if not isinstance(state, dict):
            issues.append("EXISTING_STATE_INVALID")
        else:
            if state.get("execution_mode") != "LIVE":
                issues.append("STATE_ORIGIN_UNSAFE")
            if state.get("target") != AUTHORIZED_TARGET:
                issues.append("STATE_TARGET_MISMATCH")
            if state.get("target_user_id") != config.target_user_id:
                issues.append("STATE_TARGET_ID_MISMATCH")
            if state.get("recovery_required") or state.get("recovered_from_backup"):
                issues.append("STATE_RECOVERY_REVIEW_REQUIRED")
            try:
                validate_state(state)
            except (StateError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                issues.append("EXISTING_STATE_INVALID")
    if not isinstance(plan.authorization, BatchAuthorization):
        issues.append("VALIDATION_AUTHORIZATION_REQUIRED")
    elif not isinstance(plan.ledger, BatchLedger):
        issues.append("VALIDATION_LEDGER_REQUIRED")
    elif plan.ledger.authorization != plan.authorization:
        issues.append("VALIDATION_LEDGER_AUTHORIZATION_MISMATCH")
    if isinstance(plan.ledger, BatchLedger):
        ledger_path = _absolute_path(plan.ledger.path)
        if (isinstance(plan.authorization, BatchAuthorization)
                and ledger_path != _absolute_path(plan.authorization.ledger_path)):
            issues.append("VALIDATION_LEDGER_PATH_BINDING_MISMATCH")
        if ledger_path is None or (live_path is not None and _state_artifacts(ledger_path) & _state_artifacts(live_path)):
            issues.append("VALIDATION_LEDGER_PATH_INVALID")
        if isinstance(paths, tuple) and ledger_path is not None:
            if any(_absolute_path(path) is not None and
                   _state_artifacts(ledger_path) & _state_artifacts(_absolute_path(path)) for path in paths):
                issues.append("VALIDATION_LEDGER_OFFLINE_PATH_CONFLICT")
    if isinstance(plan.authorization, BatchAuthorization):
        if (plan.authorization.target_username != config.target_username
                or plan.authorization.target_user_id != config.target_user_id
                or _absolute_path(plan.authorization.state_path) != live_path
                or plan.authorization.recipient != config.email_to
                or plan.authorization.sender != plan.smtp_sender):
            issues.append("VALIDATION_AUTHORIZATION_BINDING_INVALID")
        try:
            facts = plan.authorization.deepseek
            if (facts["currency"] != "USD" or not _declared_test_budget(plan.deepseek_test_budget_usd)
                    or Decimal(str(plan.deepseek_test_budget_usd)) != Decimal(facts["amount"])):
                issues.append("DEEPSEEK_TEST_BUDGET_BINDING_INVALID")
        except (NoExternalCall, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            issues.append("VALIDATION_AUTHORIZATION_BINDING_INVALID")
        if isinstance(plan.apify_policy, ApifyPolicy):
            from .validation import validation_preflight
            result = validation_preflight(config, _settings(plan), plan.authorization,
                                          now=plan.checked_at,
                                          source_catalog=plan.source_catalog,
                                          token_proofs=plan.token_proofs,
                                          apify_cost_proofs=plan.apify_cost_proofs)
            issues.extend(result)
    return PreflightReport(tuple(dict.fromkeys(issues)))


def _deadline_transport(transport: object) -> bool:
    request = getattr(transport, "request", None)
    if not callable(request):
        return False
    try:
        parameters = inspect.signature(request).parameters.values()
        return any(parameter.kind == inspect.Parameter.VAR_KEYWORD
                   or parameter.name == "deadline" and parameter.kind in (
                       inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                   for parameter in parameters)
    except (TypeError, ValueError):
        return False


def assemble_monitor(plan: PreparedIntegration, *, store: StateStore,
                     apify_transport: HttpTransport, deepseek_transport: HttpTransport,
                     smtp_factory: Callable, clock=None, sleep=time.sleep,
                     monotonic=time.monotonic) -> Monitor:
    """Construct the real adapter classes using explicitly injected IO objects.

    No request or send occurs here. Tests inject only artificial HTTP replies
    and SMTP sessions. This function does not discover keys, inspect files,
    schedule work, enable the CLI, or choose a real transport/factory.
    """
    report = preflight(plan)
    issues = list(report.issue_codes)
    if not isinstance(store, StateStore) or (
            isinstance(plan, PreparedIntegration) and isinstance(plan.config, Config)
            and _absolute_path(store.path) != _absolute_path(plan.config.state_path)):
        issues.append("STATE_STORE_PATH_MISMATCH")
    if not callable(getattr(apify_transport, "request", None)):
        issues.append("APIFY_TRANSPORT_REQUIRED")
    if not _deadline_transport(deepseek_transport):
        issues.append("DEEPSEEK_DEADLINE_TRANSPORT_REQUIRED")
    if not callable(smtp_factory):
        issues.append("SMTP_FACTORY_REQUIRED")
    if clock is not None and not callable(clock) or not callable(sleep) or not callable(monotonic):
        issues.append("INTEGRATION_CLOCK_INVALID")
    if issues:
        raise PreflightError(PreflightReport(tuple(dict.fromkeys(issues))))
    # The legacy entry point uses the same durable budget guards as the CLI.
    # Never construct bare adapters here, even when credentials are valid.
    from .validation import assemble_validation_monitor
    credentials = plan.credentials
    explicit = ProjectCredentials(apify_token=credentials.apify_token,
                                  deepseek_api_key=credentials.deepseek_api_key,
                                  smtp_user=credentials.smtp_user,
                                  smtp_password=credentials.smtp_password)
    try:
        return assemble_validation_monitor(
            plan.config, _settings(plan), plan.authorization, explicit, plan.ledger,
            apify_transport=apify_transport, deepseek_transport=deepseek_transport,
            smtp_factory=smtp_factory, clock=clock, sleep=sleep, monotonic=monotonic,
            source_catalog=plan.source_catalog, token_proofs=plan.token_proofs,
            apify_cost_proofs=plan.apify_cost_proofs, store=store)
    except NoExternalCall as error:
        raise PreflightError(PreflightReport((error.code,))) from None
