"""One shared CNY 1 budget for the manually authorized model validation.

No automatic initialization, refund, or per-output-directory quota. The live
factory always uses this Windows user's Local AppData, independent of cwd.
Tests inject isolated paths. This is a crash/concurrency guard, not protection
against an administrator deleting or rolling back every ledger artifact.
"""

from monitor.local_settings import PROJECT_BINDING as LOCAL_BINDING
import ctypes
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .locking import LockBusy, ProcessLock

BINDING = LOCAL_BINDING
APPROVAL = 'manual-validation-20261011-cny1'
LIMIT_CNY = Decimal('1.00')
MAX_REQUESTS = 1

class ModelBudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def _deny(code):
    raise ModelBudgetError(code) from None

def _amount(value):
    if not isinstance(value, (str, Decimal)):
        _deny('MODEL_BUDGET_AMOUNT_INVALID')
    try:
        text = format(value, 'f') if isinstance(value, Decimal) else value
        if re.fullmatch(r'(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?', text) is None:
            raise ValueError()
        result = Decimal(value)
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        _deny('MODEL_BUDGET_AMOUNT_INVALID')

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _deny('MODEL_BUDGET_LEDGER_INVALID')
        result[key] = value
    return result

def fixed_path():
    if os.name != 'nt':
        _deny('MODEL_BUDGET_WINDOWS_REQUIRED')
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x001c, None, 0, buffer) != 0:
        _deny('MODEL_BUDGET_PATH_UNAVAILABLE')
    root = Path(buffer.value)
    if not root.is_absolute() or str(root).startswith('\\\\'):
        _deny('MODEL_BUDGET_PATH_UNAVAILABLE')
    return root / 'CodexResetMonitor' / 'manual-model-budget-v1.json'

