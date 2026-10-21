"""The activity document and the session it records.

No Bluetooth and no QML: these cover the schema, the cap, the one write path and
the command the panel calls to read it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from owalky import activity, cli, storage
from owalky.activity import (
    HISTORY_MAX,
    MIN_SESSION_METERS,
    clean_activity,
    empty_activity,
    estimate_calories,
    load_activity,
    record_session,
)


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch("owalky.storage.home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)


class CleanActivityTests(unittest.TestCase):
    def test_an_unknown_document_is_empty(self):
        self.assertEqual(clean_activity({}), empty_activity())

    def test_a_document_that_is_not_an_object_is_refused(self):
        for value in ([], "text", 7, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_activity(value)

    def test_unknown_keys_are_dropped(self):
        cleaned = clean_activity({"lastTrainingDate": "", "trainingDays": 2, "evil": 1})
        self.assertEqual(sorted(cleaned), ["history", "lastTrainingDate", "trainingDays"])

    def test_bad_numbers_and_booleans_are_dropped(self):
        for value in ("3", True, -1, activity.MAX_TRAINING_DAYS + 1, 1.5):
            with self.subTest(value=value):
                self.assertEqual(clean_activity({"trainingDays": value})["trainingDays"], 0)

    def test_bad_timestamps_are_dropped(self):
        for value in ("", "2026-09-09", "2026-09-09T12:00:00", "x" * 40, 7):
            with self.subTest(value=value):
                self.assertEqual(clean_activity({"lastTrainingDate": value})["lastTrainingDate"], "")

    def test_history_entries_need_a_timestamp(self):
        cleaned = clean_activity(
            {
                "history": [
                    {"time": "2026-09-09T12:00:00Z", "meters": 1200},
                    {"time": "not a time", "meters": 900},
                    {"meters": 800},
                    "nope",
                ]
            }
        )
        self.assertEqual(
            cleaned["history"],
            [
                {
                    "time": "2026-09-09T12:00:00Z",
                    "meters": 1200,
                    "seconds": 0,
                    "calories": 0,
                    "estimated": False,
                }
            ],
        )

    def test_history_is_capped_to_the_newest_entries(self):
        history = [
            {"time": f"2026-09-{(index % 28) + 1:02d}T12:00:00Z", "meters": index}
            for index in range(HISTORY_MAX + 5)
        ]
        cleaned = clean_activity({"history": history})
        self.assertEqual(len(cleaned["history"]), HISTORY_MAX)
        self.assertEqual(cleaned["history"][-1]["meters"], HISTORY_MAX + 4)

    def test_an_oversized_distance_falls_back_to_zero_for_that_entry(self):
        cleaned = clean_activity(
            {"history": [{"time": "2026-09-09T12:00:00Z", "meters": storage.MAX_METERS + 1}]}
        )
        self.assertEqual(cleaned["history"][0]["meters"], 0)


class RecordSessionTests(TempHomeTest):
    def test_the_first_session_creates_the_file(self):
        with storage.state_dir() as dir_fd:
            document = record_session(dir_fd, 2400, 600, 42, estimated=True)
            self.assertEqual(document["trainingDays"], 1)
            self.assertEqual(document["history"][0]["meters"], 2400)
            self.assertEqual(document["history"][0]["seconds"], 600)
            self.assertEqual(document["history"][0]["calories"], 42)
            self.assertIs(document["history"][0]["estimated"], True)
            self.assertEqual(load_activity(dir_fd), document)
            mode = os.fstat(
                os.open(storage.ACTIVITY_FILE, os.O_RDONLY, dir_fd=dir_fd)
            ).st_mode
        self.assertEqual(mode & 0o777, 0o600)

    def test_sessions_append_oldest_first_and_bump_the_day_count(self):
        with storage.state_dir() as dir_fd:
            record_session(dir_fd, 1000)
            document = record_session(dir_fd, 2500)
        self.assertEqual([entry["meters"] for entry in document["history"]], [1000, 2500])
        self.assertEqual(document["trainingDays"], 2)
        self.assertEqual(document["lastTrainingDate"], document["history"][-1]["time"])

    def test_the_history_keeps_the_newest_sessions(self):
        with storage.state_dir() as dir_fd:
            for index in range(HISTORY_MAX + 3):
                record_session(dir_fd, 1000 + index)
            document = load_activity(dir_fd)
        self.assertEqual(len(document["history"]), HISTORY_MAX)
        self.assertEqual(document["history"][-1]["meters"], 1000 + HISTORY_MAX + 2)

    def test_a_short_session_is_refused_rather_than_stored(self):
        with storage.state_dir() as dir_fd:
            with self.assertRaises(ValueError):
                record_session(dir_fd, MIN_SESSION_METERS - 1)
            self.assertEqual(load_activity(dir_fd), empty_activity())

    def test_a_distance_that_is_not_whole_metres_is_refused(self):
        with storage.state_dir() as dir_fd:
            for value in (12.5, "300", True, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    record_session(dir_fd, value)

    def test_time_and_calories_must_be_whole_numbers_in_range(self):
        with storage.state_dir() as dir_fd:
            for keyword, value in (
                ("seconds", 12.5),
                ("seconds", "300"),
                ("seconds", True),
                ("seconds", activity.MAX_SESSION_SECONDS + 1),
                ("calories", 1.5),
                ("calories", activity.MAX_CALORIES + 1),
            ):
                with self.subTest(keyword=keyword, value=value), self.assertRaises(ValueError):
                    record_session(dir_fd, 1200, **{keyword: value})

    def test_the_fallback_calorie_estimate_is_monotonic(self):
        self.assertEqual(estimate_calories(0, 100), 0)
        self.assertEqual(estimate_calories(1000, 0), 0)
        self.assertLess(estimate_calories(1000, 600), estimate_calories(2000, 600))
        self.assertGreater(estimate_calories(1000, 600), 0)

    def test_a_malformed_file_reads_as_empty_and_is_replaced(self):
        with storage.state_dir() as dir_fd:
            storage.write_file(dir_fd, storage.ACTIVITY_FILE, b"{not json", 65536)
            self.assertEqual(load_activity(dir_fd), empty_activity())
            record_session(dir_fd, 1200)
            self.assertEqual(load_activity(dir_fd)["trainingDays"], 1)

    def test_a_missing_file_reads_as_empty(self):
        with storage.state_dir() as dir_fd:
            self.assertEqual(load_activity(dir_fd), empty_activity())


class ActivityCommandTests(TempHomeTest):
    def test_the_command_prints_the_empty_document(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main_entry(["activity"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue()), empty_activity())

    def test_the_command_prints_recorded_sessions(self):
        with storage.state_dir() as dir_fd:
            record_session(dir_fd, 3100)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main_entry(["activity"])
        document = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(document["history"][-1]["meters"], 3100)


if __name__ == "__main__":
    unittest.main()
