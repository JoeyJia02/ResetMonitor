"""Classification contracts and local validation; no network transport."""

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
import json
import math
import re
from typing import Protocol, runtime_checkable

from .models import parse_aware_iso
from .model_payload import pack_model_tweets
from .classification_prompt import SYSTEM_PROMPT


LABELS = frozenset({"PRE_RESET", "POST_RESET", "POSSIBLE_RESET", "IRRELEVANT"})
_FIELDS = frozenset({"label", "confidence", "reset_time", "reason"})
_CURRENT_FIELDS = frozenset({"label", "reset_time", "reason"})
_RESET_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


class ClassificationError(ValueError):
    """An incomplete or invalid model response, never an irrelevant result."""


@dataclass(frozen=True)
class ClassifierReply:
    content: str
    finish_reason: str = "stop"


class Classifier(Protocol):
    def classify(
        self, tweet: dict, *, now: datetime, timezone: str
    ) -> ClassifierReply:
        """Return the model response without treating malformed output as success."""
        ...


@runtime_checkable
class BudgetedClassifier(Protocol):
    def classify_with_deadline(self, tweet: dict, *, now: datetime,
                               timezone: str, deadline: float) -> ClassifierReply: ...


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ClassificationError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ClassificationError("NONFINITE_JSON_NUMBER")


def _validate_classification(value: object) -> dict:
    """Return a detached, normalized classification with the exact schema."""
    if not isinstance(value, dict) or set(value) not in (_FIELDS, _CURRENT_FIELDS):
        raise ClassificationError("INVALID_CLASSIFICATION_FIELDS")
    label = value["label"]
    if not isinstance(label, str) or label not in LABELS:
        raise ClassificationError("INVALID_LABEL")
    legacy = "confidence" in value
    if legacy:
        confidence = value["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ClassificationError("INVALID_CONFIDENCE")
        try:
            confidence = float(confidence)
        except (OverflowError, ValueError) as exc:
            raise ClassificationError("INVALID_CONFIDENCE") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ClassificationError("INVALID_CONFIDENCE")

    reset_time = value["reset_time"]
    if reset_time is not None:
        try:
            parsed = parse_aware_iso(reset_time)
        except ValueError as exc:
            raise ClassificationError("INVALID_RESET_TIME") from exc
        reset_time = parsed.isoformat()

    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > (1000 if legacy else 200):
        raise ClassificationError("INVALID_REASON")
    result = {
        "label": label,
        "reset_time": reset_time,
        "reason": reason.strip(),
    }
    if legacy:
        result["confidence"] = confidence
    return result


def validate_batch_results(value, expected_ids) -> list[dict]:
    """Validate the user-specified model schema without classifying any text."""
    expected_ids = tuple(expected_ids)
    if (not 1 <= len(expected_ids) <= 3
            or any(not isinstance(identifier, str) for identifier in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)):
        raise ClassificationError("CLASSIFICATION_INPUT_IDS_INVALID")
    if (not isinstance(value, dict) or set(value) != {"results"}
            or not isinstance(value["results"], list) or len(value["results"]) != len(expected_ids)):
        raise ClassificationError("CLASSIFICATION_RESULTS_INVALID")
    results = []
    for row, expected_id in zip(value["results"], expected_ids):
        if not isinstance(row, dict) or set(row) != {"id", "label", "reset_time", "reason"}:
            raise ClassificationError("CLASSIFICATION_RESULTS_FIELDS_INVALID")
        if not isinstance(row["id"], str) or row["id"] != expected_id:
            raise ClassificationError("CLASSIFICATION_RESULTS_IDS_INVALID")
        if row["reset_time"] is not None and (
                not isinstance(row["reset_time"], str) or _RESET_TIME.fullmatch(row["reset_time"]) is None):
            raise ClassificationError("CLASSIFICATION_RESULTS_FIELDS_INVALID")
        try:
            classification = _validate_classification({name: row[name] for name in _CURRENT_FIELDS})
        except ClassificationError:
            raise ClassificationError("CLASSIFICATION_RESULTS_FIELDS_INVALID") from None
        results.append({"id": expected_id, "label": classification["label"],
                        "reset_time": classification["reset_time"], "reason": classification["reason"]})
    return results


def parse_batch_classifications(reply: ClassifierReply, expected_ids) -> list[dict]:
    if not isinstance(reply, ClassifierReply) or reply.finish_reason != "stop":
        raise ClassificationError("INCOMPLETE_CLASSIFICATION")
    if not isinstance(reply.content, str) or not reply.content.strip():
        raise ClassificationError("EMPTY_CLASSIFICATION")
    try:
        value = json.loads(reply.content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError, TypeError):
        raise ClassificationError("INVALID_CLASSIFICATION_JSON") from None
    return validate_batch_results(value, expected_ids)


def parse_classification(reply: ClassifierReply) -> dict:
    """Reject incomplete output, JSON extensions, duplicate keys and schema drift."""
    if not isinstance(reply, ClassifierReply) or reply.finish_reason != "stop":
        raise ClassificationError("INCOMPLETE_CLASSIFICATION")
    if not isinstance(reply.content, str) or not reply.content.strip():
        raise ClassificationError("EMPTY_CLASSIFICATION")
    try:
        value = json.loads(
            reply.content, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ClassificationError):
            raise
        raise ClassificationError("INVALID_CLASSIFICATION_JSON") from exc
    if isinstance(value, dict) and set(value) == {"results"}:
        row = validate_batch_results(value, ("1",))[0]
        return {name: row[name] for name in ("label", "reset_time", "reason")}
    # Historical persisted/fake replies retain their original confidence schema.
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ClassificationError("INVALID_CLASSIFICATION_FIELDS")
    return _validate_classification(value)


_SYSTEM_PROMPT = SYSTEM_PROMPT


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_DATETIME_REQUIRED")
    return value.astimezone(datetime_timezone.utc).isoformat()


def build_prompt(tweet: dict, now: datetime, timezone: str) -> list[dict[str, str]]:
    """Build fixed instructions plus a JSON data message without classifying locally."""
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("TIMEZONE_REQUIRED")
    packed, _ = pack_model_tweets([tweet])
    payload = {"tweets": packed, "observed_at": _utc_iso(now), "timezone": timezone}
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)},
    ]
