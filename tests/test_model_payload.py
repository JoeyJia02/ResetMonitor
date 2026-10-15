"""Model payload tests run on synthetic data without external calls."""
from copy import deepcopy
import json
import unittest

from monitor.model_payload import PayloadError, pack_model_tweets


TIME = "2026-10-11T04:01:09.000Z"


def tweet(identifier="2109132167112782291", **changes):
    value = {"id": identifier, "created_at": TIME, "text": "All propagated.",
             "author": "SYNTHETIC_AUTHOR", "author_id": "12345",
             "url": "https://x.com/SYNTHETIC_AUTHOR/status/" + str(identifier),
             "context": None, "likes": 42, "views": 123, "media": ["SYNTHETIC_MEDIA"],
             "profile": {"name": "SYNTHETIC_PROFILE"}, "isReply": True,
             "field_provenance": "SYNTHETIC_PROVENANCE"}
    value.update(changes)
    return value


class ModelPayloadTests(unittest.TestCase):
    def test_primary_only_marker_excludes_all_incomplete_context_and_diagnostics(self):
        context = {'context_unavailable': True, 'quoted_tweet': {'text': 'DO_NOT_USE_PARTIAL_QUOTE'},
                   'poll': {'options': ['DO_NOT_USE_PARTIAL_POLL']}, 'error_code': 'LOCAL_DIAGNOSTIC'}
        original = tweet(context=context)
        packed, _ = pack_model_tweets([original, tweet('102', context={'quoted_tweet': {'text': 'VALID_QUOTE'}})])
        self.assertEqual(packed[0], {'id': '1', 'text': original['text'], 'created_at': TIME,
                                    'context_unavailable': True})
        self.assertEqual(packed[1]['quoted_tweet'], {'text': 'VALID_QUOTE'})
        self.assertNotIn('LOCAL_DIAGNOSTIC', json.dumps(packed))
        self.assertNotIn('DO_NOT_USE', json.dumps(packed))
        for value in ('true', 1, None):
            self.assert_invalid([tweet(context={'context_unavailable': value})])

    def test_upgrade_keeps_original_semantics_new_evidence_and_basis(self):
        context = {'original_context':{'quoted_tweet':{'text':'ORIGINAL_QUOTE'},
                    'poll':{'options':['ORIGINAL_POLL_A','ORIGINAL_POLL_B'],'votes':[1,2]}, 'likes':99},
                   'new_evidence':'  NEW_CONFIRMATION\ncomplete  ',
                   'reclassification_basis':'NEW_BASIS'}
        packed,_ = pack_model_tweets([tweet(context=json.dumps(context))])
        self.assertEqual(packed[0]['original_context'], {'quoted_tweet':{'text':'ORIGINAL_QUOTE'},
            'poll_options':['ORIGINAL_POLL_A','ORIGINAL_POLL_B']})
        self.assertEqual(packed[0]['new_evidence'], context['new_evidence'])
        self.assertEqual(packed[0]['reclassification_basis'], 'NEW_BASIS')
        self.assertNotIn('likes', json.dumps(packed))
        self.assertNotIn('votes', json.dumps(packed))

    def test_upgrade_plain_original_context_and_cycles_are_handled(self):
        packed,_ = pack_model_tweets([tweet(context={'original_context':'Complete legacy context',
            'new_evidence':'Confirmed','reclassification_basis':'New evidence'})])
        self.assertEqual(packed[0]['original_context'], {'context_text':'Complete legacy context'})
        context = {}
        context['original_context'] = context
        self.assert_invalid([tweet(context=context)])
        self.assert_invalid([tweet(context={'new_evidence':''})])

    def assert_invalid(self, value):
        with self.assertRaises(PayloadError) as caught:
            pack_model_tweets(value)
        self.assertEqual(caught.exception.code, "MODEL_PAYLOAD_INVALID")
        self.assertEqual(str(caught.exception), "MODEL_PAYLOAD_INVALID")
        self.assertNotIn("SYNTHETIC", repr(caught.exception))

    def test_minimal_rows_short_indices_and_local_original_id_map(self):
        tweets = [tweet("101"), tweet("102"), tweet("103")]
        packed, mapping = pack_model_tweets(tweets)
        self.assertEqual(mapping, {"1": "101", "2": "102", "3": "103"})
        self.assertEqual(packed, [{"id": str(index), "created_at": TIME, "text": "All propagated."}
                                 for index in range(1, 4)])
        serialized = json.dumps(packed)
        for excluded in ("author", "url", "likes", "views", "media", "profile", "isReply", "provenance", "101"):
            self.assertNotIn(excluded, serialized)

    def test_canonical_quote_keeps_only_complete_text_time_nested_quote_and_options(self):
        context = {"quoted_tweet": {"id": "555", "text": "Reset by EOD.", "created_at": TIME,
                   "author": {"name": "SYNTHETIC_QUOTE_AUTHOR"}, "url": "SYNTHETIC_URL",
                   "in_reply_to_status_id": "444", "provenance": "SYNTHETIC_SOURCE",
                   "poll": {"options": ["yes", "no"], "counts": [7, 6], "end_datetime_utc": TIME},
                   "quoted_tweet": {"text": "Tomorrow?", "created_at": TIME, "id": "333"}},
                   "is_quote": True, "is_reply": False}
        packed, _ = pack_model_tweets([tweet(context=context)])
        self.assertEqual(packed[0]["quoted_tweet"], {
            "text": "Reset by EOD.", "created_at": TIME,
            "quoted_tweet": {"text": "Tomorrow?", "created_at": TIME},
            "poll_options": ["yes", "no"]})
        self.assertNotIn("SYNTHETIC", json.dumps(packed))

    def test_legacy_json_context_and_quotedTweet_without_quote_timestamp(self):
        context = json.dumps({"quotedTweet": {"text": "Complete quote with no time", "id": "123"},
                              "inReplyToTweetId": "123", "inReplyToUsername": "SYNTHETIC_NAME"})
        packed, _ = pack_model_tweets([tweet(context=context)])
        self.assertEqual(packed[0]["quoted_tweet"], {"text": "Complete quote with no time"})
        self.assertNotIn("in_reply_to", packed[0])
        self.assertNotIn("SYNTHETIC", json.dumps(packed))

    def test_nested_legacy_quote_alias_and_createdAt(self):
        context = {"quotedTweet": {"text": "Outer", "createdAt": TIME,
                                   "quotedTweet": {"full_text": "Inner"}}}
        packed, _ = pack_model_tweets([tweet(context=context)])
        self.assertEqual(packed[0]["quoted_tweet"], {
            "text": "Outer", "created_at": TIME, "quoted_tweet": {"text": "Inner"}})

    def test_unstructured_legacy_context_keeps_full_text_without_invented_relation(self):
        original = "  已获取上下文。\nThe whole original context.  "
        packed, _ = pack_model_tweets([tweet(context=original)])
        self.assertEqual(packed[0]["context_text"], original)
        self.assertNotIn("in_reply_to", packed[0])
        self.assertNotIn("quoted_tweet", packed[0])

    def test_poll_retains_only_option_text_without_counts_or_end_time(self):
        for poll in ({"options": ["5h limits, 2X usage", "same usage"], "counts": [123, 456],
                      "end_datetime_utc": TIME, "counts_are_final": False},
                     {"options": [{"label": "5h limits, 2X usage", "votes": 123},
                                  {"text": "same usage", "count": 456}]}):
            packed, _ = pack_model_tweets([tweet(context={"poll": poll})])
            self.assertEqual(packed[0]["poll_options"], ["5h limits, 2X usage", "same usage"])
            self.assertEqual(set(packed[0]), {"id", "text", "created_at", "poll_options"})

    def test_direct_poll_options_are_supported(self):
        packed, _ = pack_model_tweets([tweet(context={"poll_options": ["option a", "option b"]})])
        self.assertEqual(packed[0]["poll_options"], ["option a", "option b"])

    def test_existing_reply_parent_text_is_retained_without_fetch_or_reply_metadata(self):
        for name in ("reply_parent", "replied_tweet"):
            context = {name: {"text": "When will it reset?", "created_at": TIME, "id": "555",
                              "author": "SYNTHETIC_NAME", "quoted_tweet": {"text": "Complete related quote"}},
                       "in_reply_to_tweet_id": "555", "in_reply_to_username": "SYNTHETIC_NAME"}
            packed, _ = pack_model_tweets([tweet(context=context)])
            self.assertEqual(packed[0]["in_reply_to"], {"text": "When will it reset?", "created_at": TIME,
                'quoted_tweet': {'text': 'Complete related quote'}})
            self.assertNotIn("SYNTHETIC", json.dumps(packed))
            self.assertIn("Complete related quote", json.dumps(packed))

    def test_full_text_overrides_truncated_summary_for_tweet_quote_and_reply(self):
        post = {'text':'Short…', 'full_text':'The complete original text.', 'truncated':True}
        packed, _ = pack_model_tweets([tweet(**post, context={'quoted_tweet':post, 'reply_parent':post})])
        self.assertEqual(packed[0]['text'], post['full_text'])
        self.assertEqual(packed[0]['quoted_tweet']['text'], post['full_text'])
        self.assertEqual(packed[0]['in_reply_to']['text'], post['full_text'])
        self.assertNotIn('Short…', json.dumps(packed, ensure_ascii=False))

    def test_declared_truncation_without_complete_text_is_rejected(self):
        for post in ({'text':'Short…','truncated':True},
                     {'text':'Short…','full_text':'','truncated':True},
                     {'text':'Text','truncated':'true'}):
            self.assert_invalid([tweet(**post)])
            self.assert_invalid([tweet(context={'quoted_tweet':post})])
            self.assert_invalid([tweet(context={'reply_parent':post})])

    def test_reply_parent_keeps_its_quote_and_poll_semantics(self):
        parent = {'text':'Which reset?', 'quoted_tweet':{'text':'Reset by EOD.'},
                  'poll':{'options':['Already happened','Still waiting'],'votes':[1,2]}, 'likes':10}
        packed, _ = pack_model_tweets([tweet(context={'reply_parent':parent})])
        self.assertEqual(packed[0]['in_reply_to'], {'text':'Which reset?',
            'quoted_tweet':{'text':'Reset by EOD.'}, 'poll_options':['Already happened','Still waiting']})
        self.assertNotIn('votes', json.dumps(packed))
        self.assertNotIn('likes', json.dumps(packed))

    def test_no_text_truncation_or_mutation_and_output_containers_are_detached(self):
        complete = "  Full original\n" + "完整内容" * 30_000 + "\nend  "
        original = [tweet(text=complete, context={"quoted_tweet": {"text": complete},
                         "poll": {"options": ["  One  ", "Two\nthree"]}})]
        before = deepcopy(original)
        packed, _ = pack_model_tweets(original)
        self.assertEqual(original, before)
        self.assertEqual(packed[0]["text"], complete)
        self.assertEqual(packed[0]["quoted_tweet"]["text"], complete)
        packed[0]["poll_options"][0] = "changed"
        packed[0]["quoted_tweet"]["text"] = "changed"
        self.assertEqual(original, before)

    def test_invalid_count_shape_duplicate_ids_and_invalid_ids(self):
        for value in (None, {}, (), [], [tweet()] * 4, [None], [tweet("101"), tweet("101")],
                      [tweet(101)], [tweet("")], [tweet("0")], [tweet("1" * 21)],
                      [tweet(str(1 << 64))], [tweet("SYNTHETIC_SECRET")]):
            self.assert_invalid(value)

    def test_missing_text_empty_text_invalid_time_and_naive_time_are_rejected(self):
        for changes in ({"text": ""}, {"text": " \n"}, {"text": None}, {"text": 123},
                        {"created_at": None}, {"created_at": "2026-10-11T04:01:09"},
                        {"created_at": "2026-02-31T04:01:09Z"},
                        {"created_at": "2026-10-11T04:01:09+00:60"}):
            self.assert_invalid([tweet(**changes)])

    def test_invalid_legacy_context_json_duplicate_keys_or_nonfinite_are_rejected(self):
        for context in ("", "[1]", "null", "{", "true", "123", [], False,
                        '{"quoted_tweet":null,"quoted_tweet":{"text":"SYNTHETIC"}}',
                        '{"poll":NaN}'):
            self.assert_invalid([tweet(context=context)])

    def test_malformed_quote_reply_or_poll_is_rejected_without_silent_omission(self):
        for context in ({"quoted_tweet": []}, {"quoted_tweet": {"text": ""}},
                        {"quoted_tweet": {"text": "quote", "created_at": "invalid"}},
                        {"reply_parent": "SYNTHETIC"}, {"replied_tweet": {"id": "123"}},
                        {"poll": False}, {"poll": {}}, {"poll": {"options": None}},
                        {"poll": {"options": []}},
                        {"poll_options": [""]}, {"poll_options": [123]},
                        {"poll_options": [{"votes": 7}]}):
            self.assert_invalid([tweet(context=context)])

    def test_null_optional_fields_are_omitted_and_alias_fallback_is_supported(self):
        context = {"quoted_tweet": None, "quotedTweet": {"text": "quote"},
                   "poll": None, "reply_parent": None, "replied_tweet": None}
        packed, _ = pack_model_tweets([tweet(context=context)])
        self.assertEqual(set(packed[0]), {"id", "text", "created_at", "quoted_tweet"})

    def test_cycles_and_excessive_nested_quotes_have_fixed_error(self):
        cycle = {"text": "SYNTHETIC"}
        cycle["quoted_tweet"] = cycle
        self.assert_invalid([tweet(context={"quoted_tweet": cycle})])
        nested = {"text": "deep"}
        for _ in range(10):
            nested = {"text": "outer", "quoted_tweet": nested}
        self.assert_invalid([tweet(context={"quoted_tweet": nested})])


if __name__ == "__main__":
    unittest.main()
