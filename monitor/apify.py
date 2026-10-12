"""Candidate Apify source with a durable, explicitly acknowledged paid run.

Official references checked 2026-10-10:
https://docs.apify.com/api/v2/actors-runs-post
https://docs.apify.com/api/v2/actor-run-get
https://docs.apify.com/api/v2/dataset-items-get
https://docs.apify.com/api/v2
https://apify.com/lance_api/x-tweet-scraper-api/api/openapi
https://apify.com/lance_api/x-tweet-scraper-api

The candidate schema supports profiles/maxResults, but does not document flags
proving reply inclusion and retweet exclusion. Therefore the production source
contract catalog is empty. A caller's source_contract_verified flag cannot
provide that proof. LIVE requires a stable-ID-bound reviewed catalog entry;
the legacy unbound path exists for historical offline fixtures only.

The caller holds the monitor's process lock for bind/fetch/prepare_ingest and the
final main-state commit. No separate journal, backup rollback, automatic POST
retry, or automatic release of an uncertain run is provided here.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
from typing import Callable
from urllib.parse import urlencode
from uuid import uuid4

from .http_transport import HttpTransport
from .models import FetchSnapshot, SourceContractError, StageFailure, parse_time, timestamp, validate_snapshot
from .source_contract import SourceEvidenceError, resolve_source_contract, valid_user_id


ACTOR = "lance_api~x-tweet-scraper-api"
API_BASE = "https://api.apify.com/v2"
_RESOURCE_ID = re.compile(r"[A-Za-z0-9]{17}\Z")
_RUN_STATUSES = {"READY", "RUNNING", "SUCCEEDED", "FAILED", "TIMING-OUT", "TIMED-OUT", "ABORTING", "ABORTED"}
_PHASES = {"STARTING", "FETCH_UNKNOWN", "RUNNING", "READY", "PAUSED"}
_HTTP_DATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT\Z"
)
_FIELDS = {
    "schema_version", "phase", "request_id", "actor", "target", "started_at",
    "run_id", "dataset_id", "run_status", "snapshot", "snapshot_hash", "last_error", "budget", "billing",
}
_IDENTITY_FIELDS = {"target_user_id", "source_contract_id", "build_id", "build_number"}


class ApifyError(StageFailure):
    def __init__(self, code: str, *, retryable=False, retry_after_seconds=None):
        super().__init__(retryable=retryable, retry_after_seconds=retry_after_seconds)
        self.code = code
        self.args = (code,)


def _retry_after(headers, now):
    """Preserve one finite nonnegative delta-seconds or IMF HTTP-date hint."""
    if not isinstance(headers, dict):
        return None
    values = [value for key, value in headers.items() if isinstance(key, str) and key.lower() == "retry-after"]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    text = values[0].strip(" \t")
    try:
        if re.fullmatch(r"[0-9]+", text):
            seconds = float(int(text))
        elif _HTTP_DATE.fullmatch(text):
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return None
            seconds = max(0.0, (parsedate_to_datetime(text) - now.astimezone(timezone.utc)).total_seconds())
        else:
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _money(value, *, positive=True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Explicit finite monetary value required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Explicit finite monetary value required") from None
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValueError("Explicit finite monetary value required")
    # Bound serialization and avoid accepting implausible exponent strings.
    if result and not -12 <= result.adjusted() <= 9:
        raise ValueError("Monetary precision is outside the supported range")
    return result


def _money_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class ApifyPolicy:
    """Explicit approval facts; none are discovered from account credentials.

    pricing_verified includes reviewing Actor events and additional operations
    such as dataset reads. maxTotalChargeUsd limits the Actor run, not all API or
    account spending. The approving caller must budget those additional costs.
    """

    approved: bool = False
    available_balance_usd: Decimal | str | float | None = None
    max_total_charge_usd: Decimal | str | float | None = None
    minimum_charge_cap_usd: Decimal | str | float | None = None
    per_result_usd: Decimal | str | float | None = None
    pricing_verified: bool = False
    source_contract_verified: bool = False

    def checked_budget(self) -> dict:
        if self.approved is not True or self.pricing_verified is not True:
            raise ApifyError("APIFY_BUDGET_NOT_APPROVED")
        if self.source_contract_verified is not True:
            raise ApifyError("APIFY_SOURCE_CONTRACT_UNVERIFIED")
        try:
            balance = _money(self.available_balance_usd)
            cap = _money(self.max_total_charge_usd)
            minimum = _money(self.minimum_charge_cap_usd, positive=False)
            price = _money(self.per_result_usd)
        except ValueError:
            raise ApifyError("APIFY_BUDGET_FACTS_UNKNOWN") from None
        if cap < minimum or cap < 3 * price:
            raise ApifyError("APIFY_APPROVED_CAP_INSUFFICIENT")
        if balance < cap:
            raise ApifyError("APIFY_BALANCE_INSUFFICIENT")
        return {
            "approved_cap_usd": _money_text(cap),
            "per_result_usd": _money_text(price),
            "minimum_cap_usd": _money_text(minimum),
        }


def _canonical_snapshot(tweets: list[dict], target: str, target_user_id=None) -> list[dict]:
    result = validate_snapshot(FetchSnapshot(deepcopy(tweets)), target, 3,
                               target_user_id=target_user_id)
    if target_user_id is not None and any(tweet.get("author_id") != target_user_id for tweet in result):
        raise ValueError("Snapshot author identity mismatch")
    return result


def _snapshot_hash(tweets: list[dict]) -> str:
    data = json.dumps(tweets, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate_active_fetch(value) -> None:
    """Validate the main-state journal; raise ValueError, never silently repair."""
    if value is None:
        return
    def require(condition):
        if not condition:
            raise ValueError("Invalid durable Apify fetch state")
    require(isinstance(value, dict) and type(value.get("schema_version")) is int)
    version = value["schema_version"]
    require(version in (1, 2) and set(value) == (_FIELDS if version == 1 else _FIELDS | _IDENTITY_FIELDS))
    require(isinstance(value["phase"], str) and value["phase"] in _PHASES)
    require(value["actor"] == ACTOR)
    require(isinstance(value["request_id"], str) and re.fullmatch(r"[0-9a-f]{32}", value["request_id"]) is not None)
    require(isinstance(value["target"], str) and re.fullmatch(r"[a-z0-9_]{1,15}", value["target"]) is not None)
    target_user_id = value.get("target_user_id")
    if version == 2:
        require(target_user_id is None or valid_user_id(target_user_id))
        if target_user_id is None:
            require(all(value[name] is None for name in _IDENTITY_FIELDS))
        else:
            require(isinstance(value["source_contract_id"], str)
                    and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value["source_contract_id"]) is not None)
            require(isinstance(value["build_id"], str) and _RESOURCE_ID.fullmatch(value["build_id"]) is not None)
            require(isinstance(value["build_number"], str)
                    and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", value["build_number"]) is not None)
    parse_time(value["started_at"])
    for name in ("run_id", "dataset_id"):
        require(value[name] is None or isinstance(value[name], str) and _RESOURCE_ID.fullmatch(value[name]) is not None)
    require(value["run_status"] is None or isinstance(value["run_status"], str) and value["run_status"] in _RUN_STATUSES)
    require(value["last_error"] is None or isinstance(value["last_error"], str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value["last_error"]) is not None)
    budget = value["budget"]
    require(isinstance(budget, dict) and set(budget) == {"approved_cap_usd", "per_result_usd", "minimum_cap_usd"})
    require(all(isinstance(item, str) for item in budget.values()))
    cap = _money(budget["approved_cap_usd"])
    price = _money(budget["per_result_usd"])
    minimum = _money(budget["minimum_cap_usd"], positive=False)
    require(cap >= minimum and cap >= 3 * price)
    billing = value["billing"]
    require(isinstance(billing, dict) and set(billing) == {"usage_total_usd", "charged_results"})
    require(billing["usage_total_usd"] is None or isinstance(billing["usage_total_usd"], str))
    if billing["usage_total_usd"] is not None:
        require(_money(billing["usage_total_usd"], positive=False) <= cap)
    require(billing["charged_results"] is None or type(billing["charged_results"]) is int and 0 <= billing["charged_results"] <= 3)
    phase = value["phase"]
    if phase in ("STARTING", "FETCH_UNKNOWN"):
        require(value["run_id"] is None and value["dataset_id"] is None and value["run_status"] is None)
    if phase in ("RUNNING", "READY"):
        require(value["run_id"] is not None and value["run_status"] is not None)
    if phase in ("FETCH_UNKNOWN", "PAUSED"):
        require(value["last_error"] is not None)
    if phase == "READY":
        require(value["run_status"] == "SUCCEEDED" and value["dataset_id"] is not None)
        require(isinstance(value["snapshot"], list))
        normalized = _canonical_snapshot(value["snapshot"], value["target"], target_user_id)
        require(normalized == value["snapshot"])
        require(value["snapshot_hash"] == _snapshot_hash(normalized))
    else:
        require(value["snapshot"] is None and value["snapshot_hash"] is None)


def _json_body(response):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    def bad_constant(_value):
        raise ValueError("Non-finite JSON number")
    if not isinstance(response.body, bytes) or len(response.body) > 1_000_000:
        raise ValueError("Invalid response body")
    return json.loads(response.body.decode("utf-8"), object_pairs_hook=unique, parse_constant=bad_constant)


class ApifyFetcher:
    def __init__(self, *, api_token: str, transport: HttpTransport,
                 policy: ApifyPolicy | None = None, target="thsottiaux", timeout=30.0, clock=None,
                 target_user_id=None, contract_id=None, contract_catalog=None):
        if not isinstance(api_token, str) or not api_token or any(ord(c) < 33 or ord(c) == 127 for c in api_token):
            raise ApifyError("APIFY_TOKEN_INVALID")
        if not isinstance(target, str) or not re.fullmatch(r"[a-z0-9_]{1,15}", target):
            raise ApifyError("APIFY_TARGET_INVALID")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ApifyError("APIFY_TIMEOUT_INVALID")
        if target_user_id is not None and not valid_user_id(target_user_id):
            raise ApifyError("APIFY_TARGET_ID_INVALID")
        if target_user_id is None and (contract_id is not None or contract_catalog is not None):
            raise ApifyError("APIFY_TARGET_ID_REQUIRED")
        self._token, self.transport = api_token, transport
        self.policy, self.target = policy or ApifyPolicy(), target
        self.timeout, self.clock = float(timeout), clock or (lambda: datetime.now(timezone.utc))
        self.target_user_id, self.contract_id = target_user_id, contract_id
        self.contract_catalog = contract_catalog
        self._state = self._persist = None

    def _check_contract(self):
        if self.target_user_id is None:
            return None
        try:
            return resolve_source_contract(self.contract_id, target_username=self.target,
                                           target_user_id=self.target_user_id, now=self.clock(),
                                           catalog=self.contract_catalog)
        except SourceEvidenceError as exc:
            raise ApifyError(exc.code) from None

    def _check_bound_journal(self, active, entry):
        if self.target_user_id is None or active is None:
            return
        if active["schema_version"] == 1:
            raise ApifyError("APIFY_LEGACY_IDENTITY_UNVERIFIED")
        if (active["target_user_id"] != self.target_user_id
                or active["source_contract_id"] != self.contract_id
                or active["build_id"] != entry.build_id
                or active["build_number"] != entry.build_number):
            raise ApifyError("APIFY_JOURNAL_IDENTITY_MISMATCH")

    def bind_state(self, state: dict, persist: Callable[[dict], None]) -> None:
        if not isinstance(state, dict) or state.get("target") != self.target or "active_fetch" not in state or not callable(persist):
            raise ApifyError("APIFY_STATE_BINDING_INVALID")
        try:
            validate_active_fetch(state["active_fetch"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ApifyError("APIFY_JOURNAL_INVALID") from None
        if state["active_fetch"] is not None and state["active_fetch"]["target"] != self.target:
            raise ApifyError("APIFY_JOURNAL_TARGET_MISMATCH")
        if state.get("execution_mode") == "LIVE" and self.target_user_id is None:
            raise ApifyError("APIFY_TARGET_ID_REQUIRED")
        if self.target_user_id is not None:
            if state.get("target_user_id") != self.target_user_id:
                raise ApifyError("APIFY_STATE_IDENTITY_MISMATCH")
            entry = self._check_contract()
            self._check_bound_journal(state["active_fetch"], entry)
        self._state, self._persist = state, persist

    def _commit(self):
        validate_active_fetch(self._state["active_fetch"])
        self._persist(self._state)

    def _stop(self, code, *, unknown=False):
        active = self._state["active_fetch"]
        active.update(phase="FETCH_UNKNOWN" if unknown else "PAUSED", last_error=code, snapshot=None, snapshot_hash=None)
        self._commit()
        raise ApifyError(code)

    def _request(self, method, path, *, body=None):
        try:
            entry = self._check_contract()
            self._check_bound_journal(self._state["active_fetch"], entry)
        except ApifyError:
            from .batch import NoExternalCall
            raise NoExternalCall("APIFY_SOURCE_CONTRACT_UNVERIFIED") from None
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.transport.request(method, API_BASE + path, headers=headers, body=body, timeout=self.timeout)

    def fetch_latest(self, *, limit: int) -> FetchSnapshot:
        if type(limit) is not int or limit != 3:
            raise ApifyError("APIFY_LIMIT_MUST_BE_THREE")
        if self._state is None:
            raise ApifyError("APIFY_STATE_NOT_BOUND")
        if self._state.get("execution_mode") == "LIVE" and self.target_user_id is None:
            raise ApifyError("APIFY_TARGET_ID_REQUIRED")
        entry = self._check_contract()  # Includes READY replay and known-run recovery.
        try:
            validate_active_fetch(self._state["active_fetch"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ApifyError("APIFY_JOURNAL_INVALID") from None
        active = self._state["active_fetch"]
        self._check_bound_journal(active, entry)
        if active is not None:
            if active["phase"] == "STARTING":
                self._stop("APIFY_START_RESPONSE_UNCONFIRMED", unknown=True)
            if active["phase"] in ("FETCH_UNKNOWN", "PAUSED"):
                raise ApifyError(active["last_error"])
            if active["phase"] == "READY":
                return FetchSnapshot(deepcopy(active["snapshot"]))
        else:
            self._start()
        return self._resume()

    def _start(self):
        budget = self.policy.checked_budget()
        entry = self._check_contract()
        self._state["active_fetch"] = {
            "schema_version": 2, "phase": "STARTING", "request_id": uuid4().hex,
            "actor": ACTOR, "target": self.target, "started_at": timestamp(self.clock()),
            "target_user_id": self.target_user_id, "source_contract_id": self.contract_id,
            "build_id": entry.build_id if entry is not None else None,
            "build_number": entry.build_number if entry is not None else None,
            "run_id": None, "dataset_id": None, "run_status": None,
            "snapshot": None, "snapshot_hash": None, "last_error": None, "budget": budget,
            "billing": {"usage_total_usd": None, "charged_results": None},
        }
        self._commit()  # A durable startup intention is mandatory before POST.
        options = {"maxTotalChargeUsd": budget["approved_cap_usd"], "restartOnError": "false", "waitForFinish": "0"}
        if entry is not None:
            options["build"] = entry.build_number
        query = urlencode(options)
        body = json.dumps({"profiles": [self.target], "maxResults": 3}, separators=(",", ":")).encode("utf-8")
        try:
            response = self._request("POST", f"/acts/{ACTOR}/runs?{query}", body=body)
            payload = _json_body(response)
            if type(response.status) is not int or response.status != 201 or not isinstance(payload, dict) or "error" in payload:
                raise ValueError("Unconfirmed start")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not _RESOURCE_ID.fullmatch(data["id"]):
                raise ValueError("Unconfirmed run ID")
            if not isinstance(data.get("status"), str) or data["status"] not in _RUN_STATUSES:
                raise ValueError("Unconfirmed run status")
            if entry is not None and data.get("buildId") != entry.build_id:
                raise ValueError("Unconfirmed Actor build")
            dataset = data.get("defaultDatasetId")
            if dataset is not None and (not isinstance(dataset, str) or not _RESOURCE_ID.fullmatch(dataset)):
                raise ValueError("Invalid dataset ID")
        except Exception as exc:
            # A budget wrapper's durable reservation rejection proves no HTTP
            # request began. Preserve the startup journal in a paused state.
            from .batch import NoExternalCall
            if isinstance(exc, NoExternalCall):
                self._stop("APIFY_CALL_NOT_RESERVED")
            self._stop("APIFY_START_RESPONSE_UNCONFIRMED", unknown=True)
        self._state["active_fetch"].update(phase="RUNNING", run_id=data["id"], dataset_id=dataset, run_status=data["status"])
        self._commit()

    def _get(self, path):
        try:
            response = self._request("GET", path)
        except StageFailure as exc:
            if not exc.retryable:
                self._stop("APIFY_GET_CONTRACT_ERROR")
            raise ApifyError("APIFY_GET_TEMPORARY_FAILURE", retryable=True,
                             retry_after_seconds=exc.retry_after_seconds) from None
        except (ValueError, TypeError):
            self._stop("APIFY_GET_CONFIGURATION_ERROR")
        except Exception:
            raise ApifyError("APIFY_GET_TEMPORARY_FAILURE", retryable=True) from None
        if type(getattr(response, "status", None)) is not int:
            self._stop("APIFY_HTTP_CONTRACT_ERROR")
        if response.status in (408, 429) or 500 <= response.status <= 599:
            delay = _retry_after(response.headers, self.clock()) if response.status in (429, 503) else None
            raise ApifyError("APIFY_GET_TEMPORARY_FAILURE", retryable=True, retry_after_seconds=delay)
        if response.status != 200:
            self._stop("APIFY_GET_REJECTED")
        try:
            payload = _json_body(response)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            self._stop("APIFY_RESPONSE_INVALID")
        if isinstance(payload, dict) and "error" in payload:
            self._stop("APIFY_BUSINESS_ERROR")
        return response, payload

    def _billing(self, data):
        active = self._state["active_fetch"]
        try:
            cap = _money(active["budget"]["approved_cap_usd"])
            if "usageTotalUsd" in data:
                used = _money(data["usageTotalUsd"], positive=False)
                if used > cap:
                    raise ValueError("Run exceeded approved cap")
                active["billing"]["usage_total_usd"] = _money_text(used)
            if "chargedEventCounts" in data:
                counts = data["chargedEventCounts"]
                if not isinstance(counts, dict) or any(type(count) is not int or count < 0 for count in counts.values()):
                    raise ValueError("Invalid charge events")
                if any(name != "tweet-result" and count for name, count in counts.items()):
                    raise ValueError("Unexpected charge event")
                count = counts.get("tweet-result", 0)
                if count > 3:
                    raise ValueError("Too many charge events")
                active["billing"]["charged_results"] = count
            options = data.get("options")
            if options is not None:
                if not isinstance(options, dict):
                    raise ValueError("Invalid run options")
                if "maxTotalChargeUsd" in options and _money(options["maxTotalChargeUsd"]) != cap:
                    raise ValueError("Approved cap changed")
            pricing = data.get("pricingInfo")
            if pricing is not None:
                if not isinstance(pricing, dict):
                    raise ValueError("Invalid pricing")
                if "minimalMaxTotalChargeUsd" in pricing and _money(pricing["minimalMaxTotalChargeUsd"], positive=False) > cap:
                    raise ValueError("Minimum cap exceeds approval")
        except (ValueError, TypeError, InvalidOperation):
            self._stop("APIFY_BILLING_CONTRACT_ERROR")

    def _resume(self):
        active = self._state["active_fetch"]
        _response, payload = self._get(f"/actor-runs/{active['run_id']}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("id") != active["run_id"]:
            self._stop("APIFY_RUN_ID_MISMATCH")
        if self.target_user_id is not None and data.get("buildId") != active["build_id"]:
            self._stop("APIFY_RUN_BUILD_MISMATCH")
        status, dataset = data.get("status"), data.get("defaultDatasetId")
        if not isinstance(status, str) or status not in _RUN_STATUSES:
            self._stop("APIFY_RUN_STATUS_INVALID")
        if dataset is not None and (not isinstance(dataset, str) or not _RESOURCE_ID.fullmatch(dataset)):
            self._stop("APIFY_DATASET_ID_INVALID")
        if active["dataset_id"] is not None and dataset != active["dataset_id"]:
            self._stop("APIFY_DATASET_ID_CHANGED")
        active.update(run_status=status, dataset_id=dataset, last_error=None)
        self._billing(data)
        self._commit()
        if status in {"FAILED", "TIMED-OUT", "ABORTED"}:
            self._stop("APIFY_RUN_FAILED")
        if status != "SUCCEEDED":
            raise ApifyError("APIFY_RUN_NOT_FINISHED", retryable=True)
        if dataset is None:
            self._stop("APIFY_DATASET_ID_MISSING")
        rows = self._read_dataset(dataset)
        charged = active["billing"]["charged_results"]
        if charged is not None and charged != len(rows):
            self._stop("APIFY_CHARGE_COUNT_MISMATCH")
        try:
            tweets = self._normalize(rows)
        except (ValueError, TypeError, KeyError, AttributeError, SourceContractError):
            self._stop("APIFY_TWEET_CONTRACT_ERROR")
        active.update(phase="READY", snapshot=tweets, snapshot_hash=_snapshot_hash(tweets))
        self._commit()  # Return only after the complete immutable snapshot is durable.
        return FetchSnapshot(deepcopy(tweets))

    def _read_dataset(self, dataset):
        rows, offset, expected_total = [], 0, None
        while expected_total is None or offset < expected_total:
            limit = 3 if expected_total is None else expected_total - offset
            query = urlencode({"format": "json", "offset": offset, "limit": limit, "desc": "false", "clean": "false", "skipEmpty": "false", "skipHidden": "false"})
            response, items = self._get(f"/datasets/{dataset}/items?{query}")
            try:
                if not isinstance(items, list) or not isinstance(response.headers, dict):
                    raise ValueError("Invalid dataset page")
                headers = {}
                for name, value in response.headers.items():
                    if not isinstance(name, str) or not isinstance(value, str) or name.lower() in headers:
                        raise ValueError("Invalid pagination headers")
                    headers[name.lower()] = value
                def number(name):
                    value = headers["x-apify-pagination-" + name]
                    if not re.fullmatch(r"[0-9]{1,9}", value):
                        raise ValueError("Invalid pagination number")
                    return int(value)
                page_offset, page_limit, count, total = (number(name) for name in ("offset", "limit", "count", "total"))
                if headers.get("x-apify-pagination-desc", "").lower() not in ("false", "0"):
                    raise ValueError("Unexpected dataset ordering")
                if page_offset != offset or page_limit != limit or count != len(items) or count > limit:
                    raise ValueError("Dataset page does not match its request")
                if not 0 <= total <= 3 or page_offset + count > total:
                    raise ValueError("Dataset exceeds the three-result contract")
                if expected_total is not None and total != expected_total:
                    raise ValueError("Dataset changed during reading")
                if total == 0 or count == 0:
                    raise ValueError("Missing explicit empty-response evidence or missing page")
            except (ValueError, KeyError, TypeError):
                self._stop("APIFY_PAGINATION_CONTRACT_ERROR")
            expected_total = total
            rows.extend(items)
            offset += count
        return rows

    def _normalize(self, rows):
        tweets, identifiers, ordering = [], set(), []
        for row in rows:
            if not isinstance(row, dict) or row.get("success") is not True or row.get("status") != "success" or row.get("error") is not None:
                raise ValueError("Unsuccessful dataset item")
            if row.get("inputType") != "profile" or row.get("normalizedInput", "").lower().lstrip("@") != self.target:
                raise ValueError("Unexpected input attribution")
            if row.get("code") == "NO_DATA":
                if self.target_user_id is not None:
                    raise ValueError("Empty record does not establish bound author identity")
                if len(rows) != 1 or any(row.get(name) not in (None, "") for name in ("tweetId", "text", "url", "createdAt")):
                    raise ValueError("Invalid empty-response record")
                return []
            if row.get("code") is not None or type(row.get("isReply")) is not bool or row.get("isRetweet") is not False or row.get("retweetedTweet") is not None:
                raise ValueError("Unverified tweet kind")
            identifier, author = row.get("tweetId"), row.get("authorUsername")
            if identifier in identifiers:
                raise ValueError("Repeated dataset item")
            identifiers.add(identifier)
            if not isinstance(author, str) or author.lower().lstrip("@") != self.target:
                raise ValueError("Unexpected author")
            nested = row.get("author")
            if nested is not None and (not isinstance(nested, dict) or nested.get("username", "").lower().lstrip("@") != self.target):
                raise ValueError("Conflicting author metadata")
            if self.target_user_id is not None:
                if (row.get("authorId") != self.target_user_id or not isinstance(nested, dict)
                        or nested.get("id") != self.target_user_id):
                    raise ValueError("Missing or mismatched stable author identity")
            context = {name: row[name] for name in ("inReplyToTweetId", "inReplyToUsername", "quotedTweet") if row.get(name) is not None}
            tweet = {"id": identifier, "author": self.target, "text": row.get("text"), "url": row.get("url"),
                     "created_at": row.get("createdAt"), "context": json.dumps(context, ensure_ascii=False) if context else None}
            if self.target_user_id is not None:
                tweet["author_id"] = self.target_user_id
            created = parse_time(tweet["created_at"])
            if self.target_user_id is not None and created > self.clock():
                raise ValueError("Tweet creation time is outside the observed range")
            ordering.append((created, int(identifier)))
            tweets.append(tweet)
        if ordering != sorted(ordering, reverse=True):
            raise ValueError("Provider did not deliver latest-first ordering")
        return _canonical_snapshot(tweets, self.target, self.target_user_id)

    def prepare_ingest(self, snapshot: FetchSnapshot) -> None:
        """Clear READY in memory only; raw inbox/watermark must commit with it."""
        if self._state is None:
            raise ApifyError("APIFY_STATE_NOT_BOUND")
        active = self._state["active_fetch"]
        try:
            validate_active_fetch(active)
            if active is None or active["phase"] != "READY":
                raise ValueError("No ready snapshot")
            if not isinstance(snapshot, FetchSnapshot):
                raise ValueError("Invalid snapshot acknowledgement")
            self._check_contract()
            tweets = _canonical_snapshot(snapshot.tweets, self.target, self.target_user_id)
            if snapshot.complete is not True:
                raise ValueError("Incomplete acknowledgement")
            if tweets != snapshot.tweets or _snapshot_hash(tweets) != active["snapshot_hash"]:
                raise ValueError("Snapshot acknowledgement differs from the journal")
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ApifyError("APIFY_SNAPSHOT_ACKNOWLEDGEMENT_INVALID") from None
        self._state["active_fetch"] = None
