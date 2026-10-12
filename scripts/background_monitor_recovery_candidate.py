"""Persistent per-user background monitor for the three Beijing time slots.

The Windows logon task starts this process with pythonw. Each slot is claimed
durably before any provider call. Failed work is resumed only at a later
scheduled slot, within that slot's one-model/one-mail limits. An uncertain SMTP
attempt is never repeated automatically. Source rows are held in a durable
journal until analyzed and, when needed, definitely delivered.
"""

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.credential_store import CredentialStoreError, load_credentials
from monitor.credentials import ProjectCredentials
from monitor.locking import LockBusy, ProcessLock
from monitor.smtp_transport import SMTPConfig, SMTPNotifier, stdlib_smtp_factory
from scripts import analyze_recovered_three as batch
from scripts import manual_test as manual
from scripts.one_shot_source_diagnostic_candidate import OneShotSourceError, fetch_three
from scripts.test_full_chain_once import RECIPIENT, SENDER, _body, _notification, _safe
from scripts.verify_api_connections import _build_transport, _valid_secret


BEIJING = timezone(timedelta(hours=8))
SLOTS = ((8, 0), (13, 0), (20, 0))
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_RESULTS = frozenset({"POST_RESET", "PRE_RESET", "POSSIBLE_RESET"})
_LABELS = _RESULTS | {"IRRELEVANT"}
_RECOVERABLE = frozenset({"SOURCE_READY", "MODEL_ATTEMPTED", "MODEL_FAILED",
                          "ANALYZED", "SMTP_FAILED"})
_JOURNAL_STATES = _RECOVERABLE | {"SMTP_ATTEMPTED", "SMTP_UNKNOWN", "SENT",
                                   "NO_RELEVANT_ITEMS", "NO_NEW_ITEMS",
                                   "MODEL_REVIEW_REQUIRED", "SMTP_REVIEW_REQUIRED",
                                   "SUPERSEDED_BY_MANUAL"}
_JOURNAL_NAME = re.compile(r"scheduled-[0-9]{8}-(?:0800|1300|2000)\.result\.json\Z")
_SOURCE_CODES = frozenset({
    "ONE_SHOT_SOURCE_TOKEN_INVALID", "ONE_SHOT_SOURCE_CLOCK_INVALID",
    "ONE_SHOT_SOURCE_COUNT_INVALID", "ONE_SHOT_SOURCE_TOTAL_INVALID",
    "ONE_SHOT_SOURCE_TOTAL_MISMATCH", "ONE_SHOT_SOURCE_ROW_FAILED",
    "ONE_SHOT_SOURCE_PROFILE_MISMATCH", "ONE_SHOT_SOURCE_ID_INVALID",
    "ONE_SHOT_SOURCE_IDENTITY_MISMATCH", "ONE_SHOT_SOURCE_TEXT_INVALID",
    "ONE_SHOT_SOURCE_TIME_INVALID", "ONE_SHOT_SOURCE_TIME_IN_FUTURE",
    "ONE_SHOT_SOURCE_FLAGS_INVALID", "ONE_SHOT_SOURCE_URL_MISMATCH",
    "ONE_SHOT_SOURCE_DUPLICATE_ID", "ONE_SHOT_SOURCE_ORDER_INVALID",
    "ONE_SHOT_SOURCE_REPLY_INVALID", "ONE_SHOT_SOURCE_SECRET_REFLECTION",
    "ONE_SHOT_EMBED_ROOT_INVALID", "ONE_SHOT_EMBED_ID_MISMATCH",
    "ONE_SHOT_EMBED_AUTHOR_INVALID", "ONE_SHOT_EMBED_IDENTITY_MISMATCH",
    "ONE_SHOT_EMBED_TIME_MISMATCH", "ONE_SHOT_EMBED_TEXT_INVALID",
    "ONE_SHOT_EMBED_TEXT_TRUNCATED", "ONE_SHOT_EMBED_TEXT_MISMATCH",
    "ONE_SHOT_EMBED_QUOTE_INVALID", "ONE_SHOT_EMBED_QUOTE_DEPTH_INVALID",
    "ONE_SHOT_EMBED_QUOTE_MISSING", "ONE_SHOT_EMBED_CARD_INVALID",
    "ONE_SHOT_EMBED_POLL_INVALID",
}) | frozenset(prefix + suffix for prefix in ("ONE_SHOT_SOURCE", "ONE_SHOT_EMBED")
               for suffix in ("_HTTP_FAILED", "_TOTAL_INVALID", "_BODY_INVALID",
                              "_SECRET_REFLECTION", "_JSON_INVALID", "_REQUEST_FAILED"))
