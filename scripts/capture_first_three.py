"""One explicitly confirmed candidate source run; default execution only previews.

This discovery snapshot is separate from monitor state and production proofs.
"""

from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.apify import ApifyFetcher
from monitor.locking import LockBusy, ProcessLock
from scripts.verify_api_connections import _build_transport, _reject_constant, _strict_object, _valid_secret


PLAN_ID = "first-three-20261011-01"
EXPIRES_AT = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
ACTOR_ID = "dYOm3ceq9e9PBnyJA"
BUILD_ID = "8KqwGigmIDURqQlpj"
BUILD_NUMBER = "1.0.29"
TARGET = "thsottiaux"
TARGET_USER_ID = "1953337039510003712"
CONFIRMATION = "RUN THREE ONCE"
URL = (
    "https://api.apify.com/v2/actors/dYOm3ceq9e9PBnyJA/run-sync-get-dataset-items"
    "?build=1.0.29&maxTotalChargeUsd=0.0009&restartOnError=false&timeout=60"
    "&format=json&limit=3&offset=0&desc=false&clean=false&skipEmpty=false&skipHidden=false"
)
PAYLOAD = {"profiles": [TARGET], "maxResults": 3}
MAX_RESPONSE_BYTES = 1024 * 1024


class _CandidateRejected(ValueError):
    def __init__(self, candidate=None, *, code="SOURCE_TRIAL_CANDIDATE_REJECTED", diagnostics=None):
        self.candidate = candidate
        self.code = code
        self.diagnostics = dict(diagnostics or {})


class _Status(Exception):
    def __init__(self, code):
        self.code = code


def _now(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SOURCE_TRIAL_CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _default_directory():
    value = os.environ.get("LOCALAPPDATA", "")
    root = Path(value)
    if not value or not root.is_absolute() or str(root).startswith("\\\\"):
        raise ValueError("SOURCE_TRIAL_LOCAL_DIRECTORY_REQUIRED")
    return root / "CodexResetMonitor" / "source-setup-test"


def _path(directory, kind):
    return directory / (PLAN_ID + "." + kind + ".json")


def _consumed(directory):
    return any(path.exists() or path.is_symlink() for path in (
        _path(directory, "attempt"), _path(directory, "receipt"),
        _path(directory, "snapshot"), _path(directory, "candidate")))


def _reserve(directory, approved_at):
    record = {
        "schema_version": 1, "plan_id": PLAN_ID, "status": "ATTEMPT_RESERVED",
        "approved_at": approved_at.isoformat(), "expires_at": EXPIRES_AT.isoformat(),
        "method": "POST", "url": URL, "payload": PAYLOAD,
        "actor_id": ACTOR_ID, "requested_build_id": BUILD_ID, "requested_build_number": BUILD_NUMBER,
        "target_username": TARGET, "target_user_id": TARGET_USER_ID,
        "provider_run_cap_usd": "0.0009", "full_chain_hard_cap_proven": False,
        "max_attempts": 1, "reserved_attempts": 1,
        "approval_method": "local_tty_RUN_THREE_ONCE",
    }
    with _path(directory, "attempt").open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())


