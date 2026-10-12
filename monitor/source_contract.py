"""Reviewed source evidence, separate from an operator's spending approval.

The production catalog is intentionally empty. The candidate Actor's public
documentation does not establish the complete latest-three / replies included /
retweets excluded contract. A JSON flag or URL supplied by a caller cannot add
an entry to this code-maintained catalog. Offline tests inject a separate
catalog of explicitly synthetic, reviewed value objects.

Public references checked 2026-10-10:
https://apify.com/lance_api/x-tweet-scraper-api
https://apify.com/lance_api/x-tweet-scraper-api/api/openapi
https://apify.com/lance_api/x-tweet-scraper-api/output-schema
https://apify.com/lance_api/x-tweet-scraper-api/pricing
https://docs.apify.com/api/v2/actors-runs-post
https://docs.apify.com/api/v2/dataset-items-get

Dataset ordering is insertion ordering. A run spending limit does not prove a
run + polling + dataset + retained-storage monetary upper bound. Neither a
reviewed source contract nor operation count limits supply that missing proof.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from .models import parse_time


MAX_SOURCE_REVIEW_AGE = timedelta(days=7)
_ACTOR = "lance_api~x-tweet-scraper-api"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
_RESOURCE = re.compile(r"[A-Za-z0-9]{17}\Z")
_BUILD = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}\Z")


class SourceEvidenceError(ValueError):
    def __init__(self, code="APIFY_SOURCE_CONTRACT_UNVERIFIED"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceContract:
    """An entry installed by code review, never deserialized from approval JSON.

    URLs and a digest make the review traceable, but do not verify semantics by
    themselves. Trust comes from membership in the reviewed catalog. The short
    validity period requires an explicit evidence refresh for later use.
    """

    contract_id: str
    actor: str
    build_id: str
    build_number: str
    target_username: str
    target_user_id: str
    official_evidence_urls: tuple[str, ...]
    evidence_sha256: str
    reviewed_at: str
    expires_at: str
    latest_limit: int = 3
    include_replies: bool = True
    exclude_retweets: bool = True


PRODUCTION_CONTRACT_CATALOG: Mapping[str, SourceContract] = MappingProxyType({})


def valid_user_id(value) -> bool:
    return (isinstance(value, str)
            and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
            and int(value) <= (1 << 64) - 1)


def _official_url(value) -> bool:
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.netloc in {"apify.com", "docs.apify.com"}
                and bool(url.path) and not url.username and not url.password
                and not url.query and not url.fragment)
    except ValueError:
        return False


def resolve_source_contract(contract_id, *, target_username, target_user_id,
                            now: datetime, catalog=None) -> SourceContract:
    """Pure fail-closed lookup; never downloads evidence or discovers identity."""
    if (not isinstance(contract_id, str) or not _IDENTIFIER.fullmatch(contract_id)
            or not isinstance(target_username, str)
            or not re.fullmatch(r"[a-z0-9_]{1,15}", target_username)
            or not valid_user_id(target_user_id)):
        raise SourceEvidenceError()
    entries = PRODUCTION_CONTRACT_CATALOG if catalog is None else catalog
    if not isinstance(entries, Mapping):
        raise SourceEvidenceError()
    entry = entries.get(contract_id)
    if not isinstance(entry, SourceContract):
        raise SourceEvidenceError()
    if (entry.contract_id != contract_id or entry.actor != _ACTOR
            or not isinstance(entry.build_id, str) or not _RESOURCE.fullmatch(entry.build_id)
            or not isinstance(entry.build_number, str) or not _BUILD.fullmatch(entry.build_number)
            or entry.target_username != target_username or entry.target_user_id != target_user_id
            or type(entry.latest_limit) is not int or entry.latest_limit != 3
            or entry.include_replies is not True or entry.exclude_retweets is not True
            or not isinstance(entry.official_evidence_urls, tuple) or not entry.official_evidence_urls
            or not all(_official_url(url) for url in entry.official_evidence_urls)
            or len(set(entry.official_evidence_urls)) != len(entry.official_evidence_urls)
            or not isinstance(entry.evidence_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry.evidence_sha256)):
        raise SourceEvidenceError()
    try:
        reviewed, expires = parse_time(entry.reviewed_at), parse_time(entry.expires_at)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError()
        current = now.astimezone(timezone.utc)
        if not reviewed < expires <= reviewed + MAX_SOURCE_REVIEW_AGE:
            raise ValueError()
        if not reviewed <= current < expires:
            raise SourceEvidenceError("APIFY_SOURCE_CONTRACT_EXPIRED")
    except SourceEvidenceError:
        raise
    except (ValueError, TypeError, OverflowError):
        raise SourceEvidenceError() from None
    return entry
