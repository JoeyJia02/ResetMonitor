"""Persistent per-user background monitor for the three Beijing time slots.

The Windows logon task starts this process with pythonw. Each slot is claimed
durably before any provider call. A slot is never retried automatically, even
after a crash or an uncertain SMTP outcome. Source rows already seen in prior
slots are not sent to the model again; only relevant model labels produce mail.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.credential_store import load_credentials
from monitor.credentials import ProjectCredentials
from monitor.locking import LockBusy, ProcessLock
from monitor.smtp_transport import SMTPConfig, SMTPNotifier, stdlib_smtp_factory
from scripts import analyze_recovered_three as batch
from scripts import manual_test as manual
from scripts.one_shot_source import OneShotSourceError, PUBLIC_ERROR_CODES, fetch_three
from scripts.test_full_chain_once import RECIPIENT, SENDER, _body, _notification, _safe
from scripts.verify_api_connections import _build_transport, _valid_secret


BEIJING = timezone(timedelta(hours=8))
SLOTS = ((8, 0), (13, 0), (20, 0))
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_RESULTS = frozenset({"POST_RESET", "PRE_RESET", "POSSIBLE_RESET"})
CODE_VERSION = "20261012-long-note-fix"


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
        summary.update({key: value[key] for key in ("stage", "error_code") if key in value})
        return summary
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "BACKGROUND_RECEIPT_UNAVAILABLE"}


def run_slot(now, *, root=None, credential_loader=None, source_fetch=None,
             transport=None, smtp_factory=None, chain_lock=None, manual_results_root=None):
    """Run one due slot or return without any external IO outside its minute."""
    run_id = due_slot(now)
    if run_id is None:
        return {"status": "NOT_DUE"}
    root = directory() if root is None else Path(root)
    root.mkdir(parents=True, exist_ok=True)
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
    record = {"schema_version": 1, "run_id": run_id, "status": "RUNNING",
              "captured_items": 0, "new_items": 0, "analyzed_items": 0,
              "relevant_items": 0, "smtp_status": "NOT_SENT", "stage": "LOCK_ACQUIRE"}
    _save(receipt_path, record)
    state_path = root / "seen-v1.json"
    chain_lock = manual._directory() / "manual-test.lock" if chain_lock is None else Path(chain_lock)
    try:
        with ProcessLock(chain_lock):
            record["stage"] = "SEEN_LOAD"
            seen = _state(state_path)
            manual_results_root = manual._directory() if manual_results_root is None else Path(manual_results_root)
            seen.update(_successful_manual_ids(manual_results_root))
            load = load_credentials if credential_loader is None else credential_loader
            record["stage"] = "CREDENTIAL_LOAD"
            credentials = load()
            if (not isinstance(credentials, ProjectCredentials) or credentials.smtp_user != SENDER
                    or not _valid_secret(credentials.apify_token)
                    or not _valid_secret(credentials.deepseek_api_key)):
                raise BackgroundError("BACKGROUND_CREDENTIAL_INVALID")
            secrets = (credentials.apify_token, credentials.deepseek_api_key, credentials.smtp_password)
            record["stage"] = "TRANSPORT_BUILD"
            transport = _build_transport() if transport is None else transport
            fetch = fetch_three if source_fetch is None else source_fetch
            record["stage"] = "SOURCE_FETCH"
            tweets, source = fetch(credentials.apify_token, transport, lambda: now)
            record["stage"] = "SOURCE_VALIDATE"
            _safe([tweets, source], secrets)
            if (not isinstance(tweets, list) or len(tweets) > 3
                    or any(not isinstance(tweet, dict) or not isinstance(tweet.get("id"), str)
                           or _ID.fullmatch(tweet["id"]) is None for tweet in tweets)
                    or len({tweet["id"] for tweet in tweets}) != len(tweets)):
                raise BackgroundError("BACKGROUND_SOURCE_INVALID")
            pending = [tweet for tweet in tweets if tweet["id"] not in seen]
            record.update(captured_items=len(tweets), new_items=len(pending))
            # Preserve the source before marking it consumed; a crash still
            # leaves local review data without silently re-paying next slot.
            record["stage"] = "SOURCE_SAVE"
            _save(root / (run_id + ".result.json"), _safe({
                "schema_version": 1, "run_id": run_id, "status": "SOURCE_READY",
                "tweets": pending, "source": source}, secrets))
            seen.update(tweet["id"] for tweet in pending)
            record["stage"] = "SEEN_SAVE"
            _save(state_path, {"schema_version": 1, "seen_ids": sorted(seen, key=int)})
            if not pending:
                record["status"] = "NO_NEW_ITEMS"
            else:
                packed, mapping = batch._pack(pending)
                evidence = {}
                record["stage"] = "MODEL_ANALYZE"
                analyzed = batch._analyze(
                    credentials.deepseek_api_key, _body(pending, batch._now(lambda: now)),
                    transport, evidence, {}, input_ids=[row["id"] for row in packed],
                    model_budget=manual._NoLedger())
                results = batch._restore_ids(analyzed, mapping)
                _safe([results, evidence], secrets)
                relevant_ids = {row["id"] for row in results if row["label"] in _RESULTS}
                relevant_tweets = [tweet for tweet in pending if tweet["id"] in relevant_ids]
                relevant_results = [row for row in results if row["id"] in relevant_ids]
                record.update(analyzed_items=len(results), relevant_items=len(relevant_results))
                notification = None
                record["stage"] = "NOTIFICATION_BUILD"
                if relevant_tweets:
                    notification = _notification(relevant_tweets, relevant_results, evidence,
                                                 batch._now(lambda: now), run_id, background=True)
                    _safe(notification, secrets)
                record["stage"] = "ANALYSIS_SAVE"
                _save(root / (run_id + ".result.json"), _safe({
                    "schema_version": 1, "run_id": run_id, "status": "ANALYZED",
                    "tweets": pending, "results": results, "source": source,
                    "analysis": evidence, "notification": notification}, secrets))
                if notification is None:
                    record["status"] = "NO_RELEVANT_ITEMS"
                else:
                    record["stage"] = "SMTP_SEND"
                    notifier = SMTPNotifier(SMTPConfig(
                        host=manual.SMTP_HOST, port=manual.SMTP_PORT, security="SSL",
                        sender=SENDER, user=SENDER, recipient=RECIPIENT,
                        password=credentials.smtp_password, auth_mechanism="LOGIN",
                        timeout_seconds=20), smtp_factory=smtp_factory or stdlib_smtp_factory)
                    delivery = notifier.send(notification)
                    record.update(smtp_status=delivery.status,
                                  status="SENT" if delivery.status == "SENT" else delivery.status)
                    if delivery.error_code:
                        record["error_code"] = delivery.error_code
    except LockBusy:
        record["status"] = "BUSY"
    except BackgroundError as error:
        record.update(status="FAILED", error_code=error.code)
    except OneShotSourceError as error:
        # Only codes defined by the source module may be written; never log the
        # provider's body, URL, credential, or arbitrary exception message.
        code = error.code if isinstance(error.code, str) and error.code in PUBLIC_ERROR_CODES else "BACKGROUND_SOURCE_FAILED"
        record.update(status="FAILED", error_code=code)
    except Exception:
        # No untrusted provider body, exception text, or credential is logged.
        record.update(status="UNKNOWN", error_code="BACKGROUND_" + record["stage"] + "_FAILED")
    if record["status"] in ("SENT", "NO_NEW_ITEMS", "NO_RELEVANT_ITEMS"):
        record["stage"] = "COMPLETE"
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    _save(receipt_path, record)
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
                    "code_version": CODE_VERSION,
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
