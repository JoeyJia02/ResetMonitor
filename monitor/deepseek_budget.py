"""Offline-reviewed DeepSeek prices and a fail-closed complete-input gate.

Prices were read from the official English and Chinese pricing pages on
2026-10-10. Currency tables are independent; no exchange rate is inferred.
The official character/token ratios are estimates, so they are deliberately
not implemented as a token bound. No complete API framing/tokenizer proof has
been verified for this project: the production proof registry is EMPTY.

An explicitly injected proof is a reviewed Python capability, never an
authorization-file boolean or an automatically discovered tokenizer. Tests use
artificial proof functions and do not establish a real provider contract.
Reservations are never refunded, including exceptions or absent usage.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import json
import hashlib
import re
from types import MappingProxyType
from typing import Callable, Mapping

from .http_transport import HttpTransport
from .models import StageFailure


PRICE_SNAPSHOT_ID = "deepseek-pricing-2026-10-10"
PRICE_VERIFIED_AT = datetime(2026, 10, 10, 12, 24, 37, tzinfo=timezone.utc)
PRICE_EXPIRES_AT = PRICE_VERIFIED_AT + timedelta(hours=24)
PRICE_SOURCE_USD = "https://api-docs.deepseek.com/quick_start/pricing/"
PRICE_SOURCE_CNY = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
TOKEN_USAGE_SOURCE = "https://api-docs.deepseek.com/quick_start/token_usage/"
REQUEST_SOURCE = "https://api-docs.deepseek.com/api/create-chat-completion/"
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_TOKENS = 256
# A conservative admission limit below either interpretation of official 1M.
# It is not used as a substitute for a complete-input tokenizer proof.
CONTEXT_ADMISSION_LIMIT = 1_000_000
_URL = "https://api.deepseek.com/chat/completions"
_MILLION = Decimal("1000000")
_AMOUNT_QUANTUM = Decimal("0.00000001")
_RATES = MappingProxyType({
    ("deepseek-flash", "USD"): (Decimal("0.30"), Decimal("1.20")),
    ("deepseek-v4-pro", "USD"): (Decimal("1.32"), Decimal("3.96")),
    ("deepseek-flash", "CNY"): (Decimal("2"), Decimal("8")),
    ("deepseek-v4-pro", "CNY"): (Decimal("9"), Decimal("27")),
})
_MODELS = frozenset(model for model, _currency in _RATES)
_FIELDS = frozenset({
    "model", "messages", "response_format", "thinking", "temperature",
    "max_tokens", "stream",
})


class DeepSeekBudgetError(StageFailure):
    """A permanent refusal represented only by a fixed, non-secret code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(retryable=False)
        self.args = (code,)


@dataclass(frozen=True)
class TokenBoundProof:
    """Explicit reviewed full-request bound capability.

    ``bounder`` must bound all input tokens, including server chat framing and
    the requested response format, for this exact model and request schema.
    Merely wrapping an approximate character counter does not satisfy that
    contract. Production code must register only independently reviewed proof
    implementations. JSON/CLI inputs cannot construct or register one.
    """

    proof_id: str
    model: str
    verified_at: datetime
    expires_at: datetime
    bounder: Callable[[dict], int] = field(repr=False, compare=False)


DEFAULT_TOKEN_PROOFS: Mapping[str, TokenBoundProof] = MappingProxyType({})


@dataclass(frozen=True)
class DeepSeekQuote:
    model: str
    currency: str
    input_tokens_upper_bound: int
    max_output_tokens: int
    reserved_amount: Decimal
    price_snapshot_id: str
    token_proof_id: str


def _aware(value: object) -> bool:
    try:
        return (isinstance(value, datetime) and value.tzinfo is not None
                and value.utcoffset() is not None)
    except (ValueError, TypeError, OverflowError):
        return False


def _proof_valid(proof: object, model: str, now: datetime) -> bool:
    return (isinstance(proof, TokenBoundProof) and proof.model == model
            and isinstance(proof.proof_id, str)
            and re.fullmatch(r"tokenproof-[a-z0-9-]{1,64}", proof.proof_id) is not None
            and _aware(proof.verified_at) and _aware(proof.expires_at)
            and proof.verified_at <= now < proof.expires_at
            and timedelta(0) < proof.expires_at - proof.verified_at <= timedelta(hours=24)
            and callable(proof.bounder))


