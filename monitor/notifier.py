"""Notification snapshots and delivery contracts; no SMTP or network transport."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Protocol

from .classifier import _validate_classification
from .models import parse_aware_iso


_SUBJECTS = {
    "PRE_RESET": "【高优先级】Codex Reset 预告",
    "POST_RESET": "【已重置】Codex Reset 已确认",
    "POSSIBLE_RESET": "【疑似】Codex Reset 可能即将发生",
}
_HEALTH_SUBJECT = "【Codex Reset Monitor】监控连续失败"
_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_RECIPIENT = re.compile(r"[^@\s<>,;:\x00-\x1f\x7f]+@[^@\s<>,;:\x00-\x1f\x7f]+\Z")
_BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class DeliveryResult:
    """A transport's explained outcome; SENT means accepted, not inbox delivery."""

    status: str
    retryable: bool = True
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in {"SENT", "FAILED", "UNKNOWN"}:
            raise ValueError("INVALID_DELIVERY_STATUS")
        if not isinstance(self.retryable, bool):
            raise ValueError("INVALID_RETRYABLE")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not _ERROR_CODE.fullmatch(self.error_code)
        ):
            raise ValueError("INVALID_ERROR_CODE")


class Notifier(Protocol):
    def send(self, notification: dict) -> DeliveryResult:
        """Return an explicit result; transport and retry policy live elsewhere."""
        ...


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = parse_aware_iso(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_DATETIME_REQUIRED")
    return value


def _recipient(value: str) -> str:
    if not isinstance(value, str) or not _RECIPIENT.fullmatch(value):
        raise ValueError("SINGLE_SAFE_RECIPIENT_REQUIRED")
    return value


def _message_id(key: str, recipient: str) -> str:
    # A JSON tuple avoids ambiguous concatenation; no untrusted text enters a header.
    material = json.dumps([key, recipient], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"<{digest}@example.invalid>"


def _snapshot(
    *, key: str, tweet_id: str | None, label: str, recipient: str,
    subject: str, body: str, now: datetime
) -> dict:
    recipient = _recipient(recipient)
    return {
        "key": key,
        "tweet_id": tweet_id,
        "label": label,
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "message_id": _message_id(key, recipient),
        "created_at": _aware(now).astimezone(timezone.utc).isoformat(),
    }


def build_notification(
    tweet: dict, classification: dict, recipient: str, *, now: datetime
) -> dict | None:
    """Make a detached snapshot; subsequent source mutation cannot rewrite its body."""
    classification = _validate_classification(classification)
    label = classification["label"]
    if label == "IRRELEVANT":
        return None
    published = _aware(tweet["created_at"])
    reset_time = classification["reset_time"] or "未明确"
    confidence_line = f"置信度：{classification['confidence']}\n" if "confidence" in classification else ""
    body = (
        "Codex Reset 监控通知\n\n"
        f"分类：{label}\n"
        f"{confidence_line}"
        f"判断理由：{classification['reason']}\n"
        f"预计重置时间：{reset_time}\n"
        f"作者：{tweet['author']}\n"
        f"发布时间（UTC）：{published.astimezone(timezone.utc).isoformat()}\n"
        f"发布时间（北京时间 UTC+08:00）：{published.astimezone(_BEIJING).isoformat()}\n"
        f"链接：{tweet['url']}\n\n"
        f"完整推文原文：\n{tweet['text']}"
    )
    if tweet.get("context") is not None:
        body += f"\n\n已获取上下文：\n{tweet['context']}"
    return _snapshot(
        key=f"{tweet['id']}:{label}", tweet_id=tweet["id"], label=label,
        recipient=recipient, subject=_SUBJECTS[label], body=body, now=now,
    )


def build_failure_notification(
    incident_id: str, recipient: str, *, now: datetime,
    last_success_at: str | None, failures: int, error_code: str
) -> dict:
    """Make one health snapshot with a compact error code, never exception text."""
    if not isinstance(incident_id, str) or not incident_id.strip() or len(incident_id) > 256:
        raise ValueError("INVALID_INCIDENT_ID")
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 1:
        raise ValueError("INVALID_FAILURE_COUNT")
    safe_code = (
        error_code if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code)
        else "UNSPECIFIED_ERROR"
    )
    last_success = (
        _aware(last_success_at).astimezone(timezone.utc).isoformat()
        if last_success_at is not None else "尚无成功记录"
    )
    body = (
        "Codex Reset Monitor 监控连续失败。\n\n"
        f"连续失败次数：{failures}\n"
        f"最后成功时间（UTC）：{last_success}\n"
        f"错误码：{safe_code}\n\n"
        "请检查本地运行状态与配置。此通知不包含凭据或原始异常详情。"
    )
    return _snapshot(
        key=f"health:{incident_id}", tweet_id=None, label="HEALTH",
        recipient=recipient, subject=_HEALTH_SUBJECT, body=body, now=now,
    )
