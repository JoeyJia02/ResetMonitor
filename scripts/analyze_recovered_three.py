"""One manually approved batch classification of the fixed recovered sample."""

from datetime import datetime, timezone
from decimal import Decimal
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.locking import LockBusy, ProcessLock
from monitor.models import parse_aware_iso
from monitor.model_payload import PayloadError, pack_model_tweets
from monitor.classification_prompt import SYSTEM_PROMPT
from monitor.classifier import ClassificationError, validate_batch_results
from monitor.manual_model_budget import ManualModelBudget, ModelBudgetError
from scripts.verify_api_connections import _build_transport, _reject_constant, _strict_object, _valid_secret


PLAN_ID = "classification-three-20261011-01"
SAMPLE_PATH = Path(__file__).resolve().parents[1] / "evidence" / "first-source-recovered-sample-20261011.json"
SAMPLE_SHA256 = "97492395b7f8695e73aa6bc394f5e1469bbd9345ddd61d3132d175fda27e690b"
INPUT_IDS = ("2109090080497508356", "2109132167112782291", "2109152716954222849")
TARGET_USER_ID = "1953337039510003712"
URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
PRICE_VERIFIED_AT = datetime(2026, 10, 11, 8, 4, tzinfo=timezone.utc)
EXPIRES_AT = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
CONTEXT_LIMIT = 1_048_576
MAX_OUTPUT_TOKENS = 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
INPUT_CNY_PER_MILLION = Decimal("2")
OUTPUT_CNY_PER_MILLION = Decimal("8")
RESERVED_MAXIMUM_CNY = Decimal("2.105344")
CONFIRMATION = "ANALYZE THREE ONCE"



class _LocalFailure(ValueError):
    def __init__(self, code):
        self.code = code


class _HTTPStatus(Exception):
    def __init__(self, code):
        self.code = code


def _now(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _LocalFailure("CLASSIFICATION_CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _active(now):
    return PRICE_VERIFIED_AT <= now < EXPIRES_AT


def _strict_json(value):
    return json.loads(value, object_pairs_hook=_strict_object, parse_constant=_reject_constant)


def _read_sample():
    with SAMPLE_PATH.open("rb") as handle:
        return handle.read(MAX_RESPONSE_BYTES + 1)


def _load_sample(loader):
    raw = loader()
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise _LocalFailure("CLASSIFICATION_SAMPLE_INVALID")
    if hashlib.sha256(raw).hexdigest() != SAMPLE_SHA256:
        raise _LocalFailure("CLASSIFICATION_SAMPLE_HASH_MISMATCH")
    try:
        sample = _strict_json(raw.decode("utf-8"))
        if (not isinstance(sample, dict) or type(sample.get("schema_version")) is not int
                or sample["schema_version"] != 1 or sample.get("status") != "SAMPLE"
                or not isinstance(sample.get("tweets"), list) or len(sample["tweets"]) != 3):
            raise ValueError()
        tweets, ids = [], set()
        for row in sample["tweets"]:
            if (not isinstance(row, dict) or row.get("id") not in INPUT_IDS or row["id"] in ids
                    or row.get("author_id") != TARGET_USER_ID or row.get("author") != "thsottiaux"
                    or not isinstance(row.get("text"), str) or not row["text"].strip()
                    or not isinstance(row.get("context"), dict) or not isinstance(row.get("url"), str)):
                raise ValueError()
            parse_aware_iso(row.get("created_at"))
            ids.add(row["id"])
            tweets.append({key: row[key] for key in ("id", "author", "author_id", "text", "created_at", "url", "context")})
        if ids != set(INPUT_IDS):
            raise ValueError()
        return sorted(tweets, key=lambda row: INPUT_IDS.index(row["id"]))
    except Exception:
        raise _LocalFailure("CLASSIFICATION_SAMPLE_INVALID") from None


def _pack(tweets):
    try:
        return pack_model_tweets(tweets)
    except PayloadError as error:
        raise _LocalFailure(error.code) from None


def _restore_ids(results, index_to_id):
    return [{**row, "id": index_to_id[row["id"]]} for row in results]


def _request_body(tweets, now):
    packed, _ = _pack(tweets)
    value = {
        "model": MODEL, "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
        "stream": False, "temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps({
            "observed_at": now.isoformat(), "timezone": "Asia/Shanghai", "tweets": packed,
        }, ensure_ascii=False, allow_nan=False)}],
    }
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise _LocalFailure("CLASSIFICATION_INPUT_TOO_LARGE")
    return body


