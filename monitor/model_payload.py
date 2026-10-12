"""Keep only semantic input for model classification; never fetch extra data."""
import json
import re

from monitor.models import parse_aware_iso


_ID = re.compile(r"[1-9][0-9]{0,19}\Z")


class PayloadError(ValueError):
    """Fixed, non-secret local input error."""
    code = "MODEL_PAYLOAD_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _invalid():
    raise PayloadError() from None


def _text(value):
    if not isinstance(value, str) or not value.strip():
        _invalid()
    # Preserve the complete original, including spacing and line breaks.
    return value


def _time(value):
    try:
        parse_aware_iso(value)
    except (ValueError, TypeError, OverflowError):
        _invalid()
    return value

def _post_text(value):
    truncated = value.get('truncated', False)
    if type(truncated) is not bool:
        _invalid()
    complete = value.get('full_text')
    if truncated and complete is None:
        _invalid()
    return _text(complete if complete is not None else value.get('text'))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _context(value):
    if value is None:
        return {}, None
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique,
                               parse_constant=lambda _value: _invalid())
        except json.JSONDecodeError:
            if value.lstrip().startswith(("{", "[")):
                _invalid()
            # Older callers can already hold an unstructured semantic context.
            # Keep its exact wording without claiming it is a reply or quote.
            return {}, _text(value)
        except (ValueError, TypeError, RecursionError):
            _invalid()
    if not isinstance(value, dict):
        _invalid()
    return value, None


def _choose(mapping, first, second):
    # Prefer the canonical field when populated; legacy aliases may coexist
    # as null in otherwise valid source objects.
    value = mapping.get(first)
    return mapping.get(second) if value is None else value


def _poll_options(value):
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("options")
    if not isinstance(value, list) or not value:
        _invalid()
    options = []
    for option in value:
        # Some older source shapes wrap the option text alongside vote counts.
        # Counts are discarded; only the actual option wording is retained.
        if isinstance(option, dict):
            option = _choose(option, "text", "label")
        options.append(_text(option))
    return options


def _poll_from(mapping):
    return _poll_options(_choose(mapping, "poll_options", "poll"))


def _semantic_post(value, *, depth=0, seen=None):
    if not isinstance(value, dict) or depth > 8:
        _invalid()
    seen = set() if seen is None else seen
    marker = id(value)
    if marker in seen:
        _invalid()
    seen.add(marker)
    try:
        result = {"text": _post_text(value)}
        created = _choose(value, "created_at", "createdAt")
        if created is not None:
            result["created_at"] = _time(created)
        quote = _choose(value, "quoted_tweet", "quotedTweet")
        if quote is not None:
            result["quoted_tweet"] = _semantic_post(quote, depth=depth + 1, seen=seen)
        poll = _poll_from(value)
        if poll is not None:
            result["poll_options"] = poll
        return result
    finally:
        seen.remove(marker)


def _semantic_context(value, *, depth=0, seen=None):
    if depth > 8:
        _invalid()
    context, context_text = _context(value)
    seen = set() if seen is None else seen
    marker = id(context)
    if marker in seen:
        _invalid()
    seen.add(marker)
    try:
        result = {}
        if context_text is not None:
            result['context_text'] = context_text
        quote = _choose(context, 'quoted_tweet', 'quotedTweet')
        if quote is not None:
            result['quoted_tweet'] = _semantic_post(quote, depth=depth + 1, seen=seen)
        poll = _poll_from(context)
        if poll is not None:
            result['poll_options'] = poll
        reply = _choose(context, 'reply_parent', 'replied_tweet')
        if reply is not None:
            result['in_reply_to'] = _semantic_post(reply, depth=depth + 1, seen=seen)
        original = context.get('original_context')
        if original is not None:
            semantic = _semantic_context(original, depth=depth + 1, seen=seen)
            if semantic:
                result['original_context'] = semantic
        for field in ('new_evidence','reclassification_basis'):
            if field in context:
                result[field] = _text(context[field])
        return result
    finally:
        seen.remove(marker)


def pack_model_tweets(tweets):
    """Return minimal detached model rows and a local index-to-original-ID map.

    All complete text remains available for semantic analysis. IDs used by the
    model are short indices; original IDs, links and source metadata stay local.
    No credentials, files, environment, browser, network or source calls occur.
    """
    if not isinstance(tweets, list) or not 1 <= len(tweets) <= 3:
        _invalid()
    packed, index_to_id, identifiers = [], {}, set()
    for index, tweet in enumerate(tweets, 1):
        if not isinstance(tweet, dict):
            _invalid()
        identifier = tweet.get("id")
        if (not isinstance(identifier, str) or _ID.fullmatch(identifier) is None
                or int(identifier) > (1 << 64) - 1 or identifier in identifiers):
            _invalid()
        identifiers.add(identifier)
        short_id = str(index)
        row = {"id": short_id, "created_at": _time(tweet.get("created_at")),
               "text": _post_text(tweet)}
        row.update(_semantic_context(tweet.get('context')))
        packed.append(row)
        index_to_id[short_id] = identifier
    return packed, index_to_id
