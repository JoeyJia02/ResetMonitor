"""DeepSeek classifier with an explicit transport and no environment or retry IO.

Request contract verified against the official API, JSON Output and Thinking
Mode documentation on 2026-10-10. Usage evidence is bounded, in-memory provider
metadata; it is neither a billing ledger nor proof of a real charge.
"""

from collections import deque
from datetime import datetime, timezone as datetime_timezone
from email.utils import parsedate_to_datetime
import inspect
import json
import math
import re
import time

from .classifier import ClassifierReply, build_prompt, parse_batch_classifications
from .http_transport import HttpResponse, HttpTransport
from .models import StageFailure


_URL = "https://api.deepseek.com/chat/completions"
_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
_TOKEN_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
)
_MAX_RESPONSE_BYTES = 1_048_576
_HTTP_DATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT\Z"
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_RESPONSE_KEY")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("NONFINITE_RESPONSE_NUMBER")


def _token_count(value):
    return type(value) is int and value >= 0


def _retry_after(headers, now):
    """Read one unambiguous delta-seconds or IMF-fixdate header, without IO."""
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
            parsed = parsedate_to_datetime(text)
            seconds = max(0.0, (parsed - now.astimezone(datetime_timezone.utc)).total_seconds())
        else:
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _usage(value):
    if not isinstance(value, dict) or any(not _token_count(value.get(key)) for key in _TOKEN_FIELDS):
        raise ValueError("INVALID_TOKEN_USAGE")
    result = {key: value[key] for key in _TOKEN_FIELDS}
    if result["prompt_tokens"] != result["prompt_cache_hit_tokens"] + result["prompt_cache_miss_tokens"]:
        raise ValueError("INCONSISTENT_TOKEN_USAGE")
    if result["total_tokens"] != result["prompt_tokens"] + result["completion_tokens"]:
        raise ValueError("INCONSISTENT_TOKEN_USAGE")
    if "prompt_tokens_details" in value:
        details = value["prompt_tokens_details"]
        if not isinstance(details, dict):
            raise ValueError("INVALID_TOKEN_USAGE")
        if "cached_tokens" in details and (
            not _token_count(details["cached_tokens"])
            or details["cached_tokens"] != result["prompt_cache_hit_tokens"]
        ):
            raise ValueError("INCONSISTENT_TOKEN_USAGE")
    if "completion_tokens_details" in value:
        details = value["completion_tokens_details"]
        if not isinstance(details, dict):
            raise ValueError("INVALID_TOKEN_USAGE")
        if "reasoning_tokens" in details:
            count = details["reasoning_tokens"]
            if not _token_count(count) or count > result["completion_tokens"]:
                raise ValueError("INVALID_TOKEN_USAGE")
            result["reasoning_tokens"] = count
    return result