def _directory():
    value = os.environ.get("LOCALAPPDATA", "")
    root = Path(value)
    if not value or not root.is_absolute() or str(root).startswith("\\\\"):
        raise _LocalFailure("CLASSIFICATION_LOCAL_DIRECTORY_REQUIRED")
    return root / "CodexResetMonitor" / "classification-setup-test"


def _path(directory, kind):
    return directory / (PLAN_ID + "." + kind + ".json")


def _consumed(directory):
    return any(path.exists() or path.is_symlink() for path in (
        _path(directory, "attempt"), _path(directory, "receipt"), _path(directory, "result"),
        _path(directory, "candidate")))


def _save(path, record, *, exclusive=False):
    temporary = path if exclusive else path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        if not exclusive:
            os.replace(temporary, path)
    finally:
        if not exclusive:
            temporary.unlink(missing_ok=True)


def _analyze(key, body, transport, evidence, candidate, *, input_ids=None, model_budget=None):
    expected_ids = INPUT_IDS if input_ids is None else tuple(input_ids)
    if not 1 <= len(expected_ids) <= 3 or len(set(expected_ids)) != len(expected_ids):
        raise _LocalFailure("CLASSIFICATION_INPUT_IDS_INVALID")
    diagnostics = evidence.setdefault("diagnostics", {})
    request = Request(URL, method="POST", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
    budget = ManualModelBudget() if model_budget is None else model_budget
    try:
        budget.reserve(RESERVED_MAXIMUM_CNY, request_sha256=hashlib.sha256(body).hexdigest())
    except ModelBudgetError as error:
        raise _LocalFailure(error.code) from None
    with transport(request, timeout=60) as response:
        code = response.getcode()
        if type(code) is not int or code != 200:
            raise _HTTPStatus(code)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(raw, bytes):
        raise _LocalFailure("CLASSIFICATION_RESPONSE_BYTES_INVALID")
    diagnostics["response_bytes_read"] = len(raw)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise _LocalFailure("CLASSIFICATION_RESPONSE_TOO_LARGE")
    try:
        document = _strict_json(raw.decode("utf-8"))
    except Exception:
        raise _LocalFailure("CLASSIFICATION_RESPONSE_JSON_INVALID") from None
    diagnostics["response_json_object"] = isinstance(document, dict)
    if not isinstance(document, dict):
        raise _LocalFailure("CLASSIFICATION_RESPONSE_ROOT_INVALID")
    choices = document.get("choices")
    diagnostics["choices_count"] = len(choices) if isinstance(choices, list) else None
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get("message"), dict)
            or choices[0]["message"].get("role") != "assistant"
            or not isinstance(choices[0]["message"].get("content"), str)):
        raise _LocalFailure("CLASSIFICATION_CHOICE_INVALID")
    message_content = choices[0]["message"]["content"]
    try:
        content_bytes = message_content.encode("utf-8")
    except Exception:
        raise _LocalFailure("CLASSIFICATION_CONTENT_ENCODING_INVALID") from None
    diagnostics["assistant_content_bytes"] = len(content_bytes)
    if len(content_bytes) > MAX_REQUEST_BYTES:
        raise _LocalFailure("CLASSIFICATION_CONTENT_TOO_LARGE")
    try:
        content = _strict_json(message_content)
        canonical_content = json.dumps(content, ensure_ascii=False, allow_nan=False)
    except Exception:
        raise _LocalFailure("CLASSIFICATION_CONTENT_JSON_INVALID") from None
    if key in message_content or key in canonical_content:
        raise _LocalFailure("CLASSIFICATION_CONTENT_TOKEN_REFLECTION")
    # Only bounded, strict JSON assistant output may be kept for manual review.
    # Never retain arbitrary provider response fields, headers, or exceptions.
    candidate["message_content"] = message_content
    if choices[0].get("finish_reason") != "stop":
        raise _LocalFailure("CLASSIFICATION_FINISH_INVALID")
    try:
        usage = document["usage"]
        counts = {name: usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if (any(type(value) is not int or value < 0 for value in counts.values())
                or counts["prompt_tokens"] > CONTEXT_LIMIT or counts["completion_tokens"] > MAX_OUTPUT_TOKENS
                or counts["total_tokens"] != counts["prompt_tokens"] + counts["completion_tokens"]
                or counts["total_tokens"] > CONTEXT_LIMIT):
            raise ValueError()
        cost = (Decimal(counts["prompt_tokens"]) * INPUT_CNY_PER_MILLION
                + Decimal(counts["completion_tokens"]) * OUTPUT_CNY_PER_MILLION) / Decimal(1_000_000)
        if cost > RESERVED_MAXIMUM_CNY:
            raise ValueError()
        evidence.update(usage=counts, usage_cost_upper_bound_cny=str(cost))
    except Exception:
        raise _LocalFailure("CLASSIFICATION_USAGE_INVALID") from None
    diagnostics["results_count"] = len(content.get("results")) if isinstance(content, dict) and isinstance(content.get("results"), list) else None
    try:
        return validate_batch_results(content, expected_ids)
    except ClassificationError as error:
        raise _LocalFailure(str(error)) from None



def _preview(output):
    print("INDEPENDENT THREE-ITEM BATCH CLASSIFICATION CANDIDATE", file=output)
    print("Sample: " + str(SAMPLE_PATH), file=output)
    print("SHA256: " + SAMPLE_SHA256, file=output)
    print("One POST " + URL + "; model deepseek-flash; thinking disabled; max output 1024 tokens.", file=output)
    print("All three complete texts and contexts; no keyword prefilter or truncation; request <=16KiB.", file=output)
    print("Reserve CNY 2.105344 (display CNY 2.11): very conservative upper bound, not expected cost.", file=output)
    print("Authorized cumulative budget: CNY 1.00; this larger reservation is refused before IO.", file=output)
    print("Short sample text should cost much less; usage will supply the uncached-price cost upper bound.", file=output)
    print("Billing bound: 1048576 input tokens at CNY 2/million, 1024 output at CNY 8/million.", file=output)
    print("Context billing bound and byte limit do not prove tokenizer admission or production token bounds.", file=output)
    print("No retry, redirect, proxy, new source run, email, baseline, or schedule.", file=output)
    print("Expires: 2026-10-12 16:00 Asia/Shanghai; socket timeout 60s is not a total runtime deadline.", file=output)


def main(argv=None, *, output=None, hidden_input=None, confirm=None, clock=None, directory=None, transport=None, sample_loader=None, model_budget=None):
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--preview"], ["--help"], ["--analyze-one-batch"]):
        print("CLASSIFICATION_ARGUMENTS_INVALID", file=output)
        return 2
    _preview(output)
    if argv != ["--analyze-one-batch"]:
        print("PREVIEW_ONLY", file=output)
        return 0
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        if not _active(_now(clock)):
            raise _LocalFailure("CLASSIFICATION_PLAN_INACTIVE")
        directory = _directory() if directory is None else Path(directory)
        if _consumed(directory):
            raise _LocalFailure("CLASSIFICATION_ALREADY_RESERVED")
        budget = ManualModelBudget() if model_budget is None else model_budget
        try:
            budget.preflight(RESERVED_MAXIMUM_CNY)
        except ModelBudgetError as error:
            raise _LocalFailure(error.code) from None
        tweets = _load_sample(_read_sample if sample_loader is None else sample_loader)
        packed, index_to_id = _pack(tweets)
        body = _request_body(tweets, _now(clock))
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise _LocalFailure("CLASSIFICATION_INTERACTIVE_TERMINAL_REQUIRED")
        confirm = input if confirm is None else confirm
        if confirm("Approve one batch and CNY 2.105344 upper bound: type ANALYZE THREE ONCE: ") != CONFIRMATION:
            raise _LocalFailure("CLASSIFICATION_CANCELLED")
        hidden_input = getpass.getpass if hidden_input is None else hidden_input
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = hidden_input("DeepSeek project API key (hidden): ")
        if not _valid_secret(key):
            raise _LocalFailure("CLASSIFICATION_KEY_INVALID")
        transport = _build_transport() if transport is None else transport
        with ProcessLock(directory / (PLAN_ID + ".lock")):
            if _consumed(directory):
                raise _LocalFailure("CLASSIFICATION_ALREADY_RESERVED")
            approved_at = _now(clock)
            if not _active(approved_at):
                raise _LocalFailure("CLASSIFICATION_PLAN_INACTIVE")
            _save(_path(directory, "attempt"), {
                "schema_version": 1, "plan_id": PLAN_ID, "status": "ATTEMPT_RESERVED", "attempts": 1,
                "approved_at": approved_at.isoformat(), "expires_at": EXPIRES_AT.isoformat(),
                "sample_sha256": SAMPLE_SHA256, "request_body_sha256": hashlib.sha256(body).hexdigest(),
                "url": URL, "model": MODEL, "batch_items": 3, "maximum_requests": 1,
                "reserved_maximum_cny": str(RESERVED_MAXIMUM_CNY), "price_verified_at": PRICE_VERIFIED_AT.isoformat(),
                "context_billing_bound": CONTEXT_LIMIT, "maximum_output_tokens": MAX_OUTPUT_TOKENS,
                "tokenizer_admission_proven": False, "production_token_bound_proven": False,
            }, exclusive=True)
            status, code, results, evidence, candidate = "UNKNOWN", None, None, {}, {}
            try:
                if not _active(_now(clock)):
                    status, code = "FAILED", "CLASSIFICATION_EXPIRED_AFTER_RESERVATION"
                else:
                    results = _analyze(key, body, transport, evidence, candidate,
                                       input_ids=[row["id"] for row in packed], model_budget=budget)
                    results = _restore_ids(results, index_to_id)
                    status = "BATCH_ANALYZED"
            except (HTTPError, _HTTPStatus) as error:
                value = error.code
                status = "FAILED" if type(value) is int and 400 <= value <= 499 and value != 408 else "UNKNOWN"
                code = "CLASSIFICATION_HTTP_" + str(value) if type(value) is int and 100 <= value <= 599 else "CLASSIFICATION_HTTP_ERROR"
                if isinstance(error, HTTPError):
                    try:
                        error.close()
                    except Exception:
                        pass
            except _LocalFailure as error:
                status, code = "FAILED", error.code
            except BaseException:
                status, code = "UNKNOWN", "CLASSIFICATION_REQUEST_UNCERTAIN"
            try:
                if results is not None:
                    _save(_path(directory, "result"), {
                        "schema_version": 1, "plan_id": PLAN_ID, "status": status, "sample_sha256": SAMPLE_SHA256,
                        "results": results, **evidence, "source_current_three_user_verified": True,
                        "monitor_baseline_initialized": False, "notification_sent": False,
                    })
                elif candidate:
                    _save(_path(directory, "candidate"), {
                        "schema_version": 1, "plan_id": PLAN_ID, "status": "REJECTED",
                        "sample_sha256": SAMPLE_SHA256, **candidate, **evidence,
                        "automatic_notification_allowed": False, "monitor_baseline_initialized": False,
                    })
                _save(_path(directory, "receipt"), {
                    "schema_version": 1, "plan_id": PLAN_ID, "status": status, "error_code": code, "attempts": 1,
                    "completed_at": _now(clock).isoformat(), "reserved_maximum_cny": str(RESERVED_MAXIMUM_CNY),
                    "result_items": len(results) if results is not None else 0, **evidence,
                    "rejected_candidate_saved": results is None and bool(candidate),
                })
            except BaseException:
                print("CLASSIFICATION_SAVE_FAILED_RESERVATION_KEPT", file=output)
                return 1
            print("BATCH_ANALYZED n=3" if results is not None else status + " " + code, file=output)
            return 0 if results is not None else 1
    except LockBusy:
        print("CLASSIFICATION_BUSY", file=output)
        return 2
    except getpass.GetPassWarning:
        print("CLASSIFICATION_HIDDEN_INPUT_REQUIRED", file=output)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("CLASSIFICATION_CANCELLED", file=output)
        return 2
    except _LocalFailure as error:
        print(error.code, file=output)
        return 2
    except Exception:
        print("CLASSIFICATION_LOCAL_ERROR", file=output)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