def _save_json(path, record):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _capture(token, transport, observed_at, *, diagnostics=None):
    diagnostics = {} if diagnostics is None else diagnostics

    def reject(code, candidate=None):
        raise _CandidateRejected(candidate, code=code, diagnostics=diagnostics)

    request = Request(URL, method="POST", data=json.dumps(PAYLOAD, separators=(",", ":")).encode("utf-8"),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "Accept": "application/json"})
    with transport(request, timeout=90) as response:
        code = response.getcode()
        if type(code) is int and 100 <= code <= 599:
            diagnostics["http_status"] = code
        if type(code) is not int or code != 201:
            raise _Status(code)
        try:
            totals = response.headers.get_all("X-Apify-Pagination-Total")
        except Exception:
            reject("SOURCE_TRIAL_TOTAL_HEADER_INVALID")
        if totals is None:
            totals = []
        if not isinstance(totals, list):
            reject("SOURCE_TRIAL_TOTAL_HEADER_INVALID")
        diagnostics["total_header_present"] = bool(totals)
        diagnostics["total_header_count"] = len(totals)
        if not totals:
            reject("SOURCE_TRIAL_TOTAL_HEADER_MISSING")
        if len(totals) != 1:
            reject("SOURCE_TRIAL_TOTAL_HEADER_MULTIPLE")
        if not isinstance(totals[0], str) or re.fullmatch(r"[0-9]{1,9}", totals[0]) is None:
            reject("SOURCE_TRIAL_TOTAL_HEADER_INVALID")
        total = int(totals[0])
        diagnostics["parsed_total"] = total
        if total > 3:
            reject("SOURCE_TRIAL_TOTAL_EXCEEDS_THREE")
        if totals[0] != str(total):
            reject("SOURCE_TRIAL_TOTAL_HEADER_INVALID")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    diagnostics["response_limit_bytes"] = MAX_RESPONSE_BYTES
    if not isinstance(body, bytes):
        reject("SOURCE_TRIAL_RESPONSE_BYTES_INVALID")
    diagnostics["response_bytes_read"] = len(body)
    if len(body) > MAX_RESPONSE_BYTES:
        reject("SOURCE_TRIAL_RESPONSE_TOO_LARGE")
    try:
        rows = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except Exception:
        diagnostics["json_valid"] = False
        reject("SOURCE_TRIAL_JSON_INVALID")
    diagnostics["json_valid"] = True
    diagnostics["json_root_type"] = {list: "array", dict: "object", str: "string", int: "number",
                                    float: "number", bool: "boolean", type(None): "null"}[type(rows)]
    if not isinstance(rows, list):
        reject("SOURCE_TRIAL_JSON_ROOT_INVALID")
    diagnostics["json_array_items"] = len(rows)
    if len(rows) != total or len(rows) > 3:
        reject("SOURCE_TRIAL_TOTAL_COUNT_MISMATCH")
    # Preserve bounded source evidence even when the assumed tweet schema
    # fails. It is explicitly unavailable to baseline or automatic analysis.
    candidate = {
        "schema_version": 1, "plan_id": PLAN_ID, "status": "REJECTED",
        "pagination_total": total, "candidate_rows": rows,
        "automatic_classification_allowed": False, "monitor_baseline_initialized": False,
        "production_source_contract_proven": False,
    }
    try:
        candidate_json = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
    except Exception:
        diagnostics["json_valid"] = False
        reject("SOURCE_TRIAL_JSON_INVALID")
    diagnostics["secret_reflection_detected"] = token in candidate_json
    if diagnostics["secret_reflection_detected"]:
        reject("SOURCE_TRIAL_TOKEN_REFLECTION")
    diagnostics["source_schema_accepted"] = False
    try:
        # Reuse the frozen source boundary without constructing a paid fetcher,
        # resolving a production contract, or writing any monitor state.
        boundary = SimpleNamespace(target=TARGET, target_user_id=TARGET_USER_ID, clock=observed_at)
        normalized = ApifyFetcher._normalize(boundary, rows)
    except Exception:
        reject("SOURCE_TRIAL_SOURCE_SCHEMA_REJECTED", candidate)
    if len(normalized) != len(rows):
        reject("SOURCE_TRIAL_CANONICAL_COUNT_MISMATCH", candidate)
    diagnostics["source_schema_accepted"] = True
    try:
        reply_flags = {row["tweetId"]: row["isReply"] for row in rows}
        for tweet in normalized:
            tweet["is_reply"] = reply_flags[tweet["id"]]
            tweet["is_retweet"] = False
        snapshot = {
            "schema_version": 1, "plan_id": PLAN_ID, "status": "CAPTURED" if rows else "EMPTY",
            "observed_at": observed_at().isoformat(), "actor_id": ACTOR_ID,
            "requested_build_id": BUILD_ID, "requested_build_number": BUILD_NUMBER,
            "target_username": TARGET, "target_user_id": TARGET_USER_ID,
            "pagination_total": total, "source_order_latest_first": [row["tweetId"] for row in rows],
            "canonical_order": "oldest_first", "tweets": normalized,
            "observed_author_identity": bool(rows), "monitor_baseline_initialized": False,
            "production_source_contract_proven": False, "reply_completeness_proven": False,
        }
        serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    except Exception:
        reject("SOURCE_TRIAL_CANONICAL_ENCODING_INVALID", candidate)
    if token in serialized:
        diagnostics["secret_reflection_detected"] = True
        reject("SOURCE_TRIAL_TOKEN_REFLECTION", candidate)
    return snapshot, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _preview(output):
    print("CANDIDATE SOURCE TRIAL - AT MOST ONE ACTOR RUN", file=output)
    print("POST " + URL, file=output)
    print('Body: {"profiles":["thsottiaux"],"maxResults":3}', file=output)
    print("Latest-first source delivery; require full Dataset total 0..3; never truncate locally.", file=output)
    print("Observed FREE plan with USD 10 existing credit; confirm still current before approving.", file=output)
    print("Run charge cap USD 0.0009; export/transfer/retention may also consume FREE credit.", file=output)
    print("USD 0.0009 is not a proven full-chain hard ceiling. No recharge or paid upgrade.", file=output)
    print("No retries, redirects, model calls, emails, or scheduling; direct HTTPS only.", file=output)
    print("Socket timeout 90s; Actor timeout 60s; no total wall-clock deadline claimed.", file=output)
    print("Separate candidate snapshot; does not initialize monitor baseline or production proofs.", file=output)
    print("Expires: 2026-10-12 16:00 Asia/Shanghai", file=output)


