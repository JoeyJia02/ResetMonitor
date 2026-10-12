"""Offline boundary tests for the real observed Actor response shape."""

from copy import deepcopy
from datetime import datetime, timezone
from email.message import Message
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import one_shot_source as source


ROOT = Path(__file__).resolve().parents[1]
SECRET = "SYNTHETIC_ONE_SHOT_SOURCE_PRIVATE_TOKEN"
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class Response:
    def __init__(self, value, *, status=200, totals=None):
        self.body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.status = status
        self.headers = Message()
        for value in totals or []:
            self.headers["X-Apify-Pagination-Total"] = value
        self.closed = False
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def getcode(self):
        return self.status

    def read(self, limit):
        self.read_limits.append(limit)
        return self.body[:limit]


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def official_embed(curated):
    """Make official syndication wire keys from independently reviewed fields."""
    document = {"id_str": curated["id"], "text": curated["text"], "created_at": curated["created_at"],
                "user": {"id_str": curated["author"]["id"], "screen_name": curated["author"]["username"]}}
    quote = curated.get("quoted_tweet")
    if quote:
        document["quoted_tweet"] = official_embed(quote)
    poll = curated.get("poll")
    if poll:
        values = {"choice" + str(index) + "_label": {"string_value": option}
                  for index, option in enumerate(poll["options"], 1)}
        values["end_datetime_utc"] = {"string_value": poll["end_datetime_utc"]}
        values["counts_are_final"] = {"string_value": str(poll["counts_are_final"]).lower()}
        document["card"] = {"name": poll["card_name"], "binding_values": values}
    return document


