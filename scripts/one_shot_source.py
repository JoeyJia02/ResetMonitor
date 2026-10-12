"""Bounded source stage for one manually requested end-to-end test.

This module has no CLI, persistence, credential discovery, or automatic retry.
The caller owns the durable one-shot reservation and direct HTTPS transport.
"""

from datetime import datetime, timedelta, timezone
import hashlib
from html import unescape
import json
import re
from urllib.request import Request

from scripts.capture_first_three import (
    ACTOR_ID, BUILD_ID, BUILD_NUMBER, MAX_RESPONSE_BYTES, PAYLOAD,
    TARGET, TARGET_USER_ID, URL,
)
from scripts.verify_api_connections import _reject_constant, _strict_object, _valid_secret


EMBED_BASE = "https://cdn.syndication.twimg.com/tweet-result"
EMBED_MAX_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"[1-9][0-9]{0,19}\Z")
PUBLIC_ERROR_CODES = frozenset({
    "ONE_SHOT_SOURCE_TOKEN_INVALID", "ONE_SHOT_SOURCE_CLOCK_INVALID",
    "ONE_SHOT_SOURCE_COUNT_INVALID", "ONE_SHOT_SOURCE_TOTAL_INVALID",
    "ONE_SHOT_SOURCE_TOTAL_MISMATCH", "ONE_SHOT_SOURCE_ROW_FAILED",
    "ONE_SHOT_SOURCE_PROFILE_MISMATCH", "ONE_SHOT_SOURCE_ID_INVALID",
    "ONE_SHOT_SOURCE_IDENTITY_MISMATCH", "ONE_SHOT_SOURCE_TEXT_INVALID",
    "ONE_SHOT_SOURCE_TIME_INVALID", "ONE_SHOT_SOURCE_TIME_IN_FUTURE",
    "ONE_SHOT_SOURCE_FLAGS_INVALID", "ONE_SHOT_SOURCE_URL_MISMATCH",
    "ONE_SHOT_SOURCE_DUPLICATE_ID", "ONE_SHOT_SOURCE_ORDER_INVALID",
    "ONE_SHOT_SOURCE_REPLY_INVALID", "ONE_SHOT_SOURCE_SECRET_REFLECTION",
    "ONE_SHOT_EMBED_ROOT_INVALID", "ONE_SHOT_EMBED_ID_MISMATCH",
    "ONE_SHOT_EMBED_AUTHOR_INVALID", "ONE_SHOT_EMBED_IDENTITY_MISMATCH",
    "ONE_SHOT_EMBED_TIME_MISMATCH", "ONE_SHOT_EMBED_TEXT_INVALID",
    "ONE_SHOT_EMBED_TEXT_TRUNCATED", "ONE_SHOT_EMBED_TEXT_MISMATCH",
    "ONE_SHOT_EMBED_QUOTE_INVALID", "ONE_SHOT_EMBED_QUOTE_DEPTH_INVALID",
    "ONE_SHOT_EMBED_QUOTE_MISSING", "ONE_SHOT_EMBED_CARD_INVALID",
    "ONE_SHOT_EMBED_POLL_INVALID",
}) | frozenset(prefix + suffix for prefix in ("ONE_SHOT_SOURCE", "ONE_SHOT_EMBED")
               for suffix in ("_HTTP_FAILED", "_TOTAL_INVALID", "_BODY_INVALID",
                              "_SECRET_REFLECTION", "_JSON_INVALID", "_REQUEST_FAILED"))


