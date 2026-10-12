"""Small shared value types and validation at the source boundary."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Callable, runtime_checkable
from urllib.parse import urlsplit
import math
import re


# Calendar ISO8601, extended/basic time, explicit Z or offset. Check components
# before fromisoformat: Python otherwise silently normalizes offsets like +00:60.
_AWARE_ISO = re.compile(
    r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8})[Tt]"
    r"(?:[01][0-9]|2[0-3])(?::?[0-5][0-9])"
    r"(?::?[0-5][0-9](?:[.,][0-9]{1,6})?)?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?)\Z"
)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone required")
    return value.astimezone(timezone.utc).isoformat()


def parse_aware_iso(value: str) -> datetime:
    if not isinstance(value, str) or not _AWARE_ISO.fullmatch(value):
        raise ValueError("Valid ISO8601 timestamp and offset required")
    parsed = datetime.fromisoformat(value.replace("t", "T").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timezone required")
    return parsed


def parse_time(value: str) -> datetime:
    return parse_aware_iso(value).astimezone(timezone.utc)


class SourceContractError(ValueError):
    pass


class StageFailure(Exception):
    """A transport may report whether retrying a known failure is appropriate."""

    def __init__(self, *, retryable: bool = True, retry_after_seconds: float | None = None):
        super().__init__("External stage failed")
        if type(retryable) is not bool:
            raise ValueError("INVALID_RETRYABLE")
        if retry_after_seconds is not None and (type(retry_after_seconds) not in (int, float)
                or not math.isfinite(retry_after_seconds) or retry_after_seconds < 0):
            raise ValueError("INVALID_RETRY_AFTER")
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class FetchSnapshot:
    tweets: list[dict]
    complete: bool = True


class Fetcher(Protocol):
    def fetch_latest(self, *, limit: int) -> FetchSnapshot: ...


@runtime_checkable
class DurableFetcher(Fetcher, Protocol):
    """Bind to the locked main state; clear READY only in the ingest commit."""
    def bind_state(self, state: dict, persist: Callable[[dict], None]) -> None: ...
    def prepare_ingest(self, snapshot: FetchSnapshot) -> None: ...


def validate_snapshot(snapshot: FetchSnapshot, target: str, limit: int = 3, *, target_user_id: str | None = None) -> list[dict]:
    """Reject partial/oversized output; never disguise it by slicing locally."""
    if not isinstance(snapshot, FetchSnapshot) or snapshot.complete is not True:
        raise SourceContractError("Incomplete snapshot")
    if not isinstance(snapshot.tweets, list) or len(snapshot.tweets) > limit:
        raise SourceContractError("Source delivery limit violated")
    normalized = {}
    for item in snapshot.tweets:
        if not isinstance(item, dict):
            raise SourceContractError("Invalid tweet")
        tweet_id = item.get("id")
        if (not isinstance(tweet_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", tweet_id)
                or int(tweet_id) > (1 << 64) - 1):
            raise SourceContractError("Invalid tweet ID")
        author = item.get("author")
        if not isinstance(author, str) or author.lower().lstrip("@") != target.lower().lstrip("@"):
            raise SourceContractError("Unexpected author")
        author_id = item.get("author_id")
        if author_id is not None and (not isinstance(author_id, str)
                or re.fullmatch(r"[1-9][0-9]{0,19}", author_id) is None
                or int(author_id) > (1 << 64) - 1):
            raise SourceContractError("Invalid author identity")
        if target_user_id is not None and author_id != target_user_id:
            raise SourceContractError("Unexpected stable author identity")
        text, url, context = item.get("text"), item.get("url"), item.get("context")
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise SourceContractError("Invalid tweet text")
        if context is not None and (not isinstance(context, str) or len(context) > 100_000):
            raise SourceContractError("Invalid context")
        if not isinstance(url, str) or any(c in url for c in "\r\n"):
            raise SourceContractError("Invalid tweet link")
        try:
            link = urlsplit(url)
            created_at = timestamp(parse_time(item.get("created_at")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise SourceContractError("Invalid tweet fields") from exc
        expected = f"/{target.lstrip('@')}/status/{tweet_id}".lower()
        if link.scheme != "https" or link.netloc.lower() not in {"x.com", "twitter.com", "www.twitter.com"} or link.path.lower() != expected:
            raise SourceContractError("Unexpected tweet link")
        tweet = {"id": tweet_id, "author": author.lstrip("@"), "text": text,
                 "url": url, "created_at": created_at, "context": context}
        if author_id is not None:
            tweet["author_id"] = author_id
        if tweet_id in normalized and normalized[tweet_id] != tweet:
            raise SourceContractError("Conflicting duplicate tweet")
        normalized[tweet_id] = tweet
    return sorted(normalized.values(), key=lambda t: (parse_time(t["created_at"]), int(t["id"])))