_BACKGROUND_CODES = frozenset({
    "BACKGROUND_CLOCK_INVALID", "BACKGROUND_CREDENTIAL_INVALID",
    "BACKGROUND_DIAGNOSTIC_STAGE_INVALID", "BACKGROUND_JOURNAL_REVIEW_REQUIRED",
    "BACKGROUND_REQUEST_SCOPE_INVALID", "BACKGROUND_SOURCE_INVALID",
    "BACKGROUND_STATE_REVIEW_REQUIRED",
})
_CREDENTIAL_CODES = frozenset({
    "CREDENTIAL_STORE_UNAVAILABLE", "CREDENTIAL_STORE_READ_FAILED",
    "CREDENTIAL_STORE_WRITE_FAILED", "CREDENTIAL_STORE_DELETE_FAILED",
    "CREDENTIAL_STORE_INVALID", "CREDENTIAL_STORE_TOO_LARGE",
})
_SMTP_CODES = frozenset({
    "SMTP_INVALID_MESSAGE", "SMTP_PROTOCOL_ERROR", "SMTP_DATA_UNCERTAIN",
    "SMTP_TLS_VERIFICATION_FAILED", "SMTP_TLS_OR_AUTH_UNSUPPORTED",
}) | frozenset("SMTP_" + phase + suffix
               for phase in ("CONNECT", "EHLO", "STARTTLS", "AUTH", "MAIL", "RCPT",
                             "DATA_READY", "DATA")
               for suffix in ("_REJECTED", "_UNAVAILABLE"))
_DIAGNOSTIC_STAGES = frozenset({
    "ROOT_SETUP", "SLOT_LOCK", "CLAIM_WRITE", "RECEIPT_INIT", "LOCK_ACQUIRE", "SEEN_LOAD",
    "JOURNAL_SCAN", "MANUAL_SEED", "CREDENTIAL_LOAD", "CREDENTIAL_VALIDATE",
    "PENDING_RECONCILE", "TRANSPORT_BUILD", "SOURCE_FETCH", "SOURCE_VALIDATE",
    "SOURCE_JOURNAL_WRITE", "SOURCE_PREFLIGHT", "ACTOR_POST_OPEN", "ACTOR_POST_HTTP",
    "ACTOR_POST_HEADERS", "ACTOR_POST_BODY", "ACTOR_POST_JSON", "ACTOR_ROWS",
    "CONTEXT_GET_OPEN", "CONTEXT_GET_HTTP", "CONTEXT_GET_BODY", "CONTEXT_GET_JSON",
    "CONTEXT_ROOT", "CONTEXT_ID", "CONTEXT_AUTHOR", "CONTEXT_TIME",
    "CONTEXT_TEXT", "CONTEXT_QUOTE", "CONTEXT_REPLY", "CONTEXT_POLL",
    "RECOVERY_PROCESS", "MODEL_REQUEST", "MODEL_RESULT_WRITE", "NOTIFICATION_BUILD",
    "SMTP_SEND", "DELIVERY_JOURNAL_WRITE", "SEEN_WRITE", "RECEIPT_FINAL", "COMPLETE",
})


