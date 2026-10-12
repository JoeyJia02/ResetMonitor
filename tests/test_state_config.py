from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import unittest
import uuid
from unittest.mock import patch

from config import Config, ConfigError

from monitor.local_settings import RECIPIENT as LOCAL_RECIPIENT
from monitor.locking import LockBusy, ProcessLock
from monitor.state import StateError, StateStore, initial_state, validate_state


NOW = "2026-10-10T02:00:00+00:00"
TOMORROW = "2026-10-11T02:00:00+00:00"
TEST_TMP = Path(os.environ.get(
    "RESET_MONITOR_TEST_ROOT",
    str(Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"),
)).resolve()


def lock_child_environment():
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("TWITTER_", "APIFY_", "DEEPSEEK_", "SMTP_", "EMAIL_", "OPENAI_", "ANTHROPIC_", "AWS_", "AZURE_")) or upper.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET")):
            env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


LOCK_CHILD_BOOTSTRAP = """
import os
import sys
if os.environ.get('MONITOR_TEST_VERIFY_LOCK_CREDENTIAL_ISOLATION') == '1':
    absent = (
        'TWITTER_API_KEY', 'APIFY_TOKEN', 'DEEPSEEK_API_KEY', 'SMTP_PASSWORD',
        'SMTP_USER', 'EMAIL_TO', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
        'AWS_ACCESS_KEY_ID', 'AZURE_CLIENT_SECRET', 'CUSTOM_API_KEY',
        'CUSTOM_TOKEN', 'CUSTOM_PASSWORD', 'CUSTOM_SECRET',
    )
    if (any(name in os.environ for name in absent)
            or os.environ.get('PYTHONDONTWRITEBYTECODE') != '1'
            or os.environ.get('MONITOR_TEST_LOCK_PUBLIC_CONTROL') != 'permitted-offline-control'
            or any('RESET_MONITOR_LOCK_SENTINEL_' in value for value in os.environ.values())):
        sys.stderr.write('LOCK_CHILD_CREDENTIAL_ISOLATION_FAILED\\n')
        sys.stderr.flush()
        raise SystemExit(93)
    sys.stderr.write('LOCK_CHILD_CREDENTIAL_ISOLATION_VERIFIED\\n')
    sys.stderr.flush()
"""


class TestDirectory:
    """Use inherited ACLs; Python 3.14 mode-700 temp dirs reject sandbox access."""
    def __init__(self):
        self.path = TEST_TMP / ("state-config-" + uuid.uuid4().hex)
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def cleanup(self):
        resolved = self.path.resolve()
        intended = TEST_TMP.resolve()
        if resolved == intended or not resolved.is_relative_to(intended):
            raise RuntimeError("Refusing to clean a path outside the test workspace")
        shutil.rmtree(resolved)


def notification(identifier="11", label="PRE_RESET", status="PENDING"):
    key = f"{identifier}:{label}"
    return {
        "key": key, "tweet_id": identifier, "label": label,
        "recipient": "demo@example.invalid", "subject": "Reset", "body": "Original tweet",
        "message_id": f"<{identifier}-{label}@example.invalid>", "created_at": NOW,
        "status": status, "attempts": 0, "failure_count": 0, "retryable": True,
        "next_retry_at": TOMORROW if status == "UNKNOWN" else None, "sent_at": NOW if status == "SENT" else None,
        "last_error": None, "unknown_retry_used": 0, "uncertain_since": NOW if status == "UNKNOWN" else None,
        "needs_attention": False, "had_unknown": status == "UNKNOWN",
    }


def add_notification(state, item):
    state["pending_notifications"][item["key"]] = item
    if item["status"] == "SENT":
        state["notified_ids"][item["key"]] = {"sent_at": item["sent_at"], "message_id": item["message_id"]}


