from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tests.temp_support import temporary_directory
import unittest

from monitor.settings_status import read_background_status


class SettingsStatusTests(unittest.TestCase):
    def test_stale_heartbeat_is_not_reported_as_running(self):
        now = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        with temporary_directory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"state": "RUNNING", "heartbeat_at": (now-timedelta(minutes=5)).isoformat()}))
            self.assertFalse(read_background_status(path=path, now=now)["running"])

    def test_fresh_heartbeat_and_summary_are_safe(self):
        now = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        with temporary_directory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"state": "RUNNING", "heartbeat_at": now.isoformat(),
                "last_slot": {"status": "NO_RELEVANT_ITEMS", "raw": "SYNTHETIC_PRIVATE_VALUE"}}))
            status = read_background_status(path=path, now=now)
            self.assertTrue(status["running"])
            self.assertEqual(status["next_run"], "今天 13:00")
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", str(status))

    def test_missing_and_corrupt_files_do_not_crash_the_ui(self):
        with temporary_directory() as directory:
            path = Path(directory) / "status.json"
            self.assertFalse(read_background_status(path=path)["running"])
            path.write_text("not json")
            self.assertFalse(read_background_status(path=path)["running"])