def _exception_class(error):
    """Only fixed categories; never include provider-controlled class names or text."""
    if isinstance(error, HTTPError):
        return "HTTP_ERROR"
    if isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "TLS_CERT_ERROR"
        if isinstance(reason, ssl.SSLError):
            return "SSL_ERROR"
        if isinstance(reason, TimeoutError):
            return "TIMEOUT_ERROR"
        if isinstance(reason, OSError):
            return "OS_ERROR"
        return "URL_ERROR"
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS_CERT_ERROR"
    if isinstance(error, ssl.SSLError):
        return "SSL_ERROR"
    if isinstance(error, TimeoutError):
        return "TIMEOUT_ERROR"
    if isinstance(error, PermissionError):
        return "PERMISSION_ERROR"
    if isinstance(error, json.JSONDecodeError):
        return "JSON_ERROR"
    if isinstance(error, UnicodeError):
        return "UNICODE_ERROR"
    if isinstance(error, OSError):
        return "OS_ERROR"
    if isinstance(error, OneShotSourceError):
        return "SOURCE_VALIDATION"
    if isinstance(error, CredentialStoreError):
        return "CREDENTIAL_STORE_ERROR"
    if isinstance(error, LockBusy):
        return "LOCK_BUSY"
    if isinstance(error, BackgroundError):
        return "BACKGROUND_VALIDATION"
    if isinstance(error, TypeError):
        return "TYPE_ERROR"
    if isinstance(error, ValueError):
        return "VALUE_ERROR"
    return "OTHER_EXCEPTION"


def _error_code(stage, error):
    if (stage in ("CREDENTIAL_LOAD", "CREDENTIAL_VALIDATE")
            and isinstance(error, CredentialStoreError)
            and isinstance(error.code, str) and error.code in _CREDENTIAL_CODES):
        return error.code
    if (isinstance(error, OneShotSourceError) and isinstance(error.code, str)
            and error.code in _SOURCE_CODES):
        return error.code
    if stage == "ACTOR_POST_JSON" and isinstance(error, (UnicodeError, ValueError)):
        return "ONE_SHOT_SOURCE_JSON_INVALID"
    if stage == "CONTEXT_GET_JSON" and isinstance(error, (UnicodeError, ValueError)):
        return "ONE_SHOT_EMBED_JSON_INVALID"
    if (isinstance(error, BackgroundError) and isinstance(error.code, str)
            and error.code in _BACKGROUND_CODES):
        return error.code
    if isinstance(error, LockBusy):
        return "BACKGROUND_LOCK_BUSY"
    return "BACKGROUND_" + stage + "_FAILED" if stage in _DIAGNOSTIC_STAGES else "BACKGROUND_UNKNOWN_FAILED"


class _SourceDiagnostic:
    def __init__(self, record, receipt_path):
        self.record, self.receipt_path = record, receipt_path
        self.current = None

    def stage(self, stage):
        if stage not in _DIAGNOSTIC_STAGES:
            raise BackgroundError("BACKGROUND_DIAGNOSTIC_STAGE_INVALID")
        self.record["stage"] = stage
        _save(self.receipt_path, self.record)

    @contextmanager
    def step(self, stage):
        self.stage(stage)
        try:
            yield
        except Exception as error:
            self.failure(stage, error)
            raise

    def begin_request(self, kind, context_index):
        if kind not in ("ACTOR_POST", "CONTEXT_GET") or len(self.record["requests"]) >= 4:
            raise BackgroundError("BACKGROUND_REQUEST_SCOPE_INVALID")
        if kind == "CONTEXT_GET" and (type(context_index) is not int or not 1 <= context_index <= 3):
            raise BackgroundError("BACKGROUND_REQUEST_SCOPE_INVALID")
        self.current = {"request_id": uuid4().hex, "kind": kind,
                        "context_index": context_index if kind == "CONTEXT_GET" else None,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "http_status": None, "state": "STARTED"}
        self.record["requests"].append(self.current)
        self.stage(kind + "_OPEN")

    def http_status(self, status):
        if self.current is not None and type(status) is int and 100 <= status <= 599:
            self.current["http_status"] = status
            self.current["state"] = "HTTP_RETURNED"
            _save(self.receipt_path, self.record)

    def json_parsed(self):
        if self.current is not None:
            self.current["state"] = "JSON_PARSED"
            _save(self.receipt_path, self.record)

    def request_validated(self):
        if self.current is not None:
            self.current["state"] = "VALIDATED"
            self.current["completed_at"] = datetime.now(timezone.utc).isoformat()
            _save(self.receipt_path, self.record)

    def failure(self, stage, error):
        if "source_error_code" not in self.record:
            self.record["source_error_code"] = _error_code(stage, error)
            self.record["source_exception_class"] = _exception_class(error)
            self.record["source_failure_stage"] = stage
            self.record["stage"] = stage
        if self.current is not None and self.current["state"] != "VALIDATED":
            if isinstance(error, HTTPError) and type(error.code) is int and 100 <= error.code <= 599:
                self.current["http_status"] = error.code
            self.current["state"] = "FAILED"
        try:
            _save(self.receipt_path, self.record)
        except OSError:
            # Preserve the original provider failure. A broken receipt store
            # cannot promise durable diagnostics and is handled by the caller.
            pass


