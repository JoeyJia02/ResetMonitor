"""Finite validation assembly. Production evidence catalogs deliberately block IO.

Operator approval and reviewed platform facts are distinct. Neither JSON flags
nor a token can add a source, tokenizer or full-chain cost proof to the trusted
Python catalogs. Synthetic tests inject explicitly artificial proof objects.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
import time
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from config import Config, SCHEDULE_TIMES, SCHEDULE_TIMEZONE
from .apify import ACTOR, ApifyFetcher, ApifyPolicy
from .batch import BatchAuthorization, BatchLedger, NoExternalCall
from .credentials import ProjectCredentials
from .deepseek import DeepSeekClassifier
from .deepseek_budget import GuardedDeepSeekTransport, PRICE_SNAPSHOT_ID, pricing_issue_codes
from .engine import Monitor
from .notifier import DeliveryResult
from .smtp_transport import SMTPConfig, SMTPNotifier
from .source_contract import SourceEvidenceError, resolve_source_contract
from .state import StateStore


@dataclass(frozen=True)
class ApifyCostProof:
    """Reviewed upper bounds for full per-attempt charging, not response size.

    A future real proof must cover billing units, transfer, rounding, errors,
    interrupted reads and storage exposure. Such a proof is NOT yet available.
    """
    proof_id: str
    source_contract_id: str
    build_id: str
    reviewed_at: datetime
    expires_at: datetime
    official_evidence_urls: tuple[str, ...]
    evidence_sha256: str
    start_max_cost: Decimal
    poll_max_cost: Decimal
    dataset_max_cost: Decimal


PRODUCTION_APIFY_COST_PROOFS = MappingProxyType({})
_SETTINGS = {"schema_version", "smtp_host", "smtp_port", "smtp_security", "apify_policy"}
_POLICY_FIELDS = {"available_balance_usd", "max_total_charge_usd", "minimum_charge_cap_usd", "per_result_usd"}
_TEST_PREFIX = "【ResetMonitor 测试】"


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _positive(value):
    return isinstance(value, Decimal) and value.is_finite() and 0 < value <= Decimal("1000000")


def _official_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        return False
    parts = urlsplit(value)
    return (parts.scheme == "https" and parts.netloc in {"apify.com", "docs.apify.com"}
            and bool(parts.path) and not parts.query and not parts.fragment)


def resolve_apify_cost_proof(authorization, *, now, source_catalog=None, apify_cost_proofs=None):
    catalog = PRODUCTION_APIFY_COST_PROOFS if apify_cost_proofs is None else apify_cost_proofs
    if not isinstance(catalog, Mapping):
        raise NoExternalCall("APIFY_TOTAL_COST_BOUND_UNVERIFIED")
    entry = catalog.get(authorization.apify["bound_evidence_id"])
    if not isinstance(entry, ApifyCostProof):
        raise NoExternalCall("APIFY_TOTAL_COST_BOUND_UNVERIFIED")
    contract = resolve_source_contract(authorization.source_contract_evidence_id,
        target_username=authorization.target_username, target_user_id=authorization.target_user_id,
        now=now, catalog=source_catalog)
    if (entry.proof_id != authorization.apify["bound_evidence_id"]
            or entry.source_contract_id != contract.contract_id or entry.build_id != contract.build_id
            or not isinstance(entry.proof_id, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", entry.proof_id) is None
            or not _aware(now) or not _aware(entry.reviewed_at) or not _aware(entry.expires_at)
            or not entry.reviewed_at <= now < entry.expires_at
            or not timedelta(0) < entry.expires_at - entry.reviewed_at <= timedelta(hours=24)
            or not isinstance(entry.official_evidence_urls, tuple) or not entry.official_evidence_urls
            or not all(_official_url(url) for url in entry.official_evidence_urls)
            or not isinstance(entry.evidence_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", entry.evidence_sha256) is None
            or not all(_positive(getattr(entry, name)) for name in ("start_max_cost", "poll_max_cost", "dataset_max_cost"))):
        raise NoExternalCall("APIFY_TOTAL_COST_BOUND_UNVERIFIED")
    for name in ("start_max_cost", "poll_max_cost", "dataset_max_cost"):
        if Decimal(authorization.apify[name]) < getattr(entry, name):
            raise NoExternalCall("APIFY_APPROVED_COST_BOUND_INSUFFICIENT")
    return entry


def _policy(settings):
    values = settings["apify_policy"]
    return ApifyPolicy(**values, approved=True, pricing_verified=True, source_contract_verified=True)


def validation_preflight(config, settings, authorization, *, now, source_catalog=None,
                         token_proofs=None, apify_cost_proofs=None):
    """Pure checks before any secret input, service construction or ledger writes."""
    issues = []
    if not isinstance(config, Config) or not isinstance(authorization, BatchAuthorization):
        return ("VALIDATION_CONFIGURATION_REQUIRED",)
    try:
        authorization.validate_at(now)
    except (NoExternalCall, ValueError, TypeError, OverflowError):
        return ("VALIDATION_AUTHORIZATION_INVALID_OR_EXPIRED",)
    # This entry point is only for one finite three-item validation batch.
    # Broader authorizations do not enlarge this locally reviewed envelope.
    if (authorization.apify["max_run_starts"] > 1
            or authorization.apify["max_run_polls"] > 3
            or authorization.apify["max_dataset_reads"] > 3
            or authorization.apify["max_http_attempts"] > 7
            or authorization.deepseek["max_requests"] > 9
            or authorization.smtp["max_attempts"] > 3):
        issues.append("VALIDATION_OPERATION_LIMIT_EXCEEDS_ENVELOPE")
    if (config.target_username != authorization.target_username
            or config.target_user_id != authorization.target_user_id
            or config.state_path != authorization.state_path or config.email_to != authorization.recipient
            or config.fetch_limit != 3 or type(config.fetch_limit) is not int
            or type(config.schedule_timezone) is not str or config.schedule_timezone != SCHEDULE_TIMEZONE
            or type(config.schedule_times) is not tuple or config.schedule_times != SCHEDULE_TIMES
            or any(type(item) is not str for item in config.schedule_times)
            or config.include_replies is not True or config.exclude_retweets is not True):
        issues.append("VALIDATION_SCOPE_BINDING_MISMATCH")
    if (config.retry_attempts != 3 or type(config.retry_attempts) is not int
            or config.retry_delays != (5, 15) or type(config.retry_delays) is not tuple
            or any(type(v) is not int for v in config.retry_delays)
            or config.failure_cooldown_seconds != 7200 or type(config.failure_cooldown_seconds) is not int
            or config.unknown_cooldown_seconds != 86400 or type(config.unknown_cooldown_seconds) is not int
            or config.classification_retry_budget_seconds != 60 or type(config.classification_retry_budget_seconds) is not int):
        issues.append("RETRY_POLICY_INVALID")
    if type(settings) is not dict or set(settings) != _SETTINGS or type(settings.get("schema_version")) is not int or settings["schema_version"] != 1:
        issues.append("VALIDATION_PUBLIC_SETTINGS_INVALID")
    else:
        try:
            policy = settings["apify_policy"]
            if type(policy) is not dict or set(policy) != _POLICY_FIELDS:
                raise ValueError
            checked = _policy(settings).checked_budget()
            if Decimal(checked["approved_cap_usd"]) > Decimal(authorization.apify["start_max_cost"]):
                issues.append("APIFY_RUN_CAP_EXCEEDS_RESERVED_START_BOUND")
            SMTPConfig(host=settings["smtp_host"], port=settings["smtp_port"], security=settings["smtp_security"],
                       sender=authorization.sender, recipient=authorization.recipient,
                       user="synthetic-public-validation", password="synthetic-public-validation")
        except Exception:
            issues.append("VALIDATION_PUBLIC_SETTINGS_INVALID")
    try:
        resolve_source_contract(authorization.source_contract_evidence_id,
            target_username=authorization.target_username, target_user_id=authorization.target_user_id,
            now=now, catalog=source_catalog)
    except (SourceEvidenceError, ValueError, TypeError, OverflowError):
        issues.append("APIFY_SOURCE_CONTRACT_UNVERIFIED")
    if authorization.deepseek["price_snapshot_id"] != PRICE_SNAPSHOT_ID:
        issues.append("DEEPSEEK_PRICE_SNAPSHOT_BINDING_MISMATCH")
    issues.extend(pricing_issue_codes(authorization.deepseek["model"], authorization.deepseek["currency"],
                                     now=now, token_proofs=token_proofs))
    try:
        resolve_apify_cost_proof(authorization, now=now, source_catalog=source_catalog,
                                apify_cost_proofs=apify_cost_proofs)
    except (NoExternalCall, SourceEvidenceError, ValueError, TypeError, OverflowError):
        issues.append("APIFY_TOTAL_COST_BOUND_UNVERIFIED")
    return tuple(dict.fromkeys(issues))


class GuardedApifyTransport:
    def __init__(self, transport, ledger, *, source_catalog=None, apify_cost_proofs=None, clock=None):
        self.transport, self.ledger = transport, ledger
        self.source_catalog, self.apify_cost_proofs = source_catalog, apify_cost_proofs
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def request(self, method, url, *, headers, body, timeout, deadline=None):
        auth = self.ledger.authorization
        def reviewed():
            try:
                now = self.clock()
                proof = resolve_apify_cost_proof(auth, now=now, source_catalog=self.source_catalog,
                                                apify_cost_proofs=self.apify_cost_proofs)
                contract = resolve_source_contract(auth.source_contract_evidence_id,
                    target_username=auth.target_username, target_user_id=auth.target_user_id,
                    now=now, catalog=self.source_catalog)
                return proof, contract
            except Exception:
                raise NoExternalCall("APIFY_TOTAL_COST_BOUND_UNVERIFIED") from None
        proof, contract = reviewed()
        try:
            if not isinstance(url, str) or not url.isascii() or any(ord(character) <= 32 or ord(character) == 127 for character in url):
                raise ValueError
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.netloc != "api.apify.com" or parts.fragment:
                raise ValueError
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError
            if method == "POST" and parts.path == f"/v2/acts/{ACTOR}/runs":
                operation, bound_name = "apify_start", "start_max_cost"
                if (set(query) != {"maxTotalChargeUsd", "restartOnError", "waitForFinish", "build"}
                        or query["restartOnError"] != ["false"] or query["waitForFinish"] != ["0"]
                        or query["build"] != [contract.build_number]):
                    raise ValueError
                cap_text = query["maxTotalChargeUsd"][0]
                if re.fullmatch(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?", cap_text) is None:
                    raise ValueError
                cap = Decimal(cap_text)
                if not cap.is_finite() or not 0 < cap <= Decimal(auth.apify[bound_name]):
                    raise ValueError
                if not isinstance(body, bytes) or len(body) > 1_048_576:
                    raise ValueError
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError
                        result[key] = value
                    return result
                def invalid_constant(_value):
                    raise ValueError
                payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
                if (type(payload) is not dict or set(payload) != {"profiles", "maxResults"}
                        or payload["profiles"] != [auth.target_username]
                        or type(payload["maxResults"]) is not int or payload["maxResults"] != 3):
                    raise ValueError
            elif method == "GET" and re.fullmatch(r"/v2/actor-runs/[A-Za-z0-9]{17}", parts.path) and body is None:
                operation, bound_name = "apify_poll", "poll_max_cost"
                if query:
                    raise ValueError
            elif method == "GET" and re.fullmatch(r"/v2/datasets/[A-Za-z0-9]{17}/items", parts.path) and body is None:
                operation, bound_name = "apify_dataset", "dataset_max_cost"
                expected = {"format": ["json"], "desc": ["false"], "clean": ["false"],
                            "skipEmpty": ["false"], "skipHidden": ["false"]}
                if (set(query) != set(expected) | {"limit", "offset"}
                        or any(query[name] != value for name, value in expected.items())
                        or re.fullmatch(r"[1-3]", query["limit"][0]) is None
                        or re.fullmatch(r"[0-2]", query["offset"][0]) is None
                        or int(query["limit"][0]) + int(query["offset"][0]) > 3):
                    raise ValueError
            else:
                raise ValueError
        except Exception:
            raise NoExternalCall("APIFY_UNAPPROVED_OPERATION") from None
        self.ledger.reserve(operation, amount=Decimal(auth.apify[bound_name]), currency="USD")
        # Waiting for the ledger lock or committing it can cross an evidence
        # expiry. The reservation remains consumed, but no stale-proof IO may
        # start. Also reject catalog changes between validation and the wire.
        if reviewed() != (proof, contract):
            raise NoExternalCall("APIFY_TOTAL_COST_BOUND_UNVERIFIED")
        # Apify's injected legacy fake HTTP boundary need not support deadline.
        return self.transport.request(method, url, headers=headers, body=body, timeout=timeout)


class GuardedSMTPNotifier:
    def __init__(self, notifier, ledger):
        self.notifier, self.ledger = notifier, ledger

    def send(self, notification):
        auth = self.ledger.authorization
        if (not isinstance(notification, dict) or notification.get("recipient") != auth.recipient
                or not isinstance(notification.get("subject"), str)
                or not notification["subject"].startswith(_TEST_PREFIX)):
            return DeliveryResult("FAILED", retryable=False, error_code="VALIDATION_MAIL_BINDING_MISMATCH")
        try:
            self.ledger.reserve("smtp_attempt", metadata={"notification_kind":
                "HEALTH" if notification.get("label") == "HEALTH" else "TWEET"})
        except Exception:
            return DeliveryResult("FAILED", retryable=False, error_code="VALIDATION_BATCH_REFUSED")
        return self.notifier.send(deepcopy(notification))


def assemble_validation_monitor(config, settings, authorization, credentials, ledger, *,
                                apify_transport, deepseek_transport, smtp_factory,
                                source_catalog=None, token_proofs=None, apify_cost_proofs=None,
                                clock=None, sleep=time.sleep, monotonic=time.monotonic, store=None, shared_model_budget=None):
    """Construct guarded components, no IO. CLI verifies disk state/ledger first."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    issues = validation_preflight(config, settings, authorization, now=now,
        source_catalog=source_catalog, token_proofs=token_proofs, apify_cost_proofs=apify_cost_proofs)
    if issues:
        raise NoExternalCall(issues[0])
    if (not isinstance(ledger, BatchLedger) or ledger.authorization != authorization
            or not isinstance(credentials, ProjectCredentials)):
        raise NoExternalCall("VALIDATION_ASSEMBLY_BINDING_INVALID")
    if (not callable(getattr(apify_transport, "request", None))
            or not callable(getattr(deepseek_transport, "request", None)) or not callable(smtp_factory)):
        raise NoExternalCall("VALIDATION_EXPLICIT_BOUNDARIES_REQUIRED")
    if store is not None and (not isinstance(store, StateStore) or store.path != config.state_path):
        raise NoExternalCall("STATE_STORE_PATH_MISMATCH")
    from dataclasses import replace
    config = replace(config, execution_mode="LIVE")
    fetcher = ApifyFetcher(api_token=credentials.apify_token,
        transport=GuardedApifyTransport(apify_transport, ledger, source_catalog=source_catalog,
            apify_cost_proofs=apify_cost_proofs, clock=clock), policy=_policy(settings),
        target=config.target_username, target_user_id=config.target_user_id,
        contract_id=authorization.source_contract_evidence_id, contract_catalog=source_catalog, clock=clock)
    classifier = DeepSeekClassifier(credentials.deepseek_api_key,
        GuardedDeepSeekTransport(deepseek_transport, ledger, currency=authorization.deepseek["currency"],
                                 token_proofs=token_proofs, clock=clock, shared_budget=shared_model_budget),
        model=authorization.deepseek["model"], monotonic=monotonic)
    notifier = GuardedSMTPNotifier(SMTPNotifier(SMTPConfig(host=settings["smtp_host"], port=settings["smtp_port"],
        security=settings["smtp_security"], sender=authorization.sender, recipient=authorization.recipient,
        user=credentials.smtp_user, password=credentials.smtp_password,
        auth_mechanism='LOGIN' if settings['smtp_host'].lower() == 'smtp.qq.com' else 'PLAIN'), smtp_factory=smtp_factory), ledger)
    return Monitor(config, store if store is not None else StateStore(config.state_path), fetcher, classifier, notifier,
                   clock=clock, sleep=sleep, monotonic=monotonic, test_notifications=True)
