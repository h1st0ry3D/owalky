"""Walk and run mode: the ceiling each one implies, and where it is enforced."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from owalky import cli, ftms
from owalky.daemon import PadDaemon
from owalky.state import load_config, save_config, status_document
from owalky.storage import CONFIG_FILE, StorageError, config_dir, write_file


@contextmanager
def payload_on_stdin(payload: bytes):
    """Put ``payload`` on file descriptor 0 and leave it open, as the panel does.

    The helper reads fd 0 directly rather than through ``sys.stdin``, and it must
    not wait for a close, so the write end stays open for the whole block.
    """
    read_fd, write_fd = os.pipe()
    original = os.dup(0)
    try:
        os.dup2(read_fd, 0)
        os.close(read_fd)
        if payload:
            os.write(write_fd, payload)
        yield
    finally:
        os.close(write_fd)
        os.dup2(original, 0)
        os.close(original)


class CeilingTests(unittest.TestCase):
    def test_the_two_ceilings(self):
        self.assertEqual(ftms.max_speed_for("walk"), 6.0)
        self.assertEqual(ftms.max_speed_for("run"), 12.0)

    def test_an_unknown_or_missing_mode_falls_back_to_walk(self):
        for value in (None, "", "RUN", "jog", 7, "walkk"):
            with self.subTest(value=value):
                self.assertEqual(ftms.max_speed_for(value), 6.0)

    def test_walk_mode_clamps_to_six(self):
        self.assertEqual(ftms.clamp_speed(9.0, ftms.max_speed_for("walk")), 6.0)
        self.assertEqual(ftms.clamp_speed(4.0, ftms.max_speed_for("walk")), 4.0)

    def test_run_mode_allows_twelve(self):
        self.assertEqual(ftms.clamp_speed(11.0, ftms.max_speed_for("run")), 11.0)
        self.assertEqual(ftms.clamp_speed(20.0, ftms.max_speed_for("run")), 12.0)

    def test_a_ceiling_above_the_pad_maximum_is_not_honoured(self):
        self.assertEqual(ftms.clamp_speed(30.0, 99.0), 12.0)

    def test_the_floor_still_applies(self):
        for mode in ("walk", "run"):
            with self.subTest(mode=mode):
                self.assertEqual(ftms.clamp_speed(0.2, ftms.max_speed_for(mode)), 1.0)

    def test_the_default_is_walk(self):
        self.assertEqual(ftms.DEFAULT_MODE, "walk")
        self.assertEqual(ftms.SPEED_CEILINGS[ftms.DEFAULT_MODE], 6.0)


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch("owalky.storage.home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)


class ConfigModeTests(TempHomeTest):
    def test_the_mode_round_trips(self):
        save_config({"mode": "run"})
        self.assertEqual(load_config()["mode"], "run")
        save_config({"mode": "walk"})
        self.assertEqual(load_config()["mode"], "walk")

    def test_an_unknown_mode_is_refused_and_changes_nothing(self):
        save_config({"mode": "run"})
        with self.assertRaises(StorageError):
            save_config({"mode": "sprint"})
        self.assertEqual(load_config()["mode"], "run")

    def test_an_unknown_mode_in_the_file_is_ignored(self):
        with config_dir() as dir_fd:
            write_file(dir_fd, CONFIG_FILE, json.dumps({"mode": "sprint"}).encode(), 8192)
        self.assertNotIn("mode", load_config())

    def test_the_stored_speed_survives_a_mode_change(self):
        save_config({"lastSpeed": 8.0, "mode": "run"})
        save_config({"mode": "walk"})
        config = load_config()
        self.assertEqual(config["lastSpeed"], 8.0)
        self.assertEqual(config["mode"], "walk")


class StatusTests(TempHomeTest):
    def test_the_document_reports_the_mode_and_its_ceiling(self):
        save_config({"mode": "run"})
        document = status_document()
        self.assertEqual(document["mode"], "run")
        self.assertEqual(document["maxSpeed"], 12.0)
        save_config({"mode": "walk"})
        self.assertEqual(status_document()["maxSpeed"], 6.0)

    def test_with_no_config_at_all_it_is_walk(self):
        document = status_document()
        self.assertEqual(document["mode"], "walk")
        self.assertEqual(document["maxSpeed"], 6.0)

    def test_the_document_is_still_small(self):
        save_config({"mode": "run", "mac": "AA:BB:CC:DD:EE:FF", "lastSpeed": 9.9})
        self.assertLess(len(json.dumps(status_document())), 4096)


class CommandLineTests(TempHomeTest):
    """The command line enforces the ceiling too, not just the panel.

    These run the parser in this process rather than forking the helper: the
    helper resolves the home directory from the password database by design, so a
    subprocess would write to the real one.
    """

    def run_cli(self, argv: list[str], stdin: bytes = b"") -> int:
        buffer = io.StringIO()
        with payload_on_stdin(stdin), redirect_stdout(buffer):
            code = cli.main_entry(argv)
        self.output = buffer.getvalue()
        return code

    def test_speed_is_clamped_to_the_walk_ceiling(self):
        save_config({"mode": "walk"})
        self.run_cli(["speed", "9.0"])
        self.assertIn("clamped to 6.0", self.output)
        self.assertEqual(load_config()["lastSpeed"], 6.0)

    def test_speed_above_six_is_allowed_in_run_mode(self):
        save_config({"mode": "run"})
        self.run_cli(["speed", "9.0"])
        self.assertEqual(load_config()["lastSpeed"], 9.0)
        self.assertNotIn("clamped", self.output)

    def test_a_valid_mode_is_accepted_from_stdin(self):
        code = self.run_cli(["config-set"], stdin=b'{"mode": "run"}\n')
        self.assertEqual(code, 0)
        self.assertEqual(load_config()["mode"], "run")

    def test_an_invalid_mode_from_stdin_is_refused(self):
        code = self.run_cli(["config-set"], stdin=b'{"mode": "sprint"}\n')
        self.assertEqual(code, 1)
        self.assertNotIn("mode", load_config())

    def test_the_daemon_applies_the_same_ceiling(self):
        save_config({"mode": "walk"})
        self.assertEqual(PadDaemon("AA:BB:CC:DD:EE:FF").ceiling(), 6.0)
        save_config({"mode": "run"})
        self.assertEqual(PadDaemon("AA:BB:CC:DD:EE:FF").ceiling(), 12.0)


if __name__ == "__main__":
    unittest.main()