class ManualModelBudget:
    def __init__(self, path=None):
        self.path = fixed_path() if path is None else Path(path).resolve()
        self.witness = self.path.with_name(self.path.name + '.guard')

    def _load(self):
        try:
            raw = self.path.read_bytes()
            guard = self.witness.read_bytes()
            if len(raw) > 65536 or len(guard) > 1024:
                raise ValueError()
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
            proof = json.loads(guard.decode('ascii'), object_pairs_hook=_unique)
            if (set(data) != {'schema_version','binding','approval_id','currency','limit',
                    'max_requests','revision','requests','committed_cny','entries'}
                    or type(data['schema_version']) is not int or data['schema_version'] != 1
                    or data['binding'] != BINDING or data['approval_id'] != APPROVAL
                    or data['currency'] != 'CNY' or data['limit'] != str(LIMIT_CNY)
                    or type(data['max_requests']) is not int or data['max_requests'] != MAX_REQUESTS
                    or type(data['revision']) is not int or data['revision'] < 1
                    or type(data['requests']) is not int or not 0 <= data['requests'] <= MAX_REQUESTS
                    or not isinstance(data['entries'], list) or len(data['entries']) != data['requests']
                    or proof != {'revision': data['revision'], 'sha256': hashlib.sha256(raw).hexdigest()}):
                raise ValueError()
            amounts = []
            for entry in data['entries']:
                if (not isinstance(entry, dict) or set(entry) != {'amount','kind','reference_sha256','committed_at'}
                        or entry['kind'] not in ('pre_call_reservation','historical_completed_usage')
                        or not isinstance(entry['reference_sha256'], str) or len(entry['reference_sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in entry['reference_sha256'])
                        or not isinstance(entry['committed_at'], str)):
                    raise ValueError()
                amounts.append(_amount(entry['amount']))
            if sum(amounts, Decimal(0)) != _amount(data['committed_cny']) or _amount(data['committed_cny']) > LIMIT_CNY:
                raise ValueError()
            return data
        except FileNotFoundError:
            _deny('MODEL_BUDGET_RECOVERY_REQUIRED' if self.path.exists() or self.witness.exists()
                  else 'MODEL_BUDGET_NOT_INITIALIZED')
        except ModelBudgetError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            _deny('MODEL_BUDGET_RECOVERY_REQUIRED')

    @staticmethod
    def _atomic(path, raw):
        temporary = None
        try:
            descriptor, filename = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
            temporary = Path(filename)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _save(self, data, *, first=False):
        raw = (json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False) + '\n').encode('ascii')
        guard = json.dumps({'revision': data['revision'], 'sha256': hashlib.sha256(raw).hexdigest()}).encode('ascii')
        try:
            if first:
                with self.witness.open('xb') as handle:
                    handle.write(guard)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                self._atomic(self.witness, guard)
            self._atomic(self.path, raw)
        except Exception:
            _deny('MODEL_BUDGET_COMMIT_FAILED')

    def _locked(self, action):
        try:
            with ProcessLock(str(self.path) + '.lock'):
                return action()
        except LockBusy:
            _deny('MODEL_BUDGET_BUSY')
        except OSError:
            _deny('MODEL_BUDGET_IO_FAILED')

    def initialize(self, *, historical_amount=None, historical_receipt_sha256=None):
        """Explicit migration; existing history must exactly match the request."""
        def action():
            amount = None
            if historical_amount is not None:
                amount = _amount(historical_amount)
                if amount > LIMIT_CNY or not self._valid_hash(historical_receipt_sha256):
                    _deny('MODEL_BUDGET_MIGRATION_INVALID')
            elif historical_receipt_sha256 is not None:
                _deny('MODEL_BUDGET_MIGRATION_INVALID')
            if self.path.exists() or self.witness.exists():
                data = self._load()
                if amount is not None:
                    history = [entry for entry in data['entries']
                               if entry['kind'] == 'historical_completed_usage']
                    if (len(history) != 1 or _amount(history[0]['amount']) != amount
                            or history[0]['reference_sha256'] != historical_receipt_sha256):
                        _deny('MODEL_BUDGET_MIGRATION_RECONCILIATION_REQUIRED')
                return data
            if self.path.with_name(self.path.name + '.bak').exists():
                _deny('MODEL_BUDGET_RECOVERY_REQUIRED')
            data = {'schema_version':1,'binding':BINDING,'approval_id':APPROVAL,
                'currency':'CNY','limit':str(LIMIT_CNY),'max_requests':MAX_REQUESTS,
                'revision':1,'requests':0,'committed_cny':'0','entries':[]}
            if amount is not None:
                data['requests'] = 1
                data['committed_cny'] = str(amount)
                data['entries'].append(self._entry(amount, 'historical_completed_usage', historical_receipt_sha256))
            self._save(data, first=True)
            return self._load()
        return self._locked(action)

    @staticmethod
    def _valid_hash(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

    @staticmethod
    def _entry(amount, kind, reference):
        return {'amount':str(amount),'kind':kind,'reference_sha256':reference,
                'committed_at':datetime.now(timezone.utc).isoformat()}

    def _check(self, data, amount):
        if _amount(data['committed_cny']) + amount > LIMIT_CNY:
            _deny('MODEL_BUDGET_LIMIT_EXCEEDED')
        if data['requests'] >= MAX_REQUESTS:
            _deny('MODEL_BUDGET_REQUEST_LIMIT_REACHED')

    def snapshot(self):
        return self._locked(self._load)

    def summary(self):
        """Read validated totals without creating a lock or changing history."""
        data = self._load()
        return {key: data[key] for key in
                ('limit', 'max_requests', 'committed_cny', 'requests')}

    def inspect(self, maximum_cny):
        """Read-only preflight; the eventual reservation rechecks under a lock."""
        amount = _amount(maximum_cny)
        if amount <= 0:
            _deny('MODEL_BUDGET_BOUND_UNVERIFIED')
        if amount > LIMIT_CNY:
            _deny('MODEL_BUDGET_LIMIT_EXCEEDED')
        return self._check(self._load(), amount)

    def preflight(self, maximum_cny):
        amount = _amount(maximum_cny)
        if amount <= 0:
            _deny('MODEL_BUDGET_BOUND_UNVERIFIED')
        if amount > LIMIT_CNY:
            _deny('MODEL_BUDGET_LIMIT_EXCEEDED')
        return self._locked(lambda: self._check(self._load(), amount))

    def reserve(self, maximum_cny, *, request_sha256):
        amount = _amount(maximum_cny)
        if amount <= 0:
            _deny('MODEL_BUDGET_BOUND_UNVERIFIED')
        if amount > LIMIT_CNY:
            _deny('MODEL_BUDGET_LIMIT_EXCEEDED')
        if not self._valid_hash(request_sha256):
            _deny('MODEL_BUDGET_REQUEST_INVALID')
        def action():
            data = self._load()
            self._check(data, amount)
            data['requests'] += 1
            data['committed_cny'] = str(_amount(data['committed_cny']) + amount)
            data['revision'] += 1
            data['entries'].append(self._entry(amount, 'pre_call_reservation', request_sha256))
            self._save(data)
            return data
        return self._locked(action)