def main(argv=None, *, output=None, hidden_input=None, confirm=None, clock=None, directory=None, transport=None):
    """Injected dependencies are for offline tests, never CLI switches."""
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--preview"], ["--help"], ["--capture-one-run"]):
        print("SOURCE_TRIAL_ARGUMENTS_INVALID", file=output)
        return 2
    _preview(output)
    if argv != ["--capture-one-run"]:
        print("PREVIEW_ONLY", file=output)
        return 0
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        if _now(clock) >= EXPIRES_AT:
            print("SOURCE_TRIAL_PLAN_EXPIRED", file=output)
            return 2
        directory = _default_directory() if directory is None else Path(directory)
        if _consumed(directory):
            print("SOURCE_TRIAL_ALREADY_RESERVED", file=output)
            return 2
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            print("SOURCE_TRIAL_INTERACTIVE_TERMINAL_REQUIRED", file=output)
            return 2
        confirm = input if confirm is None else confirm
        if confirm("Authorize this single candidate run: type RUN THREE ONCE: ") != CONFIRMATION:
            print("SOURCE_TRIAL_CANCELLED", file=output)
            return 2
        hidden_input = getpass.getpass if hidden_input is None else hidden_input
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = hidden_input("Apify project token (hidden): ")
        if not _valid_secret(token):
            print("SOURCE_TRIAL_TOKEN_INVALID", file=output)
            return 2
        transport = _build_transport() if transport is None else transport
        with ProcessLock(directory / (PLAN_ID + ".lock")):
            if _consumed(directory):
                print("SOURCE_TRIAL_ALREADY_RESERVED", file=output)
                return 2
            approved_at = _now(clock)
            if approved_at >= EXPIRES_AT:
                print("SOURCE_TRIAL_PLAN_EXPIRED", file=output)
                return 2
            _reserve(directory, approved_at)
            status, error_code, snapshot, digest, rejected_candidate = "UNKNOWN", None, None, None, None
            diagnostics = {}
            try:
                if _now(clock) >= EXPIRES_AT:
                    status, error_code = "FAILED", "SOURCE_TRIAL_EXPIRED_AFTER_RESERVATION"
                else:
                    snapshot, digest = _capture(token, transport, lambda: _now(clock), diagnostics=diagnostics)
                    status = snapshot["status"]
            except (HTTPError, _Status) as error:
                code = error.code
                status = "FAILED" if type(code) is int and 400 <= code <= 499 and code != 408 else "UNKNOWN"
                error_code = "SOURCE_TRIAL_HTTP_" + str(code) if type(code) is int and 100 <= code <= 599 else "SOURCE_TRIAL_HTTP_ERROR"
                if isinstance(error, HTTPError):
                    try:
                        error.close()
                    except Exception:
                        pass
            except _CandidateRejected as error:
                status, error_code = "FAILED", error.code
                rejected_candidate = error.candidate
                diagnostics = error.diagnostics
            except BaseException:
                status, error_code = "UNKNOWN", "SOURCE_TRIAL_REQUEST_UNCERTAIN"
            try:
                if snapshot is not None:
                    _save_json(_path(directory, "snapshot"), snapshot)
                if rejected_candidate is not None:
                    _save_json(_path(directory, "candidate"), rejected_candidate)
                _save_json(_path(directory, "receipt"), {
                    "schema_version": 1, "plan_id": PLAN_ID, "status": status, "error_code": error_code,
                    "completed_at": _now(clock).isoformat(), "attempts": 1,
                    "captured_items": len(snapshot["tweets"]) if snapshot is not None else 0,
                    "snapshot_sha256": digest,
                    "rejected_candidate_saved": rejected_candidate is not None,
                    "diagnostics": diagnostics,
                    "production_source_contract_proven": False, "monitor_baseline_initialized": False,
                })
            except BaseException:
                print("SOURCE_TRIAL_SAVE_FAILED_RESERVATION_KEPT", file=output)
                return 1
            if status in {"CAPTURED", "EMPTY"}:
                print(status + " n=" + str(len(snapshot["tweets"])), file=output)
                return 0
            print(status + " " + error_code, file=output)
            return 1
    except LockBusy:
        print("SOURCE_TRIAL_BUSY", file=output)
        return 2
    except getpass.GetPassWarning:
        print("SOURCE_TRIAL_HIDDEN_INPUT_REQUIRED", file=output)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("SOURCE_TRIAL_CANCELLED", file=output)
        return 2
    except Exception:
        print("SOURCE_TRIAL_LOCAL_ERROR", file=output)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
