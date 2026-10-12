"""Offline checks for the user-specified four-field model response contract."""
from datetime import datetime, timezone
import json
import unittest

from monitor.classification_prompt import SYSTEM_PROMPT
from monitor.classifier import (
    ClassificationError, ClassifierReply, build_prompt, parse_batch_classifications,
    parse_classification, validate_batch_results,
)
from monitor.notifier import build_notification
from scripts import analyze_recovered_three as batch
from scripts import test_full_chain_once as chain


NOW = datetime(2026, 10, 11, 5, tzinfo=timezone.utc)
TWEET = {"id": "101", "text": "Put the reset in the bag. All propagated.",
         "created_at": "2026-10-11T04:01:09Z", "author": "example",
         "url": "https://x.com/example/status/101", "context": {
             "quoted_tweet": {"text": "Global reset by EOD.", "created_at": "2026-10-10T22:16:20Z"}}}


def row(identifier="1", label="POST_RESET", **updates):
    value = {"id": identifier, "label": label, "reset_time": None,
             "reason": '主文用"All propagated"表示已生效；未给出具体时刻，时间不明。'}
    value.update(updates)
    return value


class PromptContractTests(unittest.TestCase):
    def test_single_and_batch_entries_use_same_prompt_and_complete_time_context(self):
        messages = [build_prompt(TWEET, NOW, "Asia/Shanghai")]
        for body in (batch._request_body([TWEET], NOW), chain._body([TWEET], NOW)):
            messages.append(json.loads(body)["messages"])
        for pair in messages:
            self.assertEqual(pair[0], {"role": "system", "content": SYSTEM_PROMPT})
            submitted = json.loads(pair[1]["content"])["tweets"]
            self.assertEqual(submitted[0]["id"], "1")
            self.assertEqual(submitted[0]["text"], TWEET["text"])
            self.assertEqual(submitted[0]["created_at"], TWEET["created_at"])
            self.assertEqual(submitted[0]["quoted_tweet"], TWEET["context"]["quoted_tweet"])
        self.assertNotIn("reset_confirmed", SYSTEM_PROMPT)
        self.assertNotIn("confidence", SYSTEM_PROMPT)

    def test_each_four_class_label_round_trips_without_extra_model_fields(self):
        for label in ("POST_RESET", "PRE_RESET", "POSSIBLE_RESET", "IRRELEVANT"):
            expected = row(label=label)
            reply = ClassifierReply(json.dumps({"results": [expected]}))
            self.assertEqual(parse_batch_classifications(reply, ("1",)), [expected])
            self.assertEqual(parse_classification(reply), {key: val for key, val in expected.items() if key != "id"})

    def test_exact_fields_unique_ids_and_input_order_are_required(self):
        valid = {"results": [row("1"), row("2", "PRE_RESET")]}
        invalid = [dict(valid, recipient="evil@example.invalid"), {"results": valid["results"][:1]},
                   {"results": list(reversed(valid["results"]))},
                   {"results": [row("1"), row("1")]}, {"results": [row("101"), row("2")]},
                   {"results": [row("1", confidence=0.9), row("2")]},
                   {"results": [row("1", reset_confirmed=True), row("2")]},
                   {"results": [row(1), row("2")]}, {"results": [row("1", label=True), row("2")]},
                   {"results": [row("1", label="RESET"), row("2")]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ClassificationError):
                validate_batch_results(value, ("1", "2"))
        self.assertEqual(validate_batch_results(valid, ("1", "2")), valid["results"])

    def test_reset_time_accepts_null_or_explicit_offset_and_rejects_guesses(self):
        for value in (None, "2026-10-11T18:00:00+08:00", "2026-10-11T10:00:00Z"):
            result = validate_batch_results({"results": [row(reset_time=value)]}, ("1",))[0]
            self.assertEqual(result["reset_time"], value.replace("Z", "+00:00") if value else None)
        for value in ("soon", "tonight", "by EOD", "下周一", "2026-10-11T18:00:00",
                      "2026-10-11", "2026-10-11T18:00:00+00:60", "2026-02-31T18:00:00+08:00", 1, True):
            with self.subTest(value=value), self.assertRaises(ClassificationError):
                validate_batch_results({"results": [row(reset_time=value)]}, ("1",))

    def test_reason_two_hundred_character_boundary_is_enforced(self):
        expected = row(reason="文" * 200)
        self.assertEqual(validate_batch_results({"results": [expected]}, ("1",)), [expected])
        for reason in ("文" * 201, "", " \n ", None, 1):
            with self.subTest(reason=reason), self.assertRaises(ClassificationError):
                validate_batch_results({"results": [row(reason=reason)]}, ("1",))

    def test_strict_json_and_completion_are_required(self):
        valid = json.dumps({"results": [row()]})
        for content in ("```json\n"+valid+"\n```", valid+"{}", '{"results":[],"results":[]}',
                        '{"results":[{"id":"1","label":"POST_RESET","reset_time":NaN,"reason":"文"}]}'):
            with self.subTest(content=content), self.assertRaises(ClassificationError):
                parse_batch_classifications(ClassifierReply(content), ("1",))
        with self.assertRaises(ClassificationError):
            parse_batch_classifications(ClassifierReply(valid, "length"), ("1",))

    def test_new_classification_can_notify_without_invented_confidence(self):
        classification = parse_classification(ClassifierReply(json.dumps({"results": [row()]})))
        notification = build_notification(TWEET, classification, "owner@example.com", now=NOW)
        self.assertIn("POST_RESET", notification["body"])
        self.assertNotIn("置信度", notification["body"])
        self.assertNotIn("confidence", classification)


if __name__ == "__main__":
    unittest.main()