class DeepSeekClassifier:
    """Perform one bounded chat-completions request per classification call.

    A caller must supply both the credential and transport. This module does not
    choose a network implementation, read environment variables, or retry calls.
    """

    def __init__(self, api_key: str, transport: HttpTransport, *, model="deepseek-flash", timeout=20,
                 monotonic=time.monotonic):
        if not isinstance(api_key, str) or not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
            raise ValueError("INVALID_API_KEY")
        if transport is None or not callable(getattr(transport, "request", None)):
            raise ValueError("HTTP_TRANSPORT_REQUIRED")
        if not isinstance(model, str) or model not in _MODELS:
            raise ValueError("UNSUPPORTED_DEEPSEEK_MODEL")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("INVALID_HTTP_TIMEOUT")
        try:
            timeout = float(timeout)
        except (OverflowError, ValueError):
            raise ValueError("INVALID_HTTP_TIMEOUT") from None
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("INVALID_HTTP_TIMEOUT")
        if not callable(monotonic):
            raise ValueError("INVALID_MONOTONIC_CLOCK")
        self._api_key = api_key
        self._transport = transport
        self.model = model
        self.timeout = timeout
        self._monotonic = monotonic
        self._last_usage = None
        self._usage_history = deque(maxlen=128)

    @property
    def last_usage(self) -> dict | None:
        """Validated token metadata for the latest call, or unknown (None)."""
        return dict(self._last_usage) if self._last_usage is not None else None

    @property
    def usage_history(self) -> list[dict]:
        """Detached evidence from the most recent 128 responses with valid usage."""
        return [dict(item) for item in self._usage_history]

    def classify(self, tweet: dict, *, now: datetime, timezone: str) -> ClassifierReply:
        return self.classify_with_deadline(
            tweet, now=now, timezone=timezone, deadline=self._monotonic() + self.timeout,
        )

    def classify_with_deadline(
        self, tweet: dict, *, now: datetime, timezone: str, deadline: float,
    ) -> ClassifierReply:
        """Use a shared engine deadline; never extend the per-call timeout.

        A transport must declare the deadline keyword contract. Arbitrary custom
        implementations must honor that contract; StdlibHTTPTransport is the
        locally verified implementation with cancellation and bounded waiting.
        """
        self._last_usage = None
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise StageFailure(retryable=False)
        deadline = min(deadline, self._monotonic() + self.timeout)
        def check():
            if self._monotonic() >= deadline:
                raise StageFailure(retryable=True)
        check()
        try:
            parameters = inspect.signature(self._transport.request).parameters
            parameter = parameters.get("deadline")
            accepts_deadline = (
                parameter is not None
                and parameter.kind in {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            ) or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        except (TypeError, ValueError):
            accepts_deadline = False
        if not accepts_deadline:
            # A legacy transport cannot silently downgrade the total budget to
            # a per-socket timeout. Fail before making any external request.
            raise StageFailure(retryable=False)
        try:
            messages = build_prompt(tweet, now, timezone)
            payload = {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                "temperature": 0,
                "max_tokens": 256,
                "stream": False,
            }
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except Exception:
            raise StageFailure(retryable=False) from None
        check()
        try:
            response = self._transport.request(
                "POST", _URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                body=body, timeout=self.timeout, deadline=deadline,
            )
        except StageFailure as exc:
            raise StageFailure(retryable=exc.retryable, retry_after_seconds=exc.retry_after_seconds) from None
        except Exception:
            raise StageFailure(retryable=True) from None

        check()
        if not isinstance(response, HttpResponse) or type(response.status) is not int:
            raise StageFailure(retryable=True)
        if response.status != 200:
            retryable = response.status in {408, 429} or 500 <= response.status <= 599
            retry_after = _retry_after(response.headers, now) if response.status in {429, 503} else None
            raise StageFailure(retryable=retryable, retry_after_seconds=retry_after)
        try:
            if isinstance(response.body, bytes) and len(response.body) > _MAX_RESPONSE_BYTES:
                raise StageFailure(retryable=False)
            if not isinstance(response.body, bytes) or not 0 < len(response.body) <= _MAX_RESPONSE_BYTES:
                raise ValueError("INVALID_RESPONSE_BODY")
            value = json.loads(
                response.body.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(value, dict):
                raise ValueError("INVALID_RESPONSE_OBJECT")
            if "error" in value:
                # An error envelope is not a completion. Do not echo its details
                # or guess retryability from untyped provider error strings.
                raise StageFailure(retryable=False)
            if value.get("object") != "chat.completion":
                raise ValueError("INVALID_COMPLETION_OBJECT")
            if not isinstance(value.get("id"), str) or not value["id"]:
                raise ValueError("MISSING_COMPLETION_ID")
            if not _token_count(value.get("created")):
                raise ValueError("INVALID_COMPLETION_TIME")
            if not isinstance(value.get("model"), str) or not value["model"]:
                raise ValueError("MISSING_COMPLETION_MODEL")
            # Usage is optional in the envelope. Missing evidence stays unknown;
            # if provided it must be internally consistent, never inferred.
            if "usage" in value:
                self._last_usage = _usage(value["usage"])
                self._usage_history.append(dict(self._last_usage))
            choices = value.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("INVALID_COMPLETION_CHOICES")
            choice = choices[0]
            if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
                raise ValueError("INVALID_COMPLETION_CHOICE")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("INVALID_COMPLETION_MESSAGE")
            if message.get("tool_calls") not in (None, []):
                raise ValueError("UNEXPECTED_TOOL_CALL")
            reply = ClassifierReply(message.get("content"), choice.get("finish_reason"))
            parse_batch_classifications(reply, ("1",))
            check()
            return reply
        except StageFailure as exc:
            raise StageFailure(retryable=exc.retryable, retry_after_seconds=exc.retry_after_seconds) from None
        except Exception:
            # Includes invalid UTF-8/JSON, incomplete generation and schema drift.
            # The engine owns the bounded retry budget; this adapter never loops.
            raise StageFailure(retryable=True) from None