class OneShotSourceError(ValueError):
    """Fixed public code; provider bodies and credentials never enter errors."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise OneShotSourceError(code)


def _identifier(value, code="ONE_SHOT_SOURCE_ID_INVALID"):
    _require(isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None, code)
    return value


def _time(value):
    try:
        _require(isinstance(value, str) and len(value) <= 64, "ONE_SHOT_SOURCE_TIME_INVALID")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
                 "ONE_SHOT_SOURCE_TIME_INVALID")
        return parsed.astimezone(timezone.utc)
    except OneShotSourceError:
        raise
    except Exception:
        raise OneShotSourceError("ONE_SHOT_SOURCE_TIME_INVALID") from None


def _text(value, code="ONE_SHOT_SOURCE_TEXT_INVALID"):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 50_000, code)
    return value


def _json_response(request, transport, token, *, limit, accepted, timeout, prefix):
    try:
        with transport(request, timeout=timeout) as response:
            status = response.getcode()
            _require(type(status) is int and status in accepted, prefix + "_HTTP_FAILED")
            totals = None
            if prefix == "ONE_SHOT_SOURCE":
                headers = response.headers
                if hasattr(headers, "get_all"):
                    totals = headers.get_all("X-Apify-Pagination-Total")
                elif hasattr(headers, "items"):
                    totals = [value for name, value in headers.items()
                              if isinstance(name, str) and name.lower() == "x-apify-pagination-total"]
                _require(totals is None or isinstance(totals, list), prefix + "_TOTAL_INVALID")
            body = response.read(limit + 1)
        _require(isinstance(body, bytes) and len(body) <= limit, prefix + "_BODY_INVALID")
        decoded = body.decode("utf-8")
        _require(token not in decoded, prefix + "_SECRET_REFLECTION")
        document = json.loads(decoded, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        return document, totals, status, hashlib.sha256(body).hexdigest()
    except OneShotSourceError:
        raise
    except (UnicodeError, ValueError):
        raise OneShotSourceError(prefix + "_JSON_INVALID") from None
    except Exception:
        raise OneShotSourceError(prefix + "_REQUEST_FAILED") from None


def _author(document, *, target=False):
    _require(isinstance(document, dict), "ONE_SHOT_EMBED_AUTHOR_INVALID")
    identifier = _identifier(document.get("id_str", document.get("id")),
                             "ONE_SHOT_EMBED_AUTHOR_INVALID")
    username = document.get("screen_name", document.get("username"))
    _require(isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", username),
             "ONE_SHOT_EMBED_AUTHOR_INVALID")
    if target:
        _require(identifier == TARGET_USER_ID and username.lower() == TARGET,
                 "ONE_SHOT_EMBED_IDENTITY_MISMATCH")
    return {"id": identifier, "username": username}


def _embed_text(document):
    # Official full_text wins where available; never truncate the provider text.
    value = document.get("full_text", document.get("text"))
    _require(document.get("truncated") is not True, "ONE_SHOT_EMBED_TEXT_TRUNCATED")
    return _text(value, "ONE_SHOT_EMBED_TEXT_INVALID")


def _compare_embed_text(document, actor_text):
    """Verify the official representation without replacing the full Actor text.

    Syndication note tweets can contain a 280-character, HTML-escaped preview
    and only a note_tweet id, with no complete body. After the caller verifies
    tweet id, author and timestamp, accept that *marked* preview as a prefix.
    Ordinary tweets and an explicit full_text still require an exact match.
    """
    raw = _embed_text(document)
    decoded = unescape(raw)
    if actor_text == raw:
        return "EXACT"
    if actor_text == decoded:
        return "HTML_UNESCAPED"
    note = document.get("note_tweet")
    display = document.get("display_text_range")
    preview_length = len(raw.encode("utf-16-le")) // 2
    is_note_preview = (
        "full_text" not in document
        and isinstance(note, dict) and isinstance(note.get("id"), str)
        and bool(note["id"].strip())
        and document.get("isStaleEdit") is not True
        and isinstance(display, list) and len(display) == 2
        and all(type(index) is int for index in display)
        and display[0] == 0 and display[1] == preview_length
        and preview_length == 280
    )
    if is_note_preview:
        for preview in (raw, decoded):
            if len(actor_text) > len(preview) and actor_text.startswith(preview):
                return "NOTE_PREVIEW_PREFIX"
    raise OneShotSourceError("ONE_SHOT_EMBED_TEXT_MISMATCH")


def _quote(value, *, depth=0):
    if value is None:
        return None
    _require(depth <= 3, "ONE_SHOT_EMBED_QUOTE_DEPTH_INVALID")
    _require(isinstance(value, dict), "ONE_SHOT_EMBED_QUOTE_INVALID")
    identifier = _identifier(value.get("id_str", value.get("id")), "ONE_SHOT_EMBED_QUOTE_INVALID")
    created = value.get("created_at")
    _time(created)
    result = {"id": identifier, "text": _embed_text(value), "created_at": created,
              "author": _author(value.get("user")),
              "provenance": "nested quoted_tweet in the original item's official embed; no extra quote GET"}
    reply_id = value.get("in_reply_to_status_id_str", value.get("in_reply_to_status_id"))
    if reply_id is not None:
        result["in_reply_to_status_id"] = _identifier(str(reply_id), "ONE_SHOT_EMBED_QUOTE_INVALID")
    if value.get("card") is not None:
        result["poll"] = _poll(value["card"])
    if value.get("quoted_tweet") is not None:
        result["quoted_tweet"] = _quote(value["quoted_tweet"], depth=depth + 1)
    return result


def _poll(card):
    if card is None:
        return None
    _require(isinstance(card, dict), "ONE_SHOT_EMBED_CARD_INVALID")
    name = card.get("name", "")
    _require(isinstance(name, str), "ONE_SHOT_EMBED_CARD_INVALID")
    if "poll" not in name.lower():
        return None
    values = card.get("binding_values")
    if isinstance(values, list):
        mapping = {}
        for item in values:
            _require(isinstance(item, dict) and isinstance(item.get("key"), str)
                     and item["key"] not in mapping and isinstance(item.get("value"), dict),
                     "ONE_SHOT_EMBED_POLL_INVALID")
            mapping[item["key"]] = item["value"]
        values = mapping
    _require(isinstance(values, dict), "ONE_SHOT_EMBED_POLL_INVALID")
    options = []
    for number in range(1, 5):
        item = values.get("choice" + str(number) + "_label")
        if item is not None:
            _require(isinstance(item, dict), "ONE_SHOT_EMBED_POLL_INVALID")
            options.append(_text(item.get("string_value"), "ONE_SHOT_EMBED_POLL_INVALID"))
    _require(2 <= len(options) <= 4, "ONE_SHOT_EMBED_POLL_INVALID")
    result = {"card_name": name, "options": options}
    end = values.get("end_datetime_utc")
    if end is not None:
        _require(isinstance(end, dict), "ONE_SHOT_EMBED_POLL_INVALID")
        result["end_datetime_utc"] = end.get("string_value")
        _time(result["end_datetime_utc"])
    final = values.get("counts_are_final")
    if final is not None:
        _require(isinstance(final, dict), "ONE_SHOT_EMBED_POLL_INVALID")
        flag = final.get("boolean_value")
        if flag is None and final.get("string_value") in ("true", "false"):
            flag = final["string_value"] == "true"
        _require(type(flag) is bool, "ONE_SHOT_EMBED_POLL_INVALID")
        result["counts_are_final"] = flag
    return result


def _validate_row(row, observed):
    _require(isinstance(row, dict) and row.get("success") is True and row.get("status") == "success",
             "ONE_SHOT_SOURCE_ROW_FAILED")
    _require(row.get("inputType") == "profile" and row.get("normalizedInput") == TARGET,
             "ONE_SHOT_SOURCE_PROFILE_MISMATCH")
    identifier = _identifier(row.get("tweetId"))
    _require(row.get("authorId") == TARGET_USER_ID, "ONE_SHOT_SOURCE_IDENTITY_MISMATCH")
    author = row.get("author")
    _require(isinstance(author, dict) and author.get("id") == TARGET_USER_ID,
             "ONE_SHOT_SOURCE_IDENTITY_MISMATCH")
    for username in (row.get("authorUsername"), author.get("username")):
        _require(username is None or isinstance(username, str) and username.lower() == TARGET,
                 "ONE_SHOT_SOURCE_IDENTITY_MISMATCH")
    text = _text(row.get("text"))
    created_at = row.get("createdAt")
    created = _time(created_at)
    _require(created <= observed + timedelta(minutes=5), "ONE_SHOT_SOURCE_TIME_IN_FUTURE")
    for name in ("isReply", "isRetweet", "isQuote"):
        _require(type(row.get(name)) is bool, "ONE_SHOT_SOURCE_FLAGS_INVALID")
    _require(row["isRetweet"] or row.get("retweetedTweet") is None,
             "ONE_SHOT_SOURCE_FLAGS_INVALID")
    canonical_url = "https://x.com/" + TARGET + "/status/" + identifier
    _require(row.get("url") in (None, canonical_url,
                                "https://twitter.com/" + TARGET + "/status/" + identifier),
             "ONE_SHOT_SOURCE_URL_MISMATCH")
    return {"id": identifier, "author": TARGET, "author_id": TARGET_USER_ID,
            "text": text, "created_at": created_at, "url": canonical_url}


def fetch_three(token, transport, clock):
    """Run once, then return oldest-first canonical tweets and safe diagnostics.

    The supplied urllib-style transport must reject redirects and retries.
    Native retweets are skipped without requesting replacement items. Every
    remaining item gets at most one credential-free official embed GET so poll
    and quoted text missing from the observed Actor schema can be included.
    """
    _require(_valid_secret(token), "ONE_SHOT_SOURCE_TOKEN_INVALID")
    try:
        observed = clock()
        _require(isinstance(observed, datetime) and observed.tzinfo is not None
                 and observed.utcoffset() is not None, "ONE_SHOT_SOURCE_CLOCK_INVALID")
        observed = observed.astimezone(timezone.utc)
    except OneShotSourceError:
        raise
    except Exception:
        raise OneShotSourceError("ONE_SHOT_SOURCE_CLOCK_INVALID") from None
    request = Request(URL, method="POST", data=json.dumps(PAYLOAD, separators=(",", ":")).encode("utf-8"),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "Accept": "application/json"})
    rows, totals, status, body_hash = _json_response(request, transport, token, limit=MAX_RESPONSE_BYTES,
                                                    accepted={200, 201}, timeout=90,
                                                    prefix="ONE_SHOT_SOURCE")
    _require(isinstance(rows, list) and len(rows) <= 3, "ONE_SHOT_SOURCE_COUNT_INVALID")
    total = None
    if totals:
        _require(len(totals) == 1 and isinstance(totals[0], str)
                 and re.fullmatch(r"0|[1-9][0-9]{0,8}", totals[0]) is not None,
                 "ONE_SHOT_SOURCE_TOTAL_INVALID")
        total = int(totals[0])
        # Fresh Apify datasets have eventually consistent total metadata.
        # Validate the actual array above; a lagging total must not reject it.
        # Still reject metadata reporting more than our authorized three items.
        _require(total <= 3, "ONE_SHOT_SOURCE_TOTAL_MISMATCH")
    canonical = [_validate_row(row, observed) for row in rows]
    ids = [tweet["id"] for tweet in canonical]
    _require(len(set(ids)) == len(ids), "ONE_SHOT_SOURCE_DUPLICATE_ID")
    source_order = [(_time(tweet["created_at"]), int(tweet["id"])) for tweet in canonical]
    _require(source_order == sorted(source_order, reverse=True), "ONE_SHOT_SOURCE_ORDER_INVALID")
    accepted, skipped, embed_hashes, text_matches = [], [], {}, {}
    for row, tweet in zip(rows, canonical):
        if row["isRetweet"]:
            skipped.append(tweet["id"])
            continue
        embed_url = EMBED_BASE + "?id=" + tweet["id"] + "&lang=en&token=0"
        embed_request = Request(embed_url, method="GET", headers={"Accept": "application/json"})
        embed, _, _, embed_hash = _json_response(embed_request, transport, token, limit=EMBED_MAX_BYTES,
                                                accepted={200}, timeout=20, prefix="ONE_SHOT_EMBED")
        _require(isinstance(embed, dict), "ONE_SHOT_EMBED_ROOT_INVALID")
        _require(embed.get("id_str", embed.get("id")) == tweet["id"], "ONE_SHOT_EMBED_ID_MISMATCH")
        _author(embed.get("user"), target=True)
        _require(_time(embed.get("created_at")) == _time(tweet["created_at"]),
                 "ONE_SHOT_EMBED_TIME_MISMATCH")
        text_matches[tweet["id"]] = _compare_embed_text(embed, tweet["text"])
        quoted = _quote(embed.get("quoted_tweet"))
        _require(not row["isQuote"] or quoted is not None, "ONE_SHOT_EMBED_QUOTE_MISSING")
        reply_id = row.get("inReplyToTweetId")
        if reply_id is not None:
            _identifier(reply_id, "ONE_SHOT_SOURCE_REPLY_INVALID")
        reply_user = row.get("inReplyToUsername")
        _require(reply_user is None or isinstance(reply_user, str)
                 and re.fullmatch(r"[A-Za-z0-9_]{1,15}", reply_user), "ONE_SHOT_SOURCE_REPLY_INVALID")
        tweet["context"] = {"is_reply": row["isReply"], "is_quote": row["isQuote"],
                            "in_reply_to_tweet_id": reply_id, "in_reply_to_username": reply_user,
                            "quoted_tweet": quoted, "poll": _poll(embed.get("card"))}
        accepted.append(tweet)
        embed_hashes[tweet["id"]] = embed_hash
    accepted.sort(key=lambda tweet: (_time(tweet["created_at"]), int(tweet["id"])))
    safe_evidence = {
        "actor_id": ACTOR_ID, "requested_build_id": BUILD_ID, "requested_build_number": BUILD_NUMBER,
        "provider_run_cap_usd": "0.0009", "http_status": status, "source_posts": 1,
        "official_context_gets": len(embed_hashes), "delivered_count": len(rows),
        "analysis_count": len(accepted), "pagination_total": total,
        "pagination_total_present": bool(totals), "delivered_ids_latest_first": ids,
        "pagination_total_matches_delivered": total == len(rows) if total is not None else None,
        "native_retweet_ids_skipped": skipped, "source_body_sha256": body_hash,
        "official_embed_sha256_by_id": embed_hashes, "canonical_order": "oldest_first",
        "official_text_match_by_id": text_matches,
        "observed_at": observed.isoformat(), "additional_actor_runs": 0,
    }
    # No provider response or raw error is retained in diagnostics.
    _require(token not in json.dumps((accepted, safe_evidence), ensure_ascii=False),
             "ONE_SHOT_SOURCE_SECRET_REFLECTION")
    return accepted, safe_evidence