def pricing_issue_codes(model: str, currency: str, *, now: datetime,
                        token_proofs: Mapping[str, TokenBoundProof] | None = None) -> tuple[str, ...]:
    """Pure preflight checks; never read files, secrets, clocks or endpoints."""
    issues = []
    if not isinstance(model, str) or model not in _MODELS:
        issues.append("DEEPSEEK_PRICED_MODEL_UNSUPPORTED")
    if not isinstance(currency, str) or currency not in {"USD", "CNY"}:
        issues.append("DEEPSEEK_CURRENCY_UNSUPPORTED")
    if not _aware(now):
        issues.append("DEEPSEEK_AWARE_PRICE_CLOCK_REQUIRED")
    elif not PRICE_VERIFIED_AT <= now < PRICE_EXPIRES_AT:
        issues.append("DEEPSEEK_PRICE_SNAPSHOT_STALE")
    registry = DEFAULT_TOKEN_PROOFS if token_proofs is None else token_proofs
    if not isinstance(registry, Mapping):
        issues.append("DEEPSEEK_TOKEN_PROOF_REGISTRY_INVALID")
    elif isinstance(model, str):
        proof = registry.get(model)
        if proof is None:
            issues.append("DEEPSEEK_TOKEN_BOUND_PROOF_UNAVAILABLE")
        elif _aware(now) and not _proof_valid(proof, model, now):
            issues.append("DEEPSEEK_TOKEN_BOUND_PROOF_INVALID")
    return tuple(issues)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError


def _payload(body: bytes) -> dict:
    try:
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_REQUEST_BYTES:
            raise ValueError
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError
        if (not isinstance(value["model"], str) or value["model"] not in _MODELS
                or value["response_format"] != {"type": "json_object"}
                or value["thinking"] != {"type": "disabled"}
                or type(value["temperature"]) not in (int, float) or value["temperature"] != 0
                or type(value["max_tokens"]) is not int or value["max_tokens"] != MAX_OUTPUT_TOKENS
                or value["stream"] is not False):
            raise ValueError
        messages = value["messages"]
        if not isinstance(messages, list) or len(messages) != 2:
            raise ValueError
        for message, role in zip(messages, ("system", "user")):
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] != role or not isinstance(message["content"], str)
                    or not message["content"]):
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        raise DeepSeekBudgetError("DEEPSEEK_FULL_REQUEST_INVALID_OR_TOO_LARGE") from None


def quote_request(body: bytes, *, currency: str, now: datetime,
                  token_proofs: Mapping[str, TokenBoundProof] | None = None) -> DeepSeekQuote:
    """Quote the complete body without truncation or semantic prefiltering."""
    payload = _payload(body)
    model = payload["model"]
    issues = pricing_issue_codes(model, currency, now=now, token_proofs=token_proofs)
    if issues:
        raise DeepSeekBudgetError(issues[0])
    registry = DEFAULT_TOKEN_PROOFS if token_proofs is None else token_proofs
    proof = registry[model]
    try:
        # A proof cannot mutate the actual outgoing bytes or the parsed payload.
        count = proof.bounder(deepcopy(payload))
    except Exception:
        raise DeepSeekBudgetError("DEEPSEEK_TOKEN_BOUND_FAILED") from None
    if type(count) is not int or not 0 < count <= CONTEXT_ADMISSION_LIMIT - MAX_OUTPUT_TOKENS:
        raise DeepSeekBudgetError("DEEPSEEK_INPUT_TOKEN_BOUND_EXCEEDED")
    input_rate, output_rate = _RATES[(model, currency)]
    amount = ((Decimal(count) * input_rate + Decimal(MAX_OUTPUT_TOKENS) * output_rate)
              / _MILLION).quantize(_AMOUNT_QUANTUM, rounding=ROUND_CEILING)
    return DeepSeekQuote(model, currency, count, MAX_OUTPUT_TOKENS, amount,
                         PRICE_SNAPSHOT_ID, proof.proof_id)


