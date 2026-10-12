"""Strict JSON snapshots with validated backups and explicit recovery holds.

The caller holds ProcessLock around the full load/modify/save transaction.
Recovery can roll back classification and billing evidence. A restored snapshot
must remain globally paused until an operator has reconciled that uncertainty.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from .models import FetchSnapshot, SourceContractError, parse_aware_iso, validate_snapshot


class StateError(RuntimeError):
    """State cannot be safely read or committed."""


LABELS = {"PRE_RESET", "POST_RESET", "POSSIBLE_RESET", "IRRELEVANT"}
NOTIFICATION_STATUSES = {"PENDING", "SENDING", "SENT", "FAILED", "UNKNOWN"}
COLLECTIONS = ("inbox", "processed_ids", "notified_ids", "pending_notifications", "failed_items")


def initial_state(target: str, *, execution_mode="OFFLINE", target_user_id=None) -> dict:
    return {
        "schema_version": 3,
        "revision": 0,
        "target": target.lstrip("@").lower(),
        "target_user_id": target_user_id,
        "execution_mode": execution_mode,
        "active_fetch": None,
        "last_completed_fetch": None,
        "coverage_mode": "LATEST_3",
        "initialized": False,
        "initial_baseline_id": None,
        "last_seen_id": None,
        "last_fetch_at": None,
        "last_success_at": None,
        "inbox": {},
        "processed_ids": {},
        "notified_ids": {},
        "pending_notifications": {},
        "failed_items": {},
        "health": {
            "consecutive_failures": 0,
            "last_error": None,
            "incident_id": None,
            "recovered_at": None,
        },
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StateError(message)


def _integer(value, minimum=0) -> bool:
    return type(value) is int and value >= minimum


def _tweet_id(value) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
        and int(value) <= (1 << 64) - 1
    )


def _text_or_none(value) -> bool:
    return value is None or isinstance(value, str)


def _timestamp(value, nullable=True) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        parse_aware_iso(value)
        return True
    except (ValueError, TypeError, OverflowError):
        return False


def _validate_tweet(tweet, identifier, target, target_user_id=None) -> None:
    _require(isinstance(tweet, dict), "Tweet data must be an object")
    _require(tweet.get("id") == identifier, "Tweet ID must match its state key")
    try:
        validate_snapshot(FetchSnapshot([tweet]), target, limit=3, target_user_id=target_user_id)
    except (SourceContractError, ValueError, TypeError, AttributeError) as exc:
        raise StateError("Persisted tweet is incomplete or violates the source contract") from exc


def _validate_classification(result) -> None:
    _require(isinstance(result, dict), "Classification must be an object")
    _require(isinstance(result.get("label"), str) and result["label"] in LABELS, "Classification label is invalid")
    if "confidence" in result:
        confidence = result["confidence"]
        _require(
            type(confidence) in (int, float) and 0 <= confidence <= 1 and math.isfinite(confidence),
            "Classification confidence is invalid",
        )
    _require("reset_time" in result and _timestamp(result["reset_time"]), "Reset time must include a timezone or be null")
    _require(isinstance(result.get("reason"), str) and bool(result["reason"].strip()), "Classification reason is missing")


def _validate_notification_key(key) -> None:
    _require(isinstance(key, str) and ":" in key, "Notification key is invalid")
    prefix, suffix = key.split(":", 1)
    _require(
        (prefix == "health" and bool(suffix)) or (_tweet_id(prefix) and suffix in LABELS),
        "Notification key is invalid",
    )


def validate_state(state: dict) -> None:
    """Reject corrupt, inconsistent, or unsupported snapshots without repair."""
    _require(isinstance(state, dict), "State must be an object")
    _require(type(state.get("schema_version")) is int and state["schema_version"] == 3, "Unsupported state schema")
    _require(_integer(state.get("revision")), "State revision is invalid")
    _require(isinstance(state.get("target"), str) and bool(state["target"].strip()), "State target is missing")
    _require(re.fullmatch(r"[a-z0-9_]{1,15}", state["target"]) is not None, "State target must be canonical")
    _require("target_user_id" in state and (state["target_user_id"] is None or _tweet_id(state["target_user_id"])), "State author binding is invalid")
    _require(state.get("execution_mode") in ("OFFLINE", "LIVE"), "State execution origin is invalid")
    _require("active_fetch" in state, "Active fetch marker is missing")
    if state["active_fetch"] is not None:
        from .apify import validate_active_fetch
        try:
            validate_active_fetch(state["active_fetch"])
        except (ValueError, TypeError, KeyError) as exc:
            raise StateError("Active fetch is invalid") from exc
        _require(state["active_fetch"]["target"] == state["target"], "Active fetch target is inconsistent")
        if state["target_user_id"] is not None:
            _require(state["active_fetch"].get("target_user_id") == state["target_user_id"], "Active fetch author binding is inconsistent")
    if state.get("last_completed_fetch") is not None:
        from .apify import validate_active_fetch
        try:
            validate_active_fetch(state["last_completed_fetch"])
        except (ValueError, TypeError, KeyError) as exc:
            raise StateError("Completed fetch receipt is invalid") from exc
        _require(state["last_completed_fetch"]["phase"] == "READY"
                 and state["last_completed_fetch"]["target"] == state["target"], "Completed fetch receipt is inconsistent")
        if state["target_user_id"] is not None:
            _require(state["last_completed_fetch"].get("target_user_id") == state["target_user_id"], "Completed fetch author binding is inconsistent")
    _require(state.get("coverage_mode") == "LATEST_3", "Unsupported coverage mode")
    _require(type(state.get("initialized")) is bool, "Initialization marker is invalid")
    for field in ("initial_baseline_id", "last_seen_id"):
        _require(field in state and (state[field] is None or _tweet_id(state[field])), "State cursor must be a safe decimal Tweet ID")
    if state["initialized"]:
        _require(state["initial_baseline_id"] is not None and state["last_seen_id"] is not None, "Initialized state requires both cursors")
        _require(int(state["last_seen_id"]) >= int(state["initial_baseline_id"]), "High watermark precedes the initial baseline")
    else:
        _require(state["initial_baseline_id"] is None and state["last_seen_id"] is None, "Uninitialized state cannot have cursors")
    for field in ("last_fetch_at", "last_success_at"):
        _require(field in state and _timestamp(state[field]), "State time must include a timezone or be null")
    for field in ("recovered_from_backup", "recovery_required"):
        if field in state:
            _require(type(state[field]) is bool, "Recovery marker is invalid")
    for name in COLLECTIONS:
        _require(isinstance(state.get(name), dict), "State collections must be objects")

    for identifier, item in state["inbox"].items():
        _require(_tweet_id(identifier) and isinstance(item, dict), "Inbox entry is invalid")
        _validate_tweet(item.get("tweet"), identifier, state["target"], state["target_user_id"])
        _require(_integer(item.get("attempts")), "Inbox attempt count is invalid")
        _require("next_retry_at" in item and _timestamp(item["next_retry_at"]), "Inbox retry time is invalid")
        _require("last_error" in item and _text_or_none(item["last_error"]), "Inbox error is invalid")
    for identifier, item in state["processed_ids"].items():
        _require(_tweet_id(identifier) and isinstance(item, dict), "Processed entry is invalid")
        _validate_tweet(item.get("tweet"), identifier, state["target"], state["target_user_id"])
        _validate_classification(item.get("classification"))
        _require(_timestamp(item.get("processed_at"), False), "Processed time is invalid")
        _require(isinstance(item.get("prompt_version"), str), "Prompt version is missing")
        _require(isinstance(item.get("evidence_hashes"), list) and all(isinstance(v, str) for v in item["evidence_hashes"]), "Evidence hashes must be a list of text")
        _require(isinstance(item.get("history"), list), "Classification history must be a list")
    for identifier in state["inbox"].keys() & state["processed_ids"].keys():
        upgrade = state["inbox"][identifier].get("upgrade")
        _require(
            isinstance(upgrade, dict)
            and all(isinstance(upgrade.get(name), str) and bool(upgrade[name].strip()) for name in ("hash", "evidence", "basis"))
            and re.fullmatch(r"[0-9a-f]{64}", upgrade["hash"]) is not None
            and state["processed_ids"][identifier]["classification"]["label"] == "POSSIBLE_RESET",
            "Only an explicitly evidenced POSSIBLE_RESET upgrade can overlap processed items",
        )
        _require(upgrade["hash"] not in state["processed_ids"][identifier]["evidence_hashes"], "Upgrade evidence was already processed")
    for identifier, item in state["inbox"].items():
        if "upgrade" in item:
            _require(identifier in state["processed_ids"], "Upgrade requires a previous classification")

    notifications = state["pending_notifications"]
    for key, item in notifications.items():
        _validate_notification_key(key)
        _require(isinstance(item, dict) and item.get("key") == key, "Notification record key is inconsistent")
        _require(isinstance(item.get("status"), str) and item["status"] in NOTIFICATION_STATUSES, "Notification status is invalid")
        if key.startswith("health:"):
            _require(item.get("tweet_id") is None and item.get("label") == "HEALTH", "Health notification identity is invalid")
        else:
            identifier, label = key.split(":", 1)
            _require(item.get("tweet_id") == identifier and item.get("label") == label, "Tweet notification identity is inconsistent")
        for name in ("recipient", "subject", "body", "message_id"):
            _require(isinstance(item.get(name), str) and bool(item[name]), "Notification snapshot is incomplete")
        for name in ("attempts", "failure_count"):
            _require(_integer(item.get(name)), "Notification attempt count is invalid")
        _require(type(item.get("unknown_retry_used")) is int and item["unknown_retry_used"] in (0, 1), "UNKNOWN retry budget is invalid")
        for name in ("retryable", "needs_attention", "had_unknown"):
            _require(type(item.get(name)) is bool, "Notification retry marker is invalid")
        _require(_timestamp(item.get("created_at"), False), "Notification creation time is invalid")
        for name in ("next_retry_at", "sent_at", "uncertain_since"):
            _require(name in item and _timestamp(item[name]), "Notification time is invalid")
        _require("last_error" in item and _text_or_none(item["last_error"]), "Notification error is invalid")
        if item["unknown_retry_used"]:
            _require(item["had_unknown"], "An UNKNOWN retry requires prior uncertainty")
            if item["status"] not in ("SENDING", "SENT"):
                _require(item["needs_attention"], "An exhausted UNKNOWN retry must pause automatic delivery")
        if item["status"] == "UNKNOWN":
            _require(item["had_unknown"] and _timestamp(item["uncertain_since"], False), "UNKNOWN requires recorded uncertainty")
            if not item["needs_attention"]:
                _require(_timestamp(item["next_retry_at"], False), "Automatic UNKNOWN recovery requires a retry deadline")
                _require(
                    parse_aware_iso(item["next_retry_at"]) >= parse_aware_iso(item["uncertain_since"]),
                    "UNKNOWN retry deadline precedes uncertainty",
                )
        if item["status"] == "SENT":
            _require(item["sent_at"] is not None and key in state["notified_ids"], "SENT requires a matching notification ledger entry")
        else:
            _require(item["sent_at"] is None and key not in state["notified_ids"], "Unsent notification conflicts with the sent ledger")

    for key, record in state["notified_ids"].items():
        _validate_notification_key(key)
        _require(isinstance(record, dict), "Sent ledger record is invalid")
        _require(_timestamp(record.get("sent_at"), False) and isinstance(record.get("message_id"), str), "Sent ledger evidence is missing")
        _require(key in notifications and notifications[key]["status"] == "SENT", "Sent ledger requires a SENT outbox record")
        _require(record["sent_at"] == notifications[key]["sent_at"] and record["message_id"] == notifications[key]["message_id"], "Sent ledger disagrees with its outbox record")
    for key, item in state["failed_items"].items():
        _require(isinstance(key, str) and bool(key) and isinstance(item, dict), "Failure record is invalid")
        for name in ("stage", "error_code"):
            _require(isinstance(item.get(name), str) and bool(item[name]), "Failure stage or code is missing")
        _require(type(item.get("retryable")) is bool, "Failure retry marker is invalid")
        _require("next_retry_at" in item and _timestamp(item["next_retry_at"]), "Failure retry time is invalid")
    health = state.get("health")
    _require(isinstance(health, dict), "Health record must be an object")
    _require(_integer(health.get("consecutive_failures")), "Failure count is invalid")
    for name in ("last_error", "incident_id"):
        _require(name in health and _text_or_none(health[name]), "Health error or incident is invalid")
    _require("recovered_at" in health and _timestamp(health["recovered_at"]), "Health recovery time is invalid")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _decode(data: bytes) -> dict:
    def invalid_constant(_value):
        raise StateError("JSON contains a non-finite number")
    try:
        state = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=invalid_constant)
        if isinstance(state, dict) and type(state.get("schema_version")) is int and state["schema_version"] == 1:
            _require(state.get("active_fetch") is None, "Legacy state cannot contain a paid fetch")
            state["schema_version"] = 2
            state["active_fetch"] = None
            state["last_completed_fetch"] = None
            state["execution_mode"] = "OFFLINE"
            if isinstance(state.get("target"), str):
                state["target"] = state["target"].lstrip("@").lower()
        # Early schema2 files were exclusively offline development fixtures.
        # A missing origin may never be interpreted as LIVE.
        if isinstance(state, dict) and state.get("schema_version") == 2 and "execution_mode" not in state:
            state["execution_mode"] = "OFFLINE"
        if isinstance(state, dict) and type(state.get("schema_version")) is int and state["schema_version"] == 2:
            # Never infer a stable identity for a historical LIVE baseline.
            state["schema_version"] = 3
            state["target_user_id"] = None
        validate_state(state)
        return state
    except (UnicodeError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise StateError("State JSON or schema is invalid") from exc


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self.recovered_from_backup = False
        self._recovery_source = None

    def load(self, recover=False):
        self.recovered_from_backup = False
        self._recovery_source = None
        if not self.path.exists() and not self.backup_path.exists():
            return None
        try:
            return _decode(self.path.read_bytes())
        except (OSError, StateError) as exc:
            if not recover:
                raise StateError("Main state is missing or invalid; explicit recovery is required") from exc
        try:
            backup_data = self.backup_path.read_bytes()
            state = _decode(backup_data)
        except (OSError, StateError) as exc:
            raise StateError("No validated state backup is available; monitoring must stop") from exc
        state = deepcopy(state)
        now = datetime.now(timezone.utc).isoformat()
        for item in state["pending_notifications"].values():
            if item["status"] != "SENT":
                item.update(
                    status="UNKNOWN", needs_attention=True, unknown_retry_used=1,
                    had_unknown=True, retryable=False, next_retry_at=None,
                    uncertain_since=item.get("uncertain_since") or now,
                    last_error="STATE_RECOVERED_FROM_BACKUP",
                )
        state["recovered_from_backup"] = True
        state["recovery_required"] = True
        state["health"]["last_error"] = "STATE_RECOVERED_FROM_BACKUP"
        validate_state(state)
        self._recovery_source = backup_data
        self.recovered_from_backup = True
        return state

    def _temporary(self, data: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def save(self, state: dict) -> None:
        validate_state(state)
        candidate = dict(state)
        candidate["revision"] += 1
        try:
            serialized = (json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise StateError("State contains unsupported JSON data") from exc

        previous_data = None
        corrupt_data = None
        try:
            if self.path.exists():
                previous_data = self.path.read_bytes()
                try:
                    previous = _decode(previous_data)
                except StateError:
                    if self._recovery_source is None or not state.get("recovery_required"):
                        raise StateError("Refusing to overwrite invalid state without explicit recovery")
                    corrupt_data, previous_data = previous_data, None
                    previous = _decode(self._recovery_source)
                _require(previous["revision"] == state["revision"], "State revision changed since loading")
            elif self.backup_path.exists():
                _require(self._recovery_source is not None and state.get("recovery_required") is True, "Missing main state requires explicit recovery")
                _require(_decode(self._recovery_source)["revision"] == state["revision"], "Recovery revision is inconsistent")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            state_temporary = self._temporary(serialized)
            try:
                if corrupt_data is not None:
                    archive = self.path.with_name(self.path.name + ".corrupt-" + uuid.uuid4().hex)
                    with archive.open("xb") as handle:
                        handle.write(corrupt_data)
                        handle.flush()
                        os.fsync(handle.fileno())
                if previous_data is not None:
                    backup_temporary = self._temporary(previous_data)
                    try:
                        os.replace(backup_temporary, self.backup_path)
                    finally:
                        backup_temporary.unlink(missing_ok=True)
                os.replace(state_temporary, self.path)
            finally:
                state_temporary.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError) as exc:
            raise StateError("State could not be committed safely") from exc
        state["revision"] = candidate["revision"]
        self._recovery_source = None