class OneShotSourceTests(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads((ROOT / "tests/fixtures/source-items.json").read_text(encoding="utf-8"))
        enriched = json.loads((ROOT / "tests/fixtures/source-context.json").read_text(encoding="utf-8"))
        self.embeds = [official_embed(item) for item in enriched["items"]]
        self.stack = patch("socket.create_connection", side_effect=AssertionError("REAL_NETWORK_FORBIDDEN"))
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def transport(self, *, rows=None, embeds=None, status=201, totals=None):
        return Transport([Response(self.rows if rows is None else rows, status=status, totals=totals)] +
                         [Response(item) for item in (self.embeds if embeds is None else embeds)])

    def fetch(self, transport):
        return source.fetch_three(SECRET, transport, lambda: NOW)

    def rejects(self, transport, code):
        with self.assertRaises(source.OneShotSourceError) as raised:
            self.fetch(transport)
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn(SECRET, str(raised.exception))

    def test_real_observed_null_fields_and_same_three_context_are_preserved(self):
        transport = self.transport(totals=["3"])
        tweets, evidence = self.fetch(transport)
        self.assertEqual(len(tweets), 3)
        self.assertEqual([item["id"] for item in tweets], [row["tweetId"] for row in reversed(self.rows)])
        self.assertEqual([item["text"] for item in tweets], [row["text"] for row in reversed(self.rows)])
        self.assertEqual(tweets[1]["context"]["quoted_tweet"]["text"],
                         "Synthetic quoted context: an extra reset is planned; time unknown.")
        self.assertEqual(tweets[2]["context"]["poll"]["options"],
                         ["Synthetic option A", "Synthetic option B", "Show test results"])
        self.assertFalse(tweets[2]["context"]["poll"]["counts_are_final"])
        self.assertEqual(evidence["source_posts"], 1)
        self.assertEqual(evidence["official_context_gets"], 3)
        self.assertEqual(evidence["pagination_total"], 3)
        self.assertNotIn(SECRET, json.dumps((tweets, evidence)))
        self.assertEqual(transport.calls[0][0].get_method(), "POST")
        self.assertEqual(transport.calls[0][0].full_url, source.URL)
        self.assertEqual(json.loads(transport.calls[0][0].data), {"profiles": ["thsottiaux"], "maxResults": 3})
        self.assertEqual(transport.calls[0][0].get_header("Authorization"), "Bearer " + SECRET)
        for request, timeout in transport.calls[1:]:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(timeout, 20)
            self.assertIsNone(request.get_header("Authorization"))
            self.assertTrue(request.full_url.startswith(source.EMBED_BASE + "?id="))
        self.assertFalse(transport.responses)

    def test_http_200_and_missing_pagination_header_are_valid(self):
        tweets, evidence = self.fetch(self.transport(status=200))
        self.assertEqual(len(tweets), 3)
        self.assertFalse(evidence["pagination_total_present"])
        self.assertIsNone(evidence["pagination_total"])

    def test_more_than_three_is_rejected_before_any_context_or_local_trim(self):
        transport = self.transport(rows=self.rows + [self.rows[0]])
        self.rejects(transport, "ONE_SHOT_SOURCE_COUNT_INVALID")
        self.assertEqual(len(transport.calls), 1)

    def test_pagination_if_present_requires_one_bounded_finite_count(self):
        for totals, code in [(["4"], "ONE_SHOT_SOURCE_TOTAL_MISMATCH"),
                             (["03"], "ONE_SHOT_SOURCE_TOTAL_INVALID"),
                             (["3", "3"], "ONE_SHOT_SOURCE_TOTAL_INVALID")]:
            with self.subTest(totals=totals):
                transport = self.transport(totals=totals)
                self.rejects(transport, code)
                self.assertEqual(len(transport.calls), 1)

    def test_fresh_dataset_lagging_totals_do_not_discard_three_actual_items(self):
        for total in ('0', '1', '2'):
            with self.subTest(total=total):
                transport = self.transport(totals=[total])
                tweets, evidence = self.fetch(transport)
                self.assertEqual(len(tweets), 3)
                self.assertEqual(evidence['pagination_total'], int(total))
                self.assertFalse(evidence['pagination_total_matches_delivered'])
                self.assertEqual(len(transport.calls), 4)

    def test_duplicate_or_out_of_order_rows_stop_before_context_gets(self):
        for rows, code in [(self.rows[:2] + [self.rows[0]], "ONE_SHOT_SOURCE_DUPLICATE_ID"),
                           (list(reversed(self.rows)), "ONE_SHOT_SOURCE_ORDER_INVALID")]:
            with self.subTest(code=code):
                transport = self.transport(rows=rows)
                self.rejects(transport, code)
                self.assertEqual(len(transport.calls), 1)

    def test_stable_author_identity_and_nullable_username_cannot_conflict(self):
        for changes in [{"authorId": "123"}, {"authorUsername": "someone"},
                        {"author": {"id": source.TARGET_USER_ID, "username": "someone"}},
                        {"url": "https://x.com/someone/status/" + self.rows[0]["tweetId"]}]:
            rows = deepcopy(self.rows)
            rows[0].update(changes)
            transport = self.transport(rows=rows)
            with self.assertRaises(source.OneShotSourceError):
                self.fetch(transport)
            self.assertEqual(len(transport.calls), 1)

    def test_original_reply_is_included_and_all_text_is_kept(self):
        rows = deepcopy(self.rows)
        rows[0].update(isReply=True, inReplyToTweetId="2109045220411351517", inReplyToUsername="thsottiaux")
        tweets, evidence = self.fetch(self.transport(rows=rows))
        self.assertEqual(evidence["analysis_count"], 3)
        self.assertTrue(tweets[-1]["context"]["is_reply"])
        self.assertEqual(tweets[-1]["context"]["in_reply_to_tweet_id"], "2109045220411351517")

    def test_native_retweet_is_skipped_without_replacement_or_extra_get(self):
        rows = deepcopy(self.rows)
        rows[0].update(isRetweet=True, retweetedTweet={"text": "Native RT"})
        transport = self.transport(rows=rows, embeds=self.embeds[1:])
        tweets, evidence = self.fetch(transport)
        self.assertEqual(len(tweets), 2)
        self.assertEqual(evidence["delivered_count"], 3)
        self.assertEqual(evidence["native_retweet_ids_skipped"], [self.rows[0]["tweetId"]])
        self.assertEqual(evidence["official_context_gets"], 2)
        self.assertEqual(len(transport.calls), 3)

    def test_empty_response_has_one_post_zero_embeds(self):
        transport = self.transport(rows=[], embeds=[], totals=["0"])
        tweets, evidence = self.fetch(transport)
        self.assertEqual(tweets, [])
        self.assertEqual(evidence["official_context_gets"], 0)
        self.assertEqual(len(transport.calls), 1)

    def test_context_identity_time_original_text_and_truncation_fail_closed(self):
        cases = [({"id_str": "123"}, "ONE_SHOT_EMBED_ID_MISMATCH"),
                 ({"created_at": "2026-10-10T05:22:48Z"}, "ONE_SHOT_EMBED_TIME_MISMATCH"),
                 ({"text": "Wrong text"}, "ONE_SHOT_EMBED_TEXT_MISMATCH"),
                 ({"truncated": True}, "ONE_SHOT_EMBED_TEXT_TRUNCATED"),
                 ({"user": {"id_str": "123", "screen_name": "thsottiaux"}},
                  "ONE_SHOT_EMBED_IDENTITY_MISMATCH")]
        for changes, code in cases:
            with self.subTest(code=code):
                embeds = deepcopy(self.embeds)
                embeds[0].update(changes)
                transport = self.transport(embeds=embeds)
                self.rejects(transport, code)
                self.assertEqual(len(transport.calls), 2)

    def test_html_entity_representation_keeps_original_actor_text(self):
        rows, embeds = deepcopy(self.rows), deepcopy(self.embeds)
        rows[0]["text"] = "Codex & Work <ready>"
        embeds[0]["text"] = "Codex &amp; Work &lt;ready&gt;"
        tweets, evidence = self.fetch(self.transport(rows=rows, embeds=embeds))
        self.assertEqual(tweets[-1]["text"], rows[0]["text"])
        self.assertEqual(evidence["official_text_match_by_id"][rows[0]["tweetId"]], "HTML_UNESCAPED")

    def note_fixture(self):
        rows, embeds = deepcopy(self.rows), deepcopy(self.embeds)
        preview = "Codex &amp; Work: " + "x" * (280 - len("Codex &amp; Work: "))
        full_text = preview.replace("&amp;", "&") + " This ending contains the reset announcement."
        rows[0]["text"] = full_text
        embeds[0].update(text=preview, note_tweet={"id": "NoteTweet:synthetic"},
                         display_text_range=[0, 280], isStaleEdit=False)
        return rows, embeds

    def test_marked_note_preview_does_not_discard_full_text_or_context(self):
        rows, embeds = self.note_fixture()
        transport = self.transport(rows=rows, embeds=embeds)
        tweets, evidence = self.fetch(transport)
        self.assertEqual(tweets[-1]["text"], rows[0]["text"])
        self.assertIn("reset announcement", tweets[-1]["text"])
        self.assertEqual(len(tweets[-1]["context"]["poll"]["options"]), 3)
        self.assertEqual(evidence["official_text_match_by_id"][rows[0]["tweetId"]], "NOTE_PREVIEW_PREFIX")
        self.assertEqual(len(transport.calls), 4)

    def test_unmarked_short_stale_or_conflicting_preview_is_still_rejected(self):
        changes = [
            {"note_tweet": None}, {"note_tweet": {}}, {"isStaleEdit": True},
            {"display_text_range": [1, 280]}, {"display_text_range": [False, 280]},
            {"text": "Codex &amp; Work", "display_text_range": [0, 16]},
            {"full_text": "Conflicting authoritative full text"},
        ]
        for change in changes:
            with self.subTest(change=change):
                rows, embeds = self.note_fixture()
                embeds[0].update(change)
                self.rejects(self.transport(rows=rows, embeds=embeds), "ONE_SHOT_EMBED_TEXT_MISMATCH")
        rows, embeds = self.note_fixture()
        rows[0]["text"] = "Different beginning " + rows[0]["text"]
        self.rejects(self.transport(rows=rows, embeds=embeds), "ONE_SHOT_EMBED_TEXT_MISMATCH")

    def test_note_preview_ranges_count_utf16_units(self):
        rows, embeds = self.note_fixture()
        preview = "\U0001f680" + "x" * 278
        embeds[0]["text"] = preview
        rows[0]["text"] = preview + " Full long-form ending."
        tweets, _ = self.fetch(self.transport(rows=rows, embeds=embeds))
        self.assertEqual(tweets[-1]["text"], rows[0]["text"])

    def test_missing_quote_is_not_silently_replaced_by_actor_null(self):
        embeds = deepcopy(self.embeds)
        embeds[1].pop("quoted_tweet")
        transport = self.transport(embeds=embeds)
        self.rejects(transport, "ONE_SHOT_EMBED_QUOTE_MISSING")
        self.assertEqual(len(transport.calls), 3)

    def test_failed_requests_do_not_retry_and_errors_do_not_reflect_credentials(self):
        transport = Transport([RuntimeError("provider echoed " + SECRET)])
        self.rejects(transport, "ONE_SHOT_SOURCE_REQUEST_FAILED")
        self.assertEqual(len(transport.calls), 1)
        transport = self.transport()
        transport.responses[1] = RuntimeError("provider echoed " + SECRET)
        self.rejects(transport, "ONE_SHOT_EMBED_REQUEST_FAILED")
        self.assertEqual(len(transport.calls), 2)

    def test_secret_reflection_and_oversize_json_or_duplicate_keys_stop(self):
        for body, code in [(('["' + SECRET + '"]').encode(), "ONE_SHOT_SOURCE_SECRET_REFLECTION"),
                           (b"x" * (source.MAX_RESPONSE_BYTES + 1), "ONE_SHOT_SOURCE_BODY_INVALID"),
                           (b'[{"a":1,"a":2}]', "ONE_SHOT_SOURCE_JSON_INVALID")]:
            with self.subTest(code=code):
                transport = Transport([Response(body)])
                self.rejects(transport, code)
                self.assertEqual(len(transport.calls), 1)

    def test_bad_token_or_clock_fail_before_any_paid_request(self):
        for token, clock in [("bad\nsecret", lambda: NOW), (SECRET, lambda: datetime(2026, 10, 11))]:
            transport = Transport([])
            with self.assertRaises(source.OneShotSourceError):
                source.fetch_three(token, transport, clock)
            self.assertEqual(transport.calls, [])

    def test_array_binding_values_and_boolean_poll_flag_are_supported(self):
        embeds = deepcopy(self.embeds)
        card = embeds[0]["card"]
        card["binding_values"]["counts_are_final"] = {"boolean_value": False}
        card["binding_values"] = [{"key": key, "value": value}
                                  for key, value in card["binding_values"].items()]
        tweets, _ = self.fetch(self.transport(embeds=embeds))
        self.assertEqual(len(tweets[-1]["context"]["poll"]["options"]), 3)

    def test_poll_in_a_quoted_post_is_preserved_without_another_get(self):
        embeds = deepcopy(self.embeds)
        embeds[1]["quoted_tweet"]["card"] = deepcopy(embeds[0]["card"])
        transport = self.transport(embeds=embeds)
        tweets, evidence = self.fetch(transport)
        self.assertEqual(tweets[1]["context"]["quoted_tweet"]["poll"]["options"],
                         ["Synthetic option A", "Synthetic option B", "Show test results"])
        self.assertEqual(evidence["official_context_gets"], 3)
        self.assertEqual(len(transport.calls), 4)


if __name__ == "__main__":
    unittest.main()
