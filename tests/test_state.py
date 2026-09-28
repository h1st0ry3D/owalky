"""The state and configuration documents, and MAC resolution.

The tests plant documents with wrong types, non-finite numbers, unknown keys and
invalid addresses.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from owalky import state, storage


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch.object(storage, "home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)
        for variable in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "OWALKY_MAC"):
            os.environ.pop(variable, None)
            self.addCleanup(os.environ.pop, variable, None)


class CleanStateTests(unittest.TestCase):
    def test_known_fields_survive_with_their_types(self):
        cleaned = state.clean_state(
            {"connected": True, "running": False, "paused": True, "speed": 3.5, "distance": 1200}
        )
        self.assertEqual(
            cleaned, {"connected": True, "running": False, "paused": True, "speed": 3.5, "distance": 1200}
        )

    def test_absent_fields_stay_absent(self):
        self.assertEqual(state.clean_state({"connected": True}), {"connected": True})

    def test_unknown_keys_are_dropped(self):
        self.assertEqual(state.clean_state({"connected": True, "belt": "on fire"}), {"connected": True})

    def test_wrong_types_are_dropped(self):
        cleaned = state.clean_state(
            {
                "connected": "yes",
                "running": 1,
                "paused": None,
                "speed": "fast",
                "distance": 12.5,
                "error": 5,
            }
        )
        self.assertEqual(cleaned, {})

    def test_non_finite_and_out_of_range_numbers_are_dropped(self):
        for value in (float("nan"), math.inf, -1.0, 1e9):
            with self.subTest(value=value):
                self.assertNotIn("speed", state.clean_state({"speed": value}))
        for value in (-1, 10**9):
            with self.subTest(value=value):
                self.assertNotIn("distance", state.clean_state({"distance": value}))

    def test_booleans_are_not_accepted_as_numbers(self):
        self.assertNotIn("distance", state.clean_state({"distance": True}))
        self.assertNotIn("speed", state.clean_state({"speed": False}))

    def test_error_strings_are_capped(self):
        cleaned = state.clean_state({"error": "x" * 1000})
        self.assertEqual(len(cleaned["error"]), state.MAX_ERROR_CHARS)

    def test_a_document_that_is_not_an_object_is_empty(self):
        for document in ([], "text", 7, None):
            with self.subTest(document=document):
                self.assertEqual(state.clean_state(document), {})


class StateFileTests(TempHomeTest):
    def test_updates_merge_and_publish(self):
        with storage.state_dir() as dir_fd:
            state.save_state(dir_fd, {"connected": True})
            state.save_state(dir_fd, {"running": True, "speed": 2.0})
            document = state.load_state(dir_fd)
        self.assertTrue(document["connected"])
        self.assertTrue(document["running"])
        self.assertEqual(document["speed"], 2.0)
        self.assertIn("updated", document)  # kept through the cleaner

    def test_a_malformed_state_file_is_treated_as_empty(self):
        with storage.state_dir() as dir_fd:
            storage.write_file(dir_fd, storage.STATE_FILE, b"{not json", 65536)
            self.assertEqual(state.load_state(dir_fd), {})
            state.save_state(dir_fd, {"connected": True})
            self.assertEqual(state.load_state(dir_fd)["connected"], True)

    def test_the_published_file_is_private(self):
        with storage.state_dir() as dir_fd:
            state.save_state(dir_fd, {"connected": True})
            mode = os.fstat(os.open(storage.STATE_FILE, os.O_RDONLY, dir_fd=dir_fd)).st_mode
        self.assertEqual(mode & 0o777, 0o600)


class ConfigTests(TempHomeTest):
    def test_a_valid_configuration_round_trips(self):
        state.save_config({"mac": "aa:bb:cc:dd:ee:ff", "lastSpeed": 4.2})
        self.assertEqual(state.load_config(), {"mac": "AA:BB:CC:DD:EE:FF", "lastSpeed": 4.2})

    def test_an_invalid_mac_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            state.save_config({"mac": "not-a-mac"})
        self.assertEqual(state.load_config(), {})

    def test_an_invalid_speed_is_ignored_rather_than_fatal(self):
        state.save_config({"lastSpeed": "quick"})
        self.assertNotIn("lastSpeed", state.load_config())

    def test_an_empty_mac_clears_the_stored_one(self):
        state.save_config({"mac": "AA:BB:CC:DD:EE:FF"})
        state.save_config({"mac": ""})
        self.assertNotIn("mac", state.load_config())

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(storage.StorageError):
            state.save_config({"shell": "rm -rf /"})

    def test_a_malformed_configuration_file_is_treated_as_empty(self):
        with storage.config_dir() as dir_fd:
            storage.write_file(dir_fd, storage.CONFIG_FILE, b"[]", 65536)
        self.assertEqual(state.load_config(), {})

    def test_the_stored_file_holds_only_validated_fields(self):
        with storage.config_dir() as dir_fd:
            storage.write_file(
                dir_fd,
                storage.CONFIG_FILE,
                json.dumps({"mac": "AA:BB:CC:DD:EE:FF", "lastSpeed": 99, "evil": 1}).encode(),
                65536,
            )
        self.assertEqual(state.load_config(), {"mac": "AA:BB:CC:DD:EE:FF", "lastSpeed": 12.0})


class StdinTests(unittest.TestCase):
    """``config-set`` must not wait for the writer to close stdin.

    The panel writes the payload and leaves the pipe open, so a read that waits
    for EOF would hang the helper and the panel's process handle with it.
    """

    def run_config_set(self, chunks: list[bytes]) -> bytes:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(
            [sys.executable, "-I", os.path.join(root, "owalky_helper.py"), "config-set"],
            stdin=read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(read_fd)
        try:
            for chunk in chunks:
                os.write(write_fd, chunk)
                time.sleep(0.05)
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            self.fail("config-set hung waiting for EOF on stdin")
        finally:
            os.close(write_fd)
        self.assertEqual(process.returncode, 0, stderr.decode())
        return stdout

    def test_it_returns_while_the_writer_holds_stdin_open(self):
        # the pipe stays open for the rest of the test: read_config_set never
        # closes the write end before the helper has answered
        self.assertIn(b"saved", self.run_config_set([b'{"lastSpeed": 2.0}\n']))

    def test_a_document_split_across_writes_is_read(self):
        self.assertIn(b"saved", self.run_config_set([b'{"lastSpeed":', b" 2.0}\n"]))

    def test_an_oversized_payload_is_refused(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-I", os.path.join(root, "owalky_helper.py"), "config-set"],
            input=b'{"lastSpeed": 1.0, "pad": "' + b"x" * 9000 + b'"}',
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"too large", result.stderr)


class ResolveMacTests(TempHomeTest):
    def test_the_flag_wins(self):
        os.environ["OWALKY_MAC"] = "11:11:11:11:11:11"
        state.save_config({"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(state.resolve_mac("22:22:22:22:22:22"), "22:22:22:22:22:22")

    def test_then_the_environment(self):
        os.environ["OWALKY_MAC"] = "11:11:11:11:11:11"
        state.save_config({"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(state.resolve_mac(None), "11:11:11:11:11:11")

    def test_then_the_configuration(self):
        state.save_config({"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(state.resolve_mac(None), "AA:BB:CC:DD:EE:FF")

    def test_and_nothing_means_a_clear_error(self):
        with self.assertRaises(ValueError) as caught:
            state.resolve_mac(None)
        self.assertIn("no MAC address", str(caught.exception))

    def test_an_invalid_environment_value_is_refused(self):
        os.environ["OWALKY_MAC"] = "totally; rm -rf /"
        with self.assertRaises(ValueError):
            state.resolve_mac(None)


class StatusDocumentTests(TempHomeTest):
    def test_the_document_is_small_and_bounded(self):
        document = state.status_document()
        self.assertLess(len(json.dumps(document)), 4096)
        self.assertEqual(
            sorted(document),
            ["connected", "daemon", "distance", "error", "lastSpeed", "log", "mac", "paused", "running", "speed"],
        )

    def test_state_without_a_daemon_is_not_reported_as_connected(self):
        with storage.state_dir() as dir_fd:
            state.save_state(dir_fd, {"connected": True, "running": True, "paused": True})
        document = state.status_document()
        self.assertFalse(document["daemon"])
        self.assertFalse(document["connected"])
        self.assertFalse(document["running"])
        self.assertFalse(document["paused"])


if __name__ == "__main__":
    unittest.main()