class GuardedDeepSeekTransport:
    """Reserve every HTTP attempt durably before using the injected boundary.

    The ledger supplies atomic persistence under the caller's process lock.
    The production proof registry is empty, so default construction cannot
    authorize any DeepSeek request. No reservations are refunded or reduced
    using a successful response's usage metadata.
    """

    def __init__(self, transport: HttpTransport, ledger, *, currency: str,
                 token_proofs: Mapping[str, TokenBoundProof] | None = None, clock=None, shared_budget=None):
        if not callable(getattr(transport, "request", None)) or not callable(getattr(ledger, "reserve", None)):
            raise DeepSeekBudgetError("DEEPSEEK_GUARDED_BOUNDARY_INVALID")
        if not isinstance(currency, str) or currency not in {"USD", "CNY"}:
            raise DeepSeekBudgetError("DEEPSEEK_CURRENCY_UNSUPPORTED")
        if shared_budget is not None and (currency != 'CNY' or not callable(getattr(shared_budget, 'reserve', None))):
            raise DeepSeekBudgetError('DEEPSEEK_SHARED_BUDGET_INVALID')
        if clock is not None and not callable(clock):
            raise DeepSeekBudgetError("DEEPSEEK_AWARE_PRICE_CLOCK_REQUIRED")
        if token_proofs is not None and not isinstance(token_proofs, Mapping):
            raise DeepSeekBudgetError("DEEPSEEK_TOKEN_PROOF_REGISTRY_INVALID")
        self._transport = transport
        self._ledger = ledger
        self._currency = currency
        self._proofs = MappingProxyType(dict(DEFAULT_TOKEN_PROOFS if token_proofs is None else token_proofs))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._shared_budget = shared_budget

    def request(self, method, url, *, headers, body, timeout, deadline=None):
        if method != "POST" or url != _URL:
            raise DeepSeekBudgetError("DEEPSEEK_GUARDED_ENDPOINT_INVALID")
        try:
            now = self._clock()
        except Exception:
            raise DeepSeekBudgetError("DEEPSEEK_AWARE_PRICE_CLOCK_REQUIRED") from None
        quote = quote_request(body, currency=self._currency, now=now, token_proofs=self._proofs)
        try:
            authorized = self._ledger.authorization.deepseek
            if type(authorized) is not dict:
                raise ValueError
            if quote.model != authorized["model"]:
                raise DeepSeekBudgetError("DEEPSEEK_AUTHORIZED_MODEL_MISMATCH")
            if quote.currency != authorized["currency"]:
                raise DeepSeekBudgetError("DEEPSEEK_AUTHORIZED_CURRENCY_MISMATCH")
            if quote.price_snapshot_id != authorized["price_snapshot_id"]:
                raise DeepSeekBudgetError("DEEPSEEK_AUTHORIZED_PRICE_SNAPSHOT_MISMATCH")
        except DeepSeekBudgetError:
            raise
        except Exception:
            raise DeepSeekBudgetError("DEEPSEEK_AUTHORIZATION_BINDING_INVALID") from None
        try:
            self._ledger.reserve("deepseek_request", amount=quote.reserved_amount,
                                 currency=quote.currency, metadata={
                                     "price_snapshot_id": PRICE_SNAPSHOT_ID,
                                     "model": quote.model,
                                     "token_proof_id": quote.token_proof_id,
                                     "input_tokens_upper_bound": quote.input_tokens_upper_bound,
                                     "output_tokens_upper_bound": MAX_OUTPUT_TOKENS,
                                 })
        except Exception:
            raise DeepSeekBudgetError("DEEPSEEK_LEDGER_RESERVATION_REJECTED") from None
        if self._shared_budget is not None:
            try:
                self._shared_budget.reserve(quote.reserved_amount, request_sha256=hashlib.sha256(body).hexdigest())
            except Exception:
                raise DeepSeekBudgetError('DEEPSEEK_SHARED_BUDGET_REFUSED') from None
        # Persistence may cross a pricing/proof expiry boundary. Keep the spent
        # reservation, but refuse the wire attempt if its evidence is now stale.
        try:
            issues = pricing_issue_codes(quote.model, quote.currency, now=self._clock(), token_proofs=self._proofs)
        except Exception:
            raise DeepSeekBudgetError("DEEPSEEK_AWARE_PRICE_CLOCK_REQUIRED") from None
        if issues:
            raise DeepSeekBudgetError(issues[0])
        return self._transport.request(method, url, headers=headers, body=body,
                                       timeout=timeout, deadline=deadline)