class BackgroundError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def directory():
    return manual._directory().parent / "background"


def due_slot(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise BackgroundError("BACKGROUND_CLOCK_INVALID")
    local = now.astimezone(BEIJING)
    if (local.hour, local.minute) not in SLOTS:
        return None
    return "scheduled-" + local.strftime("%Y%m%d-%H%M")


def _state(path):
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        ids = value["seen_ids"]
        if (not isinstance(value, dict) or set(value) != {"schema_version", "seen_ids"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(ids, list) or len(ids) > 100000
                or any(not isinstance(item, str) or _ID.fullmatch(item) is None for item in ids)
                or len(ids) != len(set(ids))):
            raise ValueError()
        return set(ids)
    except (OSError, ValueError, TypeError, KeyError):
        raise BackgroundError("BACKGROUND_STATE_REVIEW_REQUIRED") from None


def _save(path, value):
    batch._save(path, value)


def _successful_manual_ids(root):
    """Seed deduplication from completed manual runs already paid for by the user."""
    ids = set()
    for receipt_path in root.glob("manual-test-*.receipt.json"):
        result_path = receipt_path.with_name(receipt_path.name.replace(".receipt.json", ".result.json"))
        if not result_path.is_file():
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("status") not in ("FULL_CHAIN_OK", "FULL_CHAIN_EMPTY"):
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("mode") != "MANUAL" or result.get("plan_id") != receipt.get("plan_id"):
                continue
            tweets = result.get("tweets")
            if not isinstance(tweets, list) or len(tweets) > 3:
                continue
            for tweet in tweets:
                identifier = tweet.get("id") if isinstance(tweet, dict) else None
                if isinstance(identifier, str) and _ID.fullmatch(identifier):
                    ids.add(identifier)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return ids


def _readable_receipt(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        summary = {key: value[key] for key in ("run_id", "status", "captured_items",
                                               "new_items", "analyzed_items", "relevant_items", "smtp_status")}
        for key in ("recovered_run_id", "reused_analysis_items", "source_status",
                    "stage", "error_code", "exception_class", "source_error_code",
                    "source_exception_class", "source_failure_stage", "requests", "persistence"):
            if key in value:
                summary[key] = value[key]
        return summary
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "BACKGROUND_RECEIPT_UNAVAILABLE"}


def _journals(root):
    """Read work before the source call; legacy v1 rows remain review-only."""
    jobs, reserved = [], set()
    for path in sorted(root.glob("scheduled-*.result.json")):
        try:
            if path.is_symlink() or _JOURNAL_NAME.fullmatch(path.name) is None:
                raise ValueError()
            value = json.loads(path.read_text(encoding="utf-8"))
            run_id = path.name.removesuffix(".result.json")
            tweets = value["tweets"]
            if (not isinstance(value, dict) or value.get("run_id") != run_id
                    or type(value.get("schema_version")) is not int
                    or value["schema_version"] not in (1, 2)
                    or not isinstance(tweets, list) or len(tweets) > 3
                    or any(not isinstance(tweet, dict) or not isinstance(tweet.get("id"), str)
                           or _ID.fullmatch(tweet["id"]) is None for tweet in tweets)):
                raise ValueError()
            ids = [tweet["id"] for tweet in tweets]
            if len(ids) != len(set(ids)) or reserved.intersection(ids):
                raise ValueError()
            reserved.update(ids)
            if value["schema_version"] == 1:
                continue
            status = value.get("status")
            if status not in _JOURNAL_STATES or (not tweets and status != "NO_NEW_ITEMS"):
                raise ValueError()
            if (type(value.get("model_attempts", 0)) is not int
                    or type(value.get("smtp_attempts", 0)) is not int
                    or not 0 <= value.get("model_attempts", 0) <= 2
                    or not 0 <= value.get("smtp_attempts", 0) <= 2):
                raise ValueError()
            if status in ("ANALYZED", "SMTP_ATTEMPTED", "SMTP_FAILED",
                          "SMTP_UNKNOWN", "SENT", "NO_RELEVANT_ITEMS",
                          "SMTP_REVIEW_REQUIRED"):
                results = value.get("results")
                if (not isinstance(results, list) or len(results) != len(tweets)
                        or {row.get("id") for row in results if isinstance(row, dict)} != set(ids)
                        or any(not isinstance(row, dict) or row.get("label") not in _LABELS
                               for row in results)):
                    raise ValueError()
                notification = value.get("notification")
                if notification is not None and (not isinstance(notification, dict)
                        or notification.get("recipient") != RECIPIENT
                        or notification.get("message_id") != "<" + run_id + "@qq.com>"
                        or not isinstance(notification.get("body"), str)):
                    raise ValueError()
                if status in ("SMTP_ATTEMPTED", "SMTP_FAILED", "SMTP_UNKNOWN",
                              "SENT", "SMTP_REVIEW_REQUIRED") and notification is None:
                    raise ValueError()
            exhausted_model = (status in ("MODEL_ATTEMPTED", "MODEL_FAILED")
                               and value.get("model_attempts", 0) >= 2)
            exhausted_mail = (status == "SMTP_FAILED" and value.get("smtp_attempts", 0) >= 2)
            if status in _RECOVERABLE and not (exhausted_model or exhausted_mail):
                jobs.append((path, value))
        except (OSError, ValueError, TypeError, KeyError):
            raise BackgroundError("BACKGROUND_JOURNAL_REVIEW_REQUIRED") from None
    return jobs, reserved


def _checkpoint(path, job, secrets):
    _save(path, _safe(job, secrets))


def _reconcile_completed_ids(jobs, completed_ids, secrets):
    """A successful manual run wins over any older recoverable background work."""
    remaining = []
    for path, job in jobs:
        overlapping = {tweet["id"] for tweet in job["tweets"]} & completed_ids
        if not overlapping:
            remaining.append((path, job))
            continue
        if len(overlapping) == len(job["tweets"]):
            job["status"] = "SUPERSEDED_BY_MANUAL"
        else:
            job["tweets"] = [tweet for tweet in job["tweets"] if tweet["id"] not in overlapping]
            if "results" in job:
                job["results"] = [row for row in job["results"] if row["id"] not in overlapping]
                # An analyzed or definitively failed mail has not been delivered.
                # Rebuild its body for the remaining IDs without another model call.
                job["notification"] = None
                job["status"] = "ANALYZED"
            remaining.append((path, job))
        job["manual_suppressed_ids"] = sorted(
            set(job.get("manual_suppressed_ids", [])) | overlapping, key=int)
        _checkpoint(path, job, secrets)
    return remaining


def _drive_job(path, job, now, credentials, transport, smtp_factory, record, seen,
               state_path, secrets, diagnostic):
    tweets = job["tweets"]
    if job["status"] in ("SOURCE_READY", "MODEL_ATTEMPTED", "MODEL_FAILED"):
        packed, mapping = batch._pack(tweets)
        job["model_attempts"] = job.get("model_attempts", 0) + 1
        job["status"] = "MODEL_ATTEMPTED"
        _checkpoint(path, job, secrets)
        evidence = {}
        try:
            diagnostic.stage("MODEL_REQUEST")
            analyzed = batch._analyze(
                credentials.deepseek_api_key, _body(tweets, batch._now(lambda: now)),
                transport, evidence, {}, input_ids=[row["id"] for row in packed],
                model_budget=manual._NoLedger())
            results = batch._restore_ids(analyzed, mapping)
            _safe([results, evidence], secrets)
            job.update(status="ANALYZED", results=results, analysis=evidence,
                       analyzed_at=now.isoformat(), notification=None)
            diagnostic.stage("MODEL_RESULT_WRITE")
            _checkpoint(path, job, secrets)
            record["persistence"]["analysis_journal_written"] = True
            _save(diagnostic.receipt_path, record)
        except Exception:
            job["status"] = ("MODEL_REVIEW_REQUIRED" if job["model_attempts"] >= 2
                             else "MODEL_FAILED")
            _checkpoint(path, job, secrets)
            raise
        record["analyzed_items"] = len(results)
    else:
        results = job["results"]
        record["reused_analysis_items"] = len(results)

    relevant_ids = {row["id"] for row in results if row["label"] in _RESULTS}
    relevant_tweets = [tweet for tweet in tweets if tweet["id"] in relevant_ids]
    relevant_results = [row for row in results if row["id"] in relevant_ids]
    record["relevant_items"] = len(relevant_results)
    if not relevant_tweets:
        job["status"] = "NO_RELEVANT_ITEMS"
        _checkpoint(path, job, secrets)
        record["status"] = "NO_RELEVANT_ITEMS"
    else:
        notification = job.get("notification")
        if notification is None:
            diagnostic.stage("NOTIFICATION_BUILD")
            analyzed_at = datetime.fromisoformat(job["analyzed_at"])
            notification = _notification(relevant_tweets, relevant_results, job["analysis"],
                                         analyzed_at, job["run_id"], background=True)
            _safe(notification, secrets)
            job["notification"] = notification
            _checkpoint(path, job, secrets)
        job["smtp_attempts"] = job.get("smtp_attempts", 0) + 1
        job["status"] = "SMTP_ATTEMPTED"
        _checkpoint(path, job, secrets)
        diagnostic.stage("SMTP_SEND")
        notifier = SMTPNotifier(SMTPConfig(
            host=manual.SMTP_HOST, port=manual.SMTP_PORT, security="SSL",
            sender=SENDER, user=SENDER, recipient=RECIPIENT,
            password=credentials.smtp_password, auth_mechanism="LOGIN",
            timeout_seconds=20), smtp_factory=smtp_factory or stdlib_smtp_factory)
        delivery = notifier.send(notification)
        record["smtp_status"] = delivery.status
        if delivery.status == "SENT":
            job["status"] = "SENT"
            record["status"] = "SENT"
        elif delivery.status == "FAILED":
            job["status"] = ("SMTP_REVIEW_REQUIRED" if job["smtp_attempts"] >= 2
                             else "SMTP_FAILED")
            record["status"] = "FAILED"
        else:
            job["status"] = "SMTP_UNKNOWN"
            record["status"] = "UNKNOWN"
        if delivery.error_code:
            safe_error = (delivery.error_code if isinstance(delivery.error_code, str)
                          and delivery.error_code in _SMTP_CODES else "BACKGROUND_SMTP_SEND_FAILED")
            record["error_code"] = safe_error
            job["smtp_error_code"] = safe_error
        diagnostic.stage("DELIVERY_JOURNAL_WRITE")
        _checkpoint(path, job, secrets)
        record["persistence"]["delivery_journal_written"] = True
        _save(diagnostic.receipt_path, record)

    if job["status"] in ("SENT", "NO_RELEVANT_ITEMS", "SMTP_UNKNOWN"):
        seen.update(tweet["id"] for tweet in tweets)
        diagnostic.stage("SEEN_WRITE")
        _save(state_path, {"schema_version": 1, "seen_ids": sorted(seen, key=int)})
        record["persistence"]["seen_written"] = True
        _save(diagnostic.receipt_path, record)


def run_slot(now, *, root=None, credential_loader=None, source_fetch=None,
             transport=None, smtp_factory=None, chain_lock=None, manual_results_root=None):
    """Run one due slot or return without any external IO outside its minute."""
    run_id = due_slot(now)
    if run_id is None:
        return {"status": "NOT_DUE"}
    try:
        root = directory() if root is None else Path(root)
        root.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        return {"run_id": run_id, "status": "FAILED", "stage": "ROOT_SETUP",
                "error_code": _error_code("ROOT_SETUP", error),
                "exception_class": _exception_class(error)}
    # Keep readers away from every receipt replacement until this slot finishes.
    # The shared chain lock below still serializes different slots and manual runs.
    slot_lock = ProcessLock(root / (run_id + ".slot.lock"))
    try:
        slot_lock.acquire()
    except LockBusy:
        return {"run_id": run_id, "status": "BUSY", "stage": "SLOT_LOCK",
                "error_code": "BACKGROUND_LOCK_BUSY", "exception_class": "LOCK_BUSY"}
    except Exception as error:
        return {"run_id": run_id, "status": "FAILED", "stage": "SLOT_LOCK",
                "error_code": _error_code("SLOT_LOCK", error),
                "exception_class": _exception_class(error)}
    try:
        return _run_slot_locked(now, run_id, root, credential_loader, source_fetch,
                                transport, smtp_factory, chain_lock, manual_results_root)
    finally:
        slot_lock.release()


def _run_slot_locked(now, run_id, root, credential_loader, source_fetch,
                     transport, smtp_factory, chain_lock, manual_results_root):
    receipt_path = root / (run_id + ".receipt.json")
    if receipt_path.exists():
        return _readable_receipt(receipt_path)
    claim = root / (run_id + ".attempt.json")
    try:
        with claim.open("x", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "run_id": run_id,
                       "claimed_at": now.astimezone(timezone.utc).isoformat()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return {"run_id": run_id, "status": "ALREADY_ATTEMPTED"}
    except Exception as error:
        return {"run_id": run_id, "status": "FAILED", "stage": "CLAIM_WRITE",
                "error_code": _error_code("CLAIM_WRITE", error),
                "exception_class": _exception_class(error)}
    record = {"schema_version": 1, "run_id": run_id, "status": "RUNNING",
              "captured_items": 0, "new_items": 0, "analyzed_items": 0,
              "relevant_items": 0, "smtp_status": "NOT_SENT",
              "stage": "RECEIPT_INIT", "requests": [],
              "persistence": {"receipt_initialized": False,
                              "source_journal_written": False,
                              "analysis_journal_written": False,
                              "delivery_journal_written": False,
                              "seen_written": False}}
    try:
        _save(receipt_path, record)
        record["persistence"]["receipt_initialized"] = True
        _save(receipt_path, record)
    except Exception as error:
        return {"run_id": run_id, "status": "FAILED", "stage": "RECEIPT_INIT",
                "error_code": _error_code("RECEIPT_INIT", error),
                "exception_class": _exception_class(error)}
    diagnostic = _SourceDiagnostic(record, receipt_path)
    state_path = root / "seen-v1.json"
    try:
        diagnostic.stage("LOCK_ACQUIRE")
        chain_lock = manual._directory() / "manual-test.lock" if chain_lock is None else Path(chain_lock)
        with ProcessLock(chain_lock):
            diagnostic.stage("SEEN_LOAD")
            seen = _state(state_path)
            diagnostic.stage("JOURNAL_SCAN")
            jobs, reserved = _journals(root)
            manual_results_root = manual._directory() if manual_results_root is None else Path(manual_results_root)
            diagnostic.stage("MANUAL_SEED")
            seen.update(_successful_manual_ids(manual_results_root))
            load = load_credentials if credential_loader is None else credential_loader
            diagnostic.stage("CREDENTIAL_LOAD")
            credentials = load()
            diagnostic.stage("CREDENTIAL_VALIDATE")
            if (not isinstance(credentials, ProjectCredentials) or credentials.smtp_user != SENDER
                    or not _valid_secret(credentials.apify_token)
                    or not _valid_secret(credentials.deepseek_api_key)):
                raise BackgroundError("BACKGROUND_CREDENTIAL_INVALID")
            secrets = (credentials.apify_token, credentials.deepseek_api_key, credentials.smtp_password)
            diagnostic.stage("PENDING_RECONCILE")
            jobs = _reconcile_completed_ids(jobs, seen, secrets)
            resume = jobs[0] if jobs else None
            diagnostic.stage("TRANSPORT_BUILD")
            transport = _build_transport() if transport is None else transport
            fetch = (lambda token, wire, clock: fetch_three(token, wire, clock,
                       diagnostic=diagnostic)) if source_fetch is None else source_fetch
            current_job = None
            try:
                diagnostic.stage("SOURCE_FETCH")
                tweets, source = fetch(credentials.apify_token, transport, lambda: now)
                diagnostic.stage("SOURCE_VALIDATE")
                _safe([tweets, source], secrets)
                if (not isinstance(tweets, list) or len(tweets) > 3
                        or any(not isinstance(tweet, dict) or not isinstance(tweet.get("id"), str)
                               or _ID.fullmatch(tweet["id"]) is None for tweet in tweets)
                        or len({tweet["id"] for tweet in tweets}) != len(tweets)):
                    raise BackgroundError("BACKGROUND_SOURCE_INVALID")
                pending = [tweet for tweet in tweets if tweet["id"] not in seen
                           and tweet["id"] not in reserved]
                record.update(captured_items=len(tweets), new_items=len(pending))
                current_job = {"schema_version": 2, "run_id": run_id,
                               "status": "SOURCE_READY" if pending else "NO_NEW_ITEMS",
                               "tweets": pending, "source": source}
                current_path = root / (run_id + ".result.json")
                diagnostic.stage("SOURCE_JOURNAL_WRITE")
                _checkpoint(current_path, current_job, secrets)
                record["persistence"]["source_journal_written"] = True
                _save(receipt_path, record)
            except Exception as error:
                if "source_error_code" not in record:
                    record["source_error_code"] = _error_code(record["stage"], error)
                    record["source_exception_class"] = _exception_class(error)
                    record["source_failure_stage"] = record["stage"]
                    _save(receipt_path, record)
                if resume is None:
                    raise
                record["source_status"] = "FAILED"
            target = resume if resume is not None else ((current_path, current_job) if current_job and pending else None)
            if target is None:
                record["status"] = "NO_NEW_ITEMS"
            else:
                path, job = target
                if resume is not None:
                    record["recovered_run_id"] = job["run_id"]
                diagnostic.stage("RECOVERY_PROCESS")
                _drive_job(path, job, now, credentials, transport, smtp_factory,
                           record, seen, state_path, secrets, diagnostic)
    except LockBusy:
        record.update(status="BUSY", error_code="BACKGROUND_LOCK_BUSY",
                      exception_class="LOCK_BUSY")
    except BackgroundError as error:
        record.update(status="FAILED", error_code=_error_code(record["stage"], error),
                      exception_class=_exception_class(error))
    except Exception as error:
        # Never log provider bodies, URL query, exception text or credentials.
        same_source_failure = (record.get("source_failure_stage") == record["stage"])
        record.update(status="UNKNOWN",
                      error_code=(record["source_error_code"] if same_source_failure
                                  else _error_code(record["stage"], error)),
                      exception_class=(record["source_exception_class"] if same_source_failure
                                       else _exception_class(error)))
    if record["status"] not in ("FAILED", "UNKNOWN", "BUSY") and "source_error_code" not in record:
        record["stage"] = "COMPLETE"
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _save(receipt_path, record)
    except Exception as error:
        return {"run_id": run_id, "status": "FAILED", "stage": "RECEIPT_FINAL",
                "error_code": "BACKGROUND_RECEIPT_FINAL_FAILED",
                "exception_class": _exception_class(error)}
    return _readable_receipt(receipt_path)


def serve(*, clock=None, sleep=None, root=None):
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    root = directory() if root is None else Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        with ProcessLock(root / "daemon.lock"):
            started = clock().astimezone(timezone.utc).isoformat()
            last_slot = None
            while True:
                now = clock()
                outcome = run_slot(now, root=root)
                if outcome.get("status") != "NOT_DUE":
                    last_slot = outcome
                _save(root / "status.json", {"schema_version": 1, "state": "RUNNING",
                    "pid": os.getpid(), "started_at": started,
                    "heartbeat_at": now.astimezone(timezone.utc).isoformat(),
                    "last_slot": last_slot})
                sleep(20)
    except LockBusy:
        return 2
    except Exception:
        return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--preview"]:
        print(json.dumps({"mode": "BACKGROUND", "timezone": "Asia/Shanghai",
                          "times": ["08:00", "13:00", "20:00"],
                          "fetch_limit": 3, "autostart": "Windows interactive logon",
                          "provider_calls": 0}, ensure_ascii=False))
        return 0
    if argv == ["--serve"]:
        return serve()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
