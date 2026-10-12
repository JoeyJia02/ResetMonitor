"""Non-secret finite validation authorizations and conservative reservations.

The ledger's absolute path is bound into the non-secret authorization; the
application must use that exact location even when its project root changes.
Every reservation acquires an OS lock and advances a
fsynced witness before replacing the ledger, then returns permission to attempt
external IO. A crash between those commits requires manual reconciliation;
old/missing ledger data must not refund quota. This is a conservative
process-crash and concurrency guard, not tamper-proof storage or proof of
provider prices. Deleting/rolling back both files is outside this guarantee.
"""

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .locking import LockBusy, ProcessLock
from .models import StageFailure, parse_aware_iso


OPERATIONS = ("apify_start", "apify_poll", "apify_dataset", "deepseek_request", "smtp_attempt")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?\Z")
_MAX_MONEY = Decimal("1000000")  # Input sanity only; never a spending approval.
_ROOT_FIELDS = {"schema_version", "approval_id", "batch_id", "approved", "approved_at", "expires_at",
                "target", "state_path", "ledger_path", "recipient", "sender", "source_contract_evidence_id", "deepseek", "apify", "smtp"}
_DS_FIELDS = {"credential_use_authorized", "budget_approved", "model", "currency", "price_snapshot_id", "max_requests", "amount"}
_APIFY_FIELDS = {"credential_use_authorized", "budget_approved", "monetary_bound_verified", "bound_evidence_id", "currency",
                 "max_run_starts", "max_run_polls", "max_dataset_reads", "max_http_attempts", "amount",
                 "start_max_cost", "poll_max_cost", "dataset_max_cost"}
_SMTP_FIELDS = {"sender_authorized", "recipient_authorized", "send_authorized", "max_attempts"}
_RECEIPT_FIELDS = {"sequence", "operation", "amount", "currency", "committed_at", "metadata"}
_BATCH_FIELDS = {"authorization_sha256", "authorization", "counts", "reserved", "receipts"}


class NoExternalCall(StageFailure):
    """Fixed local refusal; the guarded external boundary was not invoked."""

    def __init__(self, code="VALIDATION_BATCH_REFUSED"):
        if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
            code = "VALIDATION_BATCH_REFUSED"
        super().__init__(retryable=False)
        self.code = code
        self.args = (code,)


def _deny(code="VALIDATION_AUTHORIZATION_INVALID"):
    raise NoExternalCall(code) from None


def _object(value, fields):
    if type(value) is not dict or set(value) != fields:
        _deny()


def _slug(value):
    return isinstance(value, str) and _SLUG.fullmatch(value) is not None


def _amount(value, *, positive=True):
    if not isinstance(value, (str, Decimal)):
        _deny()
    text = format(value, "f") if isinstance(value, Decimal) else value
    if _DECIMAL.fullmatch(text) is None:
        _deny()
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount > _MAX_MONEY or amount < 0 or positive and amount == 0:
            _deny()
        return amount
    except (InvalidOperation, ValueError, OverflowError):
        _deny()


def _money_text(value):
    return format(value, "f")


def _timestamp(value):
    try:
        return parse_aware_iso(value)
    except (ValueError, TypeError, OverflowError):
        _deny()