def raw_tweet(identifier="11", text="Original raw text"):
    return {"id": identifier, "text": text, "created_at": NOW,
            "author": "thsottiaux", "url": f"https://x.com/thsottiaux/status/{identifier}", "context": None}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def test_defaults_are_offline_and_resolve_to_project(self):
        config = Config.from_env({}, self.root)
        self.assertEqual(config.state_path, self.root / "state.json")
        self.assertEqual(config.target_username, "thsottiaux")
        self.assertEqual(config.email_to, LOCAL_RECIPIENT)
        self.assertEqual(config.fetch_limit, 3)
        self.assertEqual(config.schedule_timezone, "Asia/Shanghai")
        self.assertEqual(config.schedule_times, ("08:00", "13:00", "20:00"))
        self.assertEqual((config.retry_attempts, config.retry_delays), (3, (5, 15)))
        self.assertEqual((config.failure_cooldown_seconds, config.unknown_cooldown_seconds), (7200, 86400))

    def test_relative_path_does_not_depend_on_cwd(self):
        config = Config.from_env({"STATE_FILE": "runtime/custom.json"}, self.root)
        self.assertEqual(config.state_path, self.root / "runtime" / "custom.json")
        self.assertTrue(config.state_path.is_absolute())

    def test_explicit_absolute_path_is_preserved(self):
        chosen = self.root / "absolute.json"
        self.assertEqual(Config.from_env({"STATE_FILE": str(chosen)}, self.root).state_path, chosen)

    def test_fixed_schedule_and_legacy_interval_rejection(self):
        config = Config.from_env({"SCHEDULE_TIMEZONE": "Asia/Shanghai",
                                  "SCHEDULE_TIMES": "08:00,13:00,20:00"}, self.root)
        self.assertEqual(config.schedule_times, ("08:00", "13:00", "20:00"))
        for env in ({"CHECK_INTERVAL_HOURS": "2"}, {"SCHEDULE_TIMEZONE": "UTC"},
                    {"SCHEDULE_TIMES": "08:00,13:00"},
                    {"SCHEDULE_TIMES": "08:00,13:00,20:00,22:00"},
                    {"SCHEDULE_TIMES": "08:00,08:00,20:00"},
                    {"SCHEDULE_TIMES": "08:00,12:00,16:00,20:00"},
                    {"SCHEDULE_TIMES": "08:00,14:00,20:00"},
                    {"SCHEDULE_TIMES": "02:00,08:00,13:00,20:00"}):
            with self.subTest(fields=tuple(env)), self.assertRaises(ConfigError):
                Config.from_env(env, self.root)

    def test_invalid_numeric_values_exclude_supplied_secret(self):
        for name, value in (("CHECK_INTERVAL_HOURS", "3"), ("CHECK_INTERVAL_HOURS", "secret-token"), ("FETCH_LIMIT", "20"), ("FETCH_LIMIT", "3.0")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError) as caught:
                Config.from_env({name: value}, self.root)
            self.assertNotIn("secret-token", str(caught.exception))

    def test_single_plain_email_only(self):
        valid = "me@example.com"
        self.assertEqual(Config.from_env({"EMAIL_TO": valid}, self.root).email_to, valid)
        for value in ("", "a@example.com,b@example.com", "a@example.com;b@example.com", "Name <a@example.com>", "a@example.com\r\nBcc:x@example.com", "a@example.com\n", "a @example.com"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_env({"EMAIL_TO": value}, self.root)

    def test_recipient_rejects_notifier_unsafe_characters_without_echoing(self):
        for recipient in ("a:b@example.com", "a@b:example.com", "a\x00b@example.com", "a\x01b@example.com", "a@example.com\x1f", "a@example.com\x7f", "a@example"):
            with self.subTest(recipient=repr(recipient)), self.assertRaises(ConfigError) as caught:
                Config.from_env({"EMAIL_TO": recipient}, self.root)
            self.assertNotIn(recipient, str(caught.exception))

    def test_invalid_path_and_username(self):
        for env in ({"STATE_FILE": ""}, {"STATE_FILE": "."}, {"STATE_FILE": "bad\0path"}, {"TARGET_USERNAME": "bad user"}, {"TARGET_USERNAME": "x" * 16}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                Config.from_env(env, self.root)
        self.assertEqual(Config.from_env({"TARGET_USERNAME": "@thsottiaux"}, self.root).target_username, "thsottiaux")

    def test_non_string_environment_is_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"FETCH_LIMIT": 3}, self.root)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.store = StateStore(self.path)

    def test_missing_returns_none_without_creating_files(self):
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_initial_state_and_round_trip(self):
        state = initial_state("thsottiaux")
        validate_state(state)
        self.assertEqual(state["revision"], 0)
        self.store.save(state)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(self.store.load(), state)
        self.assertFalse(self.store.backup_path.exists())

    def test_backup_contains_previous_valid_generation(self):
        state = initial_state("thsottiaux")
        self.store.save(state)
        previous = self.path.read_bytes()
        state["last_fetch_at"] = NOW
        self.store.save(state)
        self.assertEqual(self.store.backup_path.read_bytes(), previous)
        self.assertEqual(state["revision"], 2)
        self.assertEqual(json.loads(previous)["revision"], 1)

    def test_failed_replace_does_not_mutate_revision_or_main(self):
        state = initial_state("thsottiaux")
        self.store.save(state)
        previous = self.path.read_bytes()
        original_replace = os.replace

        def reject_main(source, destination):
            if Path(destination) == self.path:
                raise OSError("simulated disk failure")
            return original_replace(source, destination)

        with patch("monitor.state.os.replace", side_effect=reject_main), self.assertRaises(StateError):
            self.store.save(state)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(self.store.load()["revision"], 1)

    def test_failed_fsync_does_not_commit_or_increment(self):
        state = initial_state("thsottiaux")
        with patch("monitor.state.os.fsync", side_effect=OSError("simulated fsync failure")), self.assertRaises(StateError):
            self.store.save(state)
        self.assertEqual(state["revision"], 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_state_does_not_replace_valid_state(self):
        state = initial_state("thsottiaux")
        self.store.save(state)
        old = self.path.read_bytes()
        state["last_seen_id"] = "12"
        with self.assertRaises(StateError):
            self.store.save(state)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(state["revision"], 1)

    def test_stale_snapshot_is_rejected(self):
        state = initial_state("thsottiaux")
        self.store.save(state)
        stale = deepcopy(state)
        self.store.save(state)
        with self.assertRaises(StateError):
            self.store.save(stale)
        self.assertEqual(stale["revision"], 1)

    def test_corruption_requires_explicit_recovery(self):
        self.path.write_bytes(b"{broken")
        with self.assertRaises(StateError):
            self.store.load()
        self.assertEqual(self.path.read_bytes(), b"{broken")
        with self.assertRaises(StateError):
            self.store.load(recover=True)

    def test_recovery_preserves_sent_evidence_and_holds_all_unsent(self):
        state = initial_state("thsottiaux")
        for offset, status in enumerate(("PENDING", "SENDING", "FAILED", "UNKNOWN", "SENT"), 11):
            add_notification(state, notification(str(offset), status=status))
        self.store.save(state)
        self.store.save(state)
        backup = self.store.backup_path.read_bytes()
        self.path.write_bytes(b"{corrupted main")
        recovered = self.store.load(recover=True)
        self.assertTrue(recovered["recovered_from_backup"])
        self.assertTrue(recovered["recovery_required"])
        self.assertTrue(self.store.recovered_from_backup)
        self.assertEqual(self.path.read_bytes(), b"{corrupted main")
        for key, item in recovered["pending_notifications"].items():
            if item["tweet_id"] == "15":
                self.assertEqual(item["status"], "SENT")
                self.assertIn(key, recovered["notified_ids"])
            else:
                self.assertEqual(item["status"], "UNKNOWN")
                self.assertTrue(item["needs_attention"])
                self.assertTrue(item["had_unknown"])
                self.assertEqual(item["unknown_retry_used"], 1)
                self.assertIsNone(item["next_retry_at"])
        self.store.save(recovered)
        self.assertEqual(self.store.backup_path.read_bytes(), backup)
        archives = list(self.path.parent.glob("state.json.corrupt-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), b"{corrupted main")
        self.assertTrue(StateStore(self.path).load()["recovery_required"])

    def test_missing_main_with_backup_is_not_a_new_installation(self):
        state = initial_state("thsottiaux")
        self.store.save(state)
        self.store.save(state)
        self.path.unlink()
        with self.assertRaises(StateError):
            self.store.load()
        recovered = self.store.load(recover=True)
        self.assertTrue(recovered["recovery_required"])
        self.store.save(recovered)
        self.assertTrue(self.path.exists())

    def test_double_corruption_stops_without_modifying_either_file(self):
        self.path.write_bytes(b"bad main")
        self.store.backup_path.write_bytes(b"bad backup")
        with self.assertRaises(StateError):
            self.store.load(recover=True)
        self.assertEqual(self.path.read_bytes(), b"bad main")
        self.assertEqual(self.store.backup_path.read_bytes(), b"bad backup")

    def test_json_duplicate_keys_and_non_finite_constants_are_rejected(self):
        state = initial_state("thsottiaux")
        serialized = json.dumps(state)
        for content in (serialized[:-1] + ', "revision": 2}', serialized.replace('"revision": 0', '"revision": NaN')):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(StateError):
                self.store.load()

    def test_basic_types_schema_and_cursor_consistency(self):
        for field, value in (("schema_version", True), ("schema_version", 4), ("revision", True), ("revision", -1), ("initialized", 1), ("coverage_mode", "ALL"), ("inbox", []), ("last_fetch_at", "2026-10-10T02:00:00")):
            state = initial_state("thsottiaux")
            state[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(StateError):
                validate_state(state)
        for identifier in (12, 1.5, "0", "-1", "01", "1e10", str(1 << 64)):
            state = initial_state("thsottiaux")
            state.update(initialized=True, initial_baseline_id=identifier, last_seen_id=identifier)
            with self.subTest(identifier=identifier), self.assertRaises(StateError):
                validate_state(state)
        state = initial_state("thsottiaux")
        state.update(initialized=True, initial_baseline_id="12", last_seen_id="11")
        with self.assertRaises(StateError):
            validate_state(state)

    def test_sent_ledger_and_unknown_budget_consistency(self):
        for status, budget in (("BOGUS", 0), ("PENDING", True), ("PENDING", 2), ([], 0)):
            state = initial_state("thsottiaux")
            item = notification()
            item.update(status=status, unknown_retry_used=budget)
            state["pending_notifications"][item["key"]] = item
            with self.subTest(status=status, budget=budget), self.assertRaises(StateError):
                validate_state(state)
        state = initial_state("thsottiaux")
        item = notification(status="SENT")
        state["pending_notifications"][item["key"]] = item
        with self.assertRaises(StateError):
            validate_state(state)
        add_notification(state, item)
        validate_state(state)
        state["notified_ids"][item["key"]]["message_id"] = "mismatch"
        with self.assertRaises(StateError):
            validate_state(state)

    def test_inbox_and_successful_classifications_are_distinct(self):
        state = initial_state("thsottiaux")
        tweet = raw_tweet()
        state["inbox"]["11"] = {"tweet": tweet, "attempts": 0, "next_retry_at": None, "last_error": None}
        validate_state(state)
        state["processed_ids"]["11"] = {
            "tweet": tweet, "classification": {"label": "IRRELEVANT", "confidence": 0.9, "reset_time": None, "reason": "Other topic"},
            "processed_at": NOW, "prompt_version": "v1", "evidence_hashes": [], "history": [],
        }
        with self.assertRaises(StateError):
            validate_state(state)
        state["inbox"].clear()
        validate_state(state)
        for bad_confidence in (True, float("nan"), float("inf"), -0.1, 1.1, 10 ** 1000):
            state["processed_ids"]["11"]["classification"]["confidence"] = bad_confidence
            with self.subTest(confidence=str(bad_confidence)[:30]), self.assertRaises(StateError):
                validate_state(state)

    def test_health_notifications_and_failure_records(self):
        state = initial_state("thsottiaux")
        item = notification()
        item.update(key="health:incident-1", tweet_id=None, label="HEALTH")
        add_notification(state, item)
        state["failed_items"]["fetch"] = {"stage": "fetch", "error_code": "FETCH_FAILED", "retryable": True, "next_retry_at": None}
        validate_state(state)
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_only_new_evidenced_possible_upgrade_may_overlap_processed(self):
        state = initial_state("thsottiaux")
        tweet = raw_tweet(text="Maybe another refill")
        state["processed_ids"]["11"] = {
            "tweet": tweet,
            "classification": {"label": "POSSIBLE_RESET", "confidence": 0.7, "reset_time": None, "reason": "Unconfirmed"},
            "processed_at": NOW, "prompt_version": "v1", "evidence_hashes": [], "history": [],
        }
        state["inbox"]["11"] = {
            "tweet": tweet, "attempts": 0, "next_retry_at": None, "last_error": None,
            "upgrade": {"hash": "a" * 64, "evidence": "Confirmed refill announcement", "basis": "New official confirmation"},
        }
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        for field in ("hash", "evidence", "basis"):
            bad = deepcopy(state)
            bad["inbox"]["11"]["upgrade"][field] = ""
            with self.subTest(field=field), self.assertRaises(StateError):
                validate_state(bad)
        state["processed_ids"]["11"]["evidence_hashes"] = ["a" * 64]
        with self.assertRaises(StateError):
            validate_state(state)
        state["processed_ids"]["11"]["evidence_hashes"] = []
        state["processed_ids"]["11"]["classification"]["label"] = "PRE_RESET"
        with self.assertRaises(StateError):
            validate_state(state)

    def test_persisted_raw_tweet_requires_complete_trusted_identity(self):
        for collection in ("inbox", "processed_ids"):
            state = initial_state("thsottiaux")
            if collection == "inbox":
                record = {"tweet": raw_tweet(), "attempts": 0, "next_retry_at": None, "last_error": None}
            else:
                record = {
                    "tweet": raw_tweet(), "classification": {"label": "IRRELEVANT", "confidence": 0.9, "reset_time": None, "reason": "Other topic"},
                    "processed_at": NOW, "prompt_version": "v1", "evidence_hashes": [], "history": [],
                }
            state[collection]["11"] = record
            validate_state(state)
            for field in ("id", "text", "author", "url", "created_at"):
                damaged = deepcopy(state)
                del damaged[collection]["11"]["tweet"][field]
                with self.subTest(collection=collection, missing=field), self.assertRaises(StateError):
                    validate_state(damaged)
            for field, value in (
                ("author", "someone_else"), ("created_at", "2026-10-10T02:00:00"),
                ("url", "https://evil.example/thsottiaux/status/11"),
                ("url", "https://x.com/thsottiaux/status/12"),
                ("url", "https://x.com/other/status/11"),
            ):
                damaged = deepcopy(state)
                damaged[collection]["11"]["tweet"][field] = value
                with self.subTest(collection=collection, field=field, value=value), self.assertRaises(StateError):
                    validate_state(damaged)

    def test_all_persisted_timestamp_locations_reject_malformed_offsets(self):
        state = initial_state("thsottiaux")
        state["inbox"]["11"] = {"tweet": raw_tweet(), "attempts": 0, "next_retry_at": NOW, "last_error": None}
        state["processed_ids"]["12"] = {
            "tweet": raw_tweet("12"),
            "classification": {"label": "IRRELEVANT", "confidence": 0.9, "reset_time": NOW, "reason": "Other topic"},
            "processed_at": NOW, "prompt_version": "v1", "evidence_hashes": [], "history": [],
        }
        add_notification(state, notification("12", status="UNKNOWN"))
        add_notification(state, notification("13", status="SENT"))
        state["failed_items"]["fetch"] = {"stage": "fetch", "error_code": "FETCH_FAILED", "retryable": True, "next_retry_at": NOW}
        paths = [
            ("last_fetch_at",), ("last_success_at",), ("health", "recovered_at"),
            ("inbox", "11", "next_retry_at"), ("inbox", "11", "tweet", "created_at"),
            ("processed_ids", "12", "processed_at"), ("processed_ids", "12", "classification", "reset_time"),
            ("pending_notifications", "12:PRE_RESET", "created_at"),
            ("pending_notifications", "12:PRE_RESET", "next_retry_at"),
            ("pending_notifications", "12:PRE_RESET", "uncertain_since"),
            ("pending_notifications", "13:PRE_RESET", "sent_at"),
            ("notified_ids", "13:PRE_RESET", "sent_at"),
            ("failed_items", "fetch", "next_retry_at"),
        ]
        for malformed in ("2026-10-10T02:00:00+00:60", "2026-10-10T02:00:00+00:99", "2026-10-10T02:00:00+24:00", "2026-10-10T02:60:00+00:00"):
            for path in paths:
                damaged = deepcopy(state)
                destination = damaged
                for part in path[:-1]:
                    destination = destination[part]
                destination[path[-1]] = malformed
                with self.subTest(path=path, malformed=malformed), self.assertRaises(StateError):
                    validate_state(damaged)

    def test_unknown_semantics_reject_corrupt_retry_permissions(self):
        mutations = (
            {"had_unknown": False}, {"uncertain_since": None}, {"next_retry_at": None},
            {"next_retry_at": "2026-10-09T02:00:00+00:00"},
            {"unknown_retry_used": 1, "needs_attention": False},
            {"status": "FAILED", "unknown_retry_used": 1, "needs_attention": False},
            {"status": "PENDING", "unknown_retry_used": 1, "needs_attention": False},
            {"status": "SENDING", "unknown_retry_used": 1, "had_unknown": False},
        )
        for mutation in mutations:
            state = initial_state("thsottiaux")
            item = notification(status="UNKNOWN")
            item.update(mutation)
            add_notification(state, item)
            with self.subTest(mutation=mutation), self.assertRaises(StateError):
                self.store.save(state)
            self.assertEqual(state["revision"], 0)

    def test_unknown_budget_atomic_transition_round_trips(self):
        state = initial_state("thsottiaux")
        item = notification(status="UNKNOWN")
        add_notification(state, item)
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        # Consume the one uncertainty retry while conservatively paused.
        item.update(unknown_retry_used=1, needs_attention=True)
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        # The next atomic snapshot begins the actual attempt.
        item.update(status="SENDING", needs_attention=False)
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        item.update(status="SENT", sent_at=TOMORROW)
        state["notified_ids"][item["key"]] = {"sent_at": TOMORROW, "message_id": item["message_id"]}
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_failed_after_unknown_budget_must_remain_paused(self):
        state = initial_state("thsottiaux")
        item = notification(status="FAILED")
        item.update(unknown_retry_used=1, had_unknown=True, uncertain_since=NOW, needs_attention=True)
        add_notification(state, item)
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
class LockTests(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "monitor.lock"

    def run_child(self, script):
        return subprocess.run(
            [sys.executable, "-B", "-c", LOCK_CHILD_BOOTSTRAP + script, str(self.path)],
            cwd=Path(__file__).resolve().parents[1], env=lock_child_environment(),
            capture_output=True, text=True, timeout=10,
        )

    def child_attempt(self):
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "from monitor.locking import ProcessLock, LockBusy\n"
            "try:\n"
            "    with ProcessLock(Path(sys.argv[1])): pass\n"
            "except LockBusy:\n"
            "    sys.exit(23)\n"
        )
        return self.run_child(script)

    def child_exit_with_lock(self):
        script = (
            "from pathlib import Path\n"
            "import os, sys\n"
            "from monitor.locking import ProcessLock\n"
            "lock = ProcessLock(Path(sys.argv[1]))\n"
            "lock.acquire()\n"
            "os._exit(0)\n"
        )
        return self.run_child(script)

    def test_other_process_cannot_enter_then_can_after_release(self):
        with ProcessLock(self.path):
            result = self.child_attempt()
            self.assertEqual(result.returncode, 23, result.stderr)
        result = self.child_attempt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.path.exists())

    def test_exception_releases_lock_and_retains_lock_file(self):
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            with ProcessLock(self.path):
                raise RuntimeError("simulated")
        with ProcessLock(self.path):
            self.assertTrue(self.path.exists())

    def test_process_exit_releases_os_lock(self):
        result = self.child_exit_with_lock()
        self.assertEqual(result.returncode, 0)
        with ProcessLock(self.path):
            pass

    def test_both_lock_child_paths_strip_parent_dummy_credentials(self):
        names = (
            "TWITTER_API_KEY", "APIFY_TOKEN", "DEEPSEEK_API_KEY", "SMTP_PASSWORD",
            "SMTP_USER", "EMAIL_TO", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID", "AZURE_CLIENT_SECRET", "CUSTOM_API_KEY",
            "CUSTOM_TOKEN", "CUSTOM_PASSWORD", "CUSTOM_SECRET",
        )
        sentinels = {name: "RESET_MONITOR_LOCK_SENTINEL_" + uuid.uuid4().hex for name in names}
        parent_values = {
            **sentinels,
            "MONITOR_TEST_VERIFY_LOCK_CREDENTIAL_ISOLATION": "1",
            "MONITOR_TEST_LOCK_PUBLIC_CONTROL": "permitted-offline-control",
        }
        with patch.dict(os.environ, parent_values, clear=False):
            self.assertTrue(all(os.environ[name] == value for name, value in sentinels.items()))
            for launch in (self.child_attempt, self.child_exit_with_lock):
                with self.subTest(path=launch.__name__):
                    completed = launch()
                    output = completed.stdout + completed.stderr
                    self.assertEqual(completed.returncode, 0, "Child credential isolation failed")
                    self.assertIn("LOCK_CHILD_CREDENTIAL_ISOLATION_VERIFIED", completed.stderr)
                    self.assertTrue(all(value not in output for value in sentinels.values()), "Dummy credential appeared in subprocess output")

    def test_competing_context_does_not_touch_state(self):
        state_path = self.path.parent / "state.json"
        state_path.write_text("sentinel", encoding="utf-8")
        with ProcessLock(self.path):
            with self.assertRaises(LockBusy):
                with ProcessLock(self.path):
                    state_path.write_text("changed", encoding="utf-8")
        self.assertEqual(state_path.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