def _clock_value(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _deny("VALIDATION_CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _address(value):
    # SMTPConfig performs its stronger envelope validation during assembly.
    return (isinstance(value, str) and len(value) <= 254 and value.isascii()
            and re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value) is not None
            and all(33 <= ord(character) <= 126 for character in value))


def _path(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        _deny()
    path = Path(value)
    if not path.is_absolute() or path.name in ("", ".", "..") or ".." in path.parts:
        _deny()
    if os.name == "nt":
        # This local project does not authorize network shares, Win32 device
        # namespaces, alternate data streams or ambiguous Windows filenames.
        if re.match(r"[A-Za-z]:[\\/]", value) is None or ":" in value[2:]:
            _deny()
        for part in path.parts[1:]:
            stem = part.split(".", 1)[0].upper()
            if (part.endswith((".", " ")) or stem in {"CON", "PRN", "AUX", "NUL"}
                    or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
                _deny()
    return path


def _path_artifacts(path):
    text = os.path.normcase(os.path.abspath(path))
    return {text, text + ".bak", text + ".lock", text + ".guard"}


def _limit(value, maximum=10000):
    if type(value) is not int or not 0 < value <= maximum:
        _deny()


def _canonical(raw):
    try:
        return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        _deny()


@dataclass(frozen=True)
class BatchAuthorization:
    """Validated declarations; evidence IDs still require independent review.

    Approval booleans do not establish source behavior, prices or identity.
    The root preflight must validate their referenced official evidence before
    constructing live services. No credentials are accepted in this schema.
    """

    approval_id: str
    batch_id: str
    approved_at: datetime
    expires_at: datetime
    target_username: str
    target_user_id: str
    state_path: Path
    ledger_path: Path
    recipient: str
    sender: str
    source_contract_evidence_id: str
    authorization_sha256: str
    _canonical_json: str = field(repr=False)

    @classmethod
    def from_dict(cls, raw, *, now):
        _object(raw, _ROOT_FIELDS)
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["approved"] is not True:
            _deny()
        for name in ("approval_id", "batch_id", "source_contract_evidence_id"):
            if not _slug(raw[name]):
                _deny()
        approved, expiry, current = _timestamp(raw["approved_at"]), _timestamp(raw["expires_at"]), _clock_value(now)
        if expiry <= approved or expiry - approved > timedelta(hours=24):
            _deny("VALIDATION_AUTHORIZATION_WINDOW_INVALID")
        if current < approved or current >= expiry:
            _deny("VALIDATION_AUTHORIZATION_EXPIRED")
        _object(raw["target"], {"username", "user_id"})
        username, identifier = raw["target"]["username"], raw["target"]["user_id"]
        if username != "thsottiaux" or not isinstance(identifier, str) or re.fullmatch(r"[1-9][0-9]{0,19}", identifier) is None:
            _deny()
        if int(identifier) > (1 << 64) - 1:
            _deny()
        state_path = _path(raw["state_path"])
        ledger_path = _path(raw["ledger_path"])
        if _path_artifacts(state_path) & _path_artifacts(ledger_path):
            _deny("VALIDATION_LEDGER_STATE_PATH_CONFLICT")
        if not _address(raw["recipient"]) or not _address(raw["sender"]) or raw["recipient"] != LOCAL_RECIPIENT:
            _deny()
        ds, apify, smtp = raw["deepseek"], raw["apify"], raw["smtp"]
        _object(ds, _DS_FIELDS)
        _object(apify, _APIFY_FIELDS)
        _object(smtp, _SMTP_FIELDS)
        for section, flags in ((ds, ("credential_use_authorized", "budget_approved")),
                               (apify, ("credential_use_authorized", "budget_approved", "monetary_bound_verified")),
                               (smtp, ("sender_authorized", "recipient_authorized", "send_authorized"))):
            if any(section[name] is not True for name in flags):
                _deny("VALIDATION_APPROVAL_REQUIRED")
        if not _slug(ds["model"]) or not _slug(ds["price_snapshot_id"]) or ds["currency"] not in ("USD", "CNY"):
            _deny()
        _limit(ds["max_requests"])
        _amount(ds["amount"])
        if apify["currency"] != "USD" or not _slug(apify["bound_evidence_id"]):
            _deny()
        for name in ("max_run_starts", "max_run_polls", "max_dataset_reads", "max_http_attempts"):
            _limit(apify[name])
        _amount(apify["amount"])
        for name in ("start_max_cost", "poll_max_cost", "dataset_max_cost"):
            _amount(apify[name], positive=name == "start_max_cost")
        _limit(smtp["max_attempts"])
        canonical = _canonical(raw)
        return cls(raw["approval_id"], raw["batch_id"], approved, expiry, username, identifier, state_path, ledger_path,
                   raw["recipient"], raw["sender"], raw["source_contract_evidence_id"],
                   hashlib.sha256(canonical.encode("ascii")).hexdigest(), canonical)

    def to_dict(self):
        return json.loads(self._canonical_json)

    @property
    def deepseek(self):
        return self.to_dict()["deepseek"]

    @property
    def apify(self):
        return self.to_dict()["apify"]

    @property
    def smtp(self):
        return self.to_dict()["smtp"]

    def validate_at(self, now):
        # Re-parse the full canonical data as a guard against manually forged DTOs.
        try:
            verified = type(self).from_dict(self.to_dict(), now=now)
        except (ValueError, TypeError, KeyError, AttributeError):
            _deny("VALIDATION_AUTHORIZATION_BINDING_INVALID")
        if verified != self:
            _deny("VALIDATION_AUTHORIZATION_BINDING_INVALID")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _deny("VALIDATION_LEDGER_INVALID")
        result[key] = value
    return result


class BatchLedger:
    """Independent cumulative ledger with no automatic initialization/recovery."""

    def __init__(self, path, authorization, *, clock=None, fault_hook=None):
        if not isinstance(authorization, BatchAuthorization):
            _deny()
        self.path = _path(str(path))
        authorized_path = _path(str(authorization.ledger_path))
        state_path = _path(str(authorization.state_path))
        if os.path.normcase(os.path.abspath(self.path)) != os.path.normcase(os.path.abspath(authorized_path)):
            _deny("VALIDATION_LEDGER_PATH_BINDING_MISMATCH")
        if _path_artifacts(self.path) & _path_artifacts(state_path):
            _deny("VALIDATION_LEDGER_STATE_PATH_CONFLICT")
        self.guard_path = self.path.with_name(self.path.name + ".guard")
        self.authorization = authorization
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_hook = fault_hook or (lambda event, snapshot: None)
        if not callable(self.clock) or not callable(self.fault_hook):
            _deny()

    def _current(self):
        now = _clock_value(self.clock())
        self.authorization.validate_at(now)
        return now

    def _load(self):
        try:
            data = self.path.read_bytes()
            if len(data) > 4_194_304:
                _deny("VALIDATION_LEDGER_INVALID")
            raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                             parse_constant=lambda _value: _deny("VALIDATION_LEDGER_INVALID"))
            self._validate(raw)
            self._check_witness(data, raw["revision"])
            return raw
        except NoExternalCall:
            raise
        except FileNotFoundError:
            if self.guard_path.exists():
                _deny("VALIDATION_LEDGER_RECOVERY_REQUIRED")
            _deny("VALIDATION_LEDGER_NOT_INITIALIZED")
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            _deny("VALIDATION_LEDGER_INVALID")

    def _check_witness(self, data, revision):
        try:
            witness_data = self.guard_path.read_bytes()
            if len(witness_data) > 1024:
                _deny()
            witness = json.loads(witness_data.decode("ascii"), object_pairs_hook=_unique,
                                 parse_constant=lambda _value: _deny())
            _object(witness, {"schema_version", "revision", "ledger_sha256"})
            if (type(witness["schema_version"]) is not int or witness["schema_version"] != 1
                    or type(witness["revision"]) is not int or witness["revision"] != revision
                    or witness["ledger_sha256"] != hashlib.sha256(data).hexdigest()):
                _deny()
        except (NoExternalCall, OSError, ValueError, TypeError, KeyError, UnicodeError):
            _deny("VALIDATION_LEDGER_RECOVERY_REQUIRED")

    def _advance_witness(self, data, revision, *, first_creation):
        witness = {"schema_version": 1, "revision": revision,
                   "ledger_sha256": hashlib.sha256(data).hexdigest()}
        serialized = (json.dumps(witness, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if first_creation:
            # First initialization leaves a permanent marker even if the
            # subsequent main-ledger commit never becomes visible.
            with self.guard_path.open("xb") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            return
        if not self.guard_path.exists():
            _deny("VALIDATION_LEDGER_RECOVERY_REQUIRED")
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=self.guard_path.name + ".", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.guard_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _validate(self, raw):
        try:
            _object(raw, {"schema_version", "revision", "approval_index", "batches"})
            if raw["schema_version"] != 1 or type(raw["schema_version"]) is not int or type(raw["revision"]) is not int or raw["revision"] < 1:
                _deny()
            if type(raw["approval_index"]) is not dict or type(raw["batches"]) is not dict or not 1 <= len(raw["batches"]) <= 128:
                _deny()
            approvals = {}
            for identifier, entry in raw["batches"].items():
                _object(entry, _BATCH_FIELDS)
                source = entry["authorization"]
                auth = BatchAuthorization.from_dict(source, now=_timestamp(source["approved_at"]))
                if identifier != auth.batch_id or entry["authorization_sha256"] != auth.authorization_sha256 or auth.approval_id in approvals:
                    _deny()
                approvals[auth.approval_id] = identifier
                counts = {operation: 0 for operation in OPERATIONS}
                counts["apify_http_attempts"] = 0
                reserved = {"deepseek": {"currency": auth.deepseek["currency"], "amount": "0"},
                            "apify": {"currency": "USD", "amount": "0"}}
                receipts = entry["receipts"]
                if type(receipts) is not list or len(receipts) > 20000:
                    _deny()
                if (type(entry["counts"]) is not dict or set(entry["counts"]) != set(counts)
                        or any(type(value) is not int or value < 0 for value in entry["counts"].values())):
                    _deny()
                for sequence, receipt in enumerate(receipts, 1):
                    _object(receipt, _RECEIPT_FIELDS)
                    if type(receipt["sequence"]) is not int or receipt["sequence"] != sequence:
                        _deny()
                    if type(receipt["metadata"]) is not dict:
                        _deny()
                    at = _timestamp(receipt["committed_at"])
                    if not auth.approved_at <= at < auth.expires_at:
                        _deny()
                    self._apply(auth, counts, reserved, receipt["operation"], receipt["amount"], receipt["currency"], receipt["metadata"])
                if entry["counts"] != counts or entry["reserved"] != reserved:
                    _deny()
            if raw["approval_index"] != approvals:
                _deny()
        except (NoExternalCall, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            _deny("VALIDATION_LEDGER_INVALID")

    def _entry(self, raw):
        auth = self.authorization
        batch = raw["approval_index"].get(auth.approval_id)
        if batch is not None and batch != auth.batch_id:
            _deny("VALIDATION_APPROVAL_ALREADY_BOUND")
        entry = raw["batches"].get(auth.batch_id)
        if entry is None:
            _deny("VALIDATION_BATCH_NOT_INITIALIZED")
        if entry["authorization_sha256"] != auth.authorization_sha256 or entry["authorization"] != auth.to_dict():
            _deny("VALIDATION_AUTHORIZATION_CHANGED")
        return entry

    def _save(self, raw, *, first_creation=False):
        self._validate(raw)
        temporary = None
        try:
            self.fault_hook("before_save", deepcopy(raw))
            data = (json.dumps(raw, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
            if len(data) > 4_194_304:
                _deny("VALIDATION_LEDGER_FULL")
            descriptor, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self.fault_hook("after_temp_fsync", deepcopy(raw))
            self.fault_hook("before_replace", deepcopy(raw))
            self._advance_witness(data, raw["revision"], first_creation=first_creation)
            self.fault_hook("after_guard_replace", deepcopy(raw))
            os.replace(temporary, self.path)
            temporary = None
            self.fault_hook("after_replace", deepcopy(raw))
        except NoExternalCall:
            raise
        except Exception:
            _deny("VALIDATION_LEDGER_COMMIT_FAILED")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _locked(self, action):
        try:
            with ProcessLock(str(self.path) + ".lock"):
                return action()
        except LockBusy:
            _deny("VALIDATION_LEDGER_BUSY")
        except OSError:
            _deny("VALIDATION_LEDGER_IO_FAILED")

    def initialize(self):
        """Explicit local initialization; existing approvals are never reset."""
        def action():
            self._current()
            first_creation = False
            if self.path.exists():
                raw = self._load()
                auth = self.authorization
                if auth.approval_id in raw["approval_index"] or auth.batch_id in raw["batches"]:
                    return deepcopy(self._entry(raw))
            else:
                # An old .bak is uncertainty, not permission to start at zero.
                if self.guard_path.exists() or self.path.with_name(self.path.name + ".bak").exists():
                    _deny("VALIDATION_LEDGER_RECOVERY_REQUIRED")
                first_creation = True
                raw = {"schema_version": 1, "revision": 0, "approval_index": {}, "batches": {}}
            auth = self.authorization
            raw["approval_index"][auth.approval_id] = auth.batch_id
            raw["batches"][auth.batch_id] = {
                "authorization_sha256": auth.authorization_sha256, "authorization": auth.to_dict(),
                "counts": {**{operation: 0 for operation in OPERATIONS}, "apify_http_attempts": 0},
                "reserved": {"deepseek": {"currency": auth.deepseek["currency"], "amount": "0"},
                             "apify": {"currency": "USD", "amount": "0"}}, "receipts": [],
            }
            raw["revision"] += 1
            self._save(raw, first_creation=first_creation)
            return deepcopy(self._entry(raw))
        return self._locked(action)

    def validate_ready(self):
        def action():
            self._current()
            return deepcopy(self._entry(self._load()))
        return self._locked(action)

    @staticmethod
    def _metadata(auth, value):
        if value is None:
            return {}
        if type(value) is not dict or set(value) - {"input_tokens_upper_bound", "output_tokens_upper_bound", "attempt_number", "price_snapshot_id", "notification_kind", "model", "token_proof_id"}:
            _deny("VALIDATION_RESERVATION_METADATA_INVALID")
        for name, item in value.items():
            if name in ("input_tokens_upper_bound", "output_tokens_upper_bound", "attempt_number"):
                if type(item) is not int or not 0 <= item <= 10_000_000:
                    _deny("VALIDATION_RESERVATION_METADATA_INVALID")
            elif name == "price_snapshot_id" and item != auth.deepseek["price_snapshot_id"]:
                _deny("VALIDATION_RESERVATION_METADATA_INVALID")
            elif name == "model" and item != auth.deepseek["model"]:
                _deny("VALIDATION_RESERVATION_METADATA_INVALID")
            elif name == "token_proof_id" and not _slug(item):
                _deny("VALIDATION_RESERVATION_METADATA_INVALID")
            elif name == "notification_kind" and item not in ("TWEET", "HEALTH"):
                _deny("VALIDATION_RESERVATION_METADATA_INVALID")
        return deepcopy(value)

    @classmethod
    def _apply(cls, auth, counts, reserved, operation, amount, currency, metadata):
        if operation not in OPERATIONS:
            _deny("VALIDATION_OPERATION_INVALID")
        cls._metadata(auth, metadata)
        if operation == "smtp_attempt":
            if amount is not None or currency is not None:
                _deny("VALIDATION_CURRENCY_MISMATCH")
            limit = auth.smtp["max_attempts"]
        else:
            section = "deepseek" if operation == "deepseek_request" else "apify"
            facts = getattr(auth, section)
            if currency != facts["currency"]:
                _deny("VALIDATION_CURRENCY_MISMATCH")
            debit = _amount(amount, positive=operation in ("deepseek_request", "apify_start"))
            if section == "apify":
                bound_name, limit_name = {
                    "apify_start": ("start_max_cost", "max_run_starts"),
                    "apify_poll": ("poll_max_cost", "max_run_polls"),
                    "apify_dataset": ("dataset_max_cost", "max_dataset_reads"),
                }[operation]
                if debit < _amount(facts[bound_name], positive=operation == "apify_start"):
                    _deny("VALIDATION_COST_BOUND_INSUFFICIENT")
                limit = facts[limit_name]
                if counts["apify_http_attempts"] >= facts["max_http_attempts"]:
                    _deny("VALIDATION_APIFY_HTTP_LIMIT_REACHED")
                counts["apify_http_attempts"] += 1
            else:
                limit = facts["max_requests"]
            total = _amount(reserved[section]["amount"], positive=False) + debit
            if total > _amount(facts["amount"]):
                _deny("VALIDATION_MONEY_LIMIT_REACHED")
            reserved[section]["amount"] = _money_text(total)
        if counts[operation] >= limit:
            _deny("VALIDATION_ATTEMPT_LIMIT_REACHED")
        counts[operation] += 1

    def reserve(self, operation, *, amount=None, currency=None, metadata=None):
        """Commit an irrevocable reservation before the caller attempts IO."""
        def action():
            now = self._current()
            raw = self._load()
            entry = self._entry(raw)
            safe_metadata = self._metadata(self.authorization, metadata)
            self._apply(self.authorization, entry["counts"], entry["reserved"], operation, amount, currency, safe_metadata)
            receipt = {"sequence": len(entry["receipts"]) + 1, "operation": operation,
                       "amount": None if amount is None else _money_text(_amount(amount, positive=False)),
                       "currency": currency, "committed_at": now.isoformat(), "metadata": safe_metadata}
            entry["receipts"].append(receipt)
            raw["revision"] += 1
            self._save(raw)
            return deepcopy(receipt)
        return self._locked(action)
