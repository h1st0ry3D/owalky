"""The file layer: directory chains, bounded reads, atomic writes, the log.

Each test plants something the plugin has to refuse: a symlink, a fifo, a hard
link, an oversized or world-readable file.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from owalky import storage
from owalky.state import load_state


class TempHomeTest(unittest.TestCase):
    """Runs each test against a throwaway home directory."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.home = self._home.name
        patch = mock.patch.object(storage, "home_dir", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def path(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)

    def open_state(self) -> int:
        return storage.open_dir(storage.state_components())


class LocationTests(TempHomeTest):
    def test_defaults_are_the_documented_xdg_paths(self):
        for variable in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            os.environ.pop(variable, None)
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.assertEqual(storage.state_components(), [".local", "state", "owalky"])
        self.assertEqual(storage.config_components(), [".config", "owalky"])

    def test_xdg_variables_are_honoured_inside_the_home_directory(self):
        os.environ["XDG_STATE_HOME"] = self.path("elsewhere")
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.assertEqual(storage.state_components(), ["elsewhere", "owalky"])

    def test_the_plugin_directory_is_always_the_last_component(self):
        # the XDG variable names the parent, never the plugin's own leaf
        os.environ["XDG_STATE_HOME"] = self.home
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.assertEqual(storage.state_components()[-1], "owalky")
        self.assertEqual(storage.config_components()[-1], "owalky")

    def test_xdg_variables_outside_the_home_directory_are_ignored(self):
        os.environ["XDG_STATE_HOME"] = "/tmp/elsewhere"
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.assertEqual(storage.state_components(), [".local", "state", "owalky"])

    def test_traversal_in_a_component_is_refused(self):
        for parts in ([], [".."], ["."], ["a/b"], ["a", ".."], ["a\x00b"]):
            with self.subTest(parts=parts), self.assertRaises(storage.StorageError):
                storage.open_dir(parts)


class DirectoryTests(TempHomeTest):
    def test_the_chain_is_created_private(self):
        dir_fd = self.open_state()
        self.addCleanup(os.close, dir_fd)
        for parts in ((".local",), (".local", "state"), (".local", "state", "owalky")):
            with self.subTest(parts=parts):
                self.assertEqual(stat.S_IMODE(os.stat(self.path(*parts)).st_mode), 0o700)

    def test_a_widened_leaf_is_tightened_again(self):
        os.makedirs(self.path(".local", "state", "owalky"))
        os.chmod(self.path(".local", "state", "owalky"), 0o777)
        dir_fd = self.open_state()
        self.addCleanup(os.close, dir_fd)
        self.assertEqual(stat.S_IMODE(os.fstat(dir_fd).st_mode), 0o700)

    def test_a_symlinked_leaf_is_refused_rather_than_followed(self):
        os.makedirs(self.path(".local", "state"))
        target = self.path("elsewhere")
        os.makedirs(target)
        os.symlink(target, self.path(".local", "state", "owalky"))
        with self.assertRaises(OSError):
            self.open_state()

    def test_sweep_removes_a_planted_symlink(self):
        dir_fd = self.open_state()
        self.addCleanup(os.close, dir_fd)
        victim = self.path("victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("must survive")
        os.symlink(victim, self.path(".local", "state", "owalky", "state.json"))
        self.open_state()
        self.assertNotIn("state.json", os.listdir(self.path(".local", "state", "owalky")))
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "must survive")

    def test_sweep_tightens_a_loosened_file(self):
        dir_fd = self.open_state()
        self.addCleanup(os.close, dir_fd)
        target = self.path(".local", "state", "owalky", "state.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{}")
        os.chmod(target, 0o644)
        self.open_state()
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_sweep_refuses_a_directory_it_did_not_create(self):
        dir_fd = self.open_state()
        self.addCleanup(os.close, dir_fd)
        os.mkdir(self.path(".local", "state", "owalky", "nested"))
        with self.assertRaises(storage.StorageError):
            self.open_state()


class ReadTests(TempHomeTest):
    def setUp(self):
        super().setUp()
        self.dir_fd = self.open_state()
        self.addCleanup(os.close, self.dir_fd)
        self.leaf = self.path(".local", "state", "owalky")

    def create(self, name: str, data: bytes, mode: int = 0o600) -> str:
        target = os.path.join(self.leaf, name)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return target

    def test_a_missing_file_is_absent_not_an_error(self):
        self.assertIsNone(storage.read_file(self.dir_fd, "state.json", 4096))

    def test_a_file_at_the_limit_is_read(self):
        self.create("state.json", b"x" * 4096)
        self.assertEqual(len(storage.read_file(self.dir_fd, "state.json", 4096)), 4096)

    def test_a_file_over_the_limit_is_refused(self):
        self.create("state.json", b"x" * 4097)
        with self.assertRaises(storage.StorageError):
            storage.read_file(self.dir_fd, "state.json", 4096)

    def test_a_symlink_is_refused(self):
        victim = self.path("victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("{}")
        os.symlink(victim, os.path.join(self.leaf, "state.json"))
        with self.assertRaises(storage.StorageError):
            storage.read_file(self.dir_fd, "state.json", 4096)

    def test_a_fifo_is_refused_without_hanging(self):
        os.mkfifo(os.path.join(self.leaf, "state.json"))
        with self.assertRaises(storage.StorageError):
            storage.read_file(self.dir_fd, "state.json", 4096)

    def test_a_readable_by_others_file_is_refused(self):
        self.create("state.json", b"{}", mode=0o644)
        with self.assertRaises(storage.StorageError):
            storage.read_file(self.dir_fd, "state.json", 4096)

    def test_a_hard_link_is_refused(self):
        self.create("state.json", b"{}")
        os.link(os.path.join(self.leaf, "state.json"), os.path.join(self.leaf, "copy.json"))
        with self.assertRaises(storage.StorageError):
            storage.read_file(self.dir_fd, "state.json", 4096)

    def test_tail_is_bounded_and_capped_at_the_line_limit(self):
        self.create("owalky.log", ("\n".join(f"line {index}" for index in range(500))).encode())
        tail = storage.read_file_tail(self.dir_fd, "owalky.log", 4096, 10)
        self.assertEqual(len(tail.splitlines()), 10)
        self.assertIn("line 499", tail)

    def test_tail_of_a_missing_file_is_empty(self):
        self.assertEqual(storage.read_file_tail(self.dir_fd, "owalky.log", 4096, 10), "")


class WriteTests(TempHomeTest):
    def setUp(self):
        super().setUp()
        self.dir_fd = self.open_state()
        self.addCleanup(os.close, self.dir_fd)
        self.leaf = self.path(".local", "state", "owalky")

    def test_a_write_publishes_a_private_file(self):
        storage.write_file(self.dir_fd, "state.json", b'{"connected":true}', 4096)
        target = os.path.join(self.leaf, "state.json")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b'{"connected":true}')

    def test_a_write_replaces_a_planted_symlink_without_following_it(self):
        victim = self.path("victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("must survive")
        os.symlink(victim, os.path.join(self.leaf, "state.json"))
        storage.write_file(self.dir_fd, "state.json", b"{}", 4096)
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "must survive")
        self.assertFalse(os.path.islink(os.path.join(self.leaf, "state.json")))

    def test_no_temporary_is_left_behind(self):
        storage.write_file(self.dir_fd, "state.json", b"{}", 4096)
        self.assertEqual(os.listdir(self.leaf), ["state.json"])

    def test_an_oversized_payload_is_refused_before_any_write(self):
        with self.assertRaises(storage.StorageError):
            storage.write_file(self.dir_fd, "state.json", b"x" * 4097, 4096)
        self.assertEqual(os.listdir(self.leaf), [])


class LogTests(TempHomeTest):
    def setUp(self):
        super().setUp()
        self.dir_fd = self.open_state()
        self.addCleanup(os.close, self.dir_fd)
        self.target = self.path(".local", "state", "owalky", "owalky.log")

    def test_lines_are_appended_private(self):
        storage.log_append(self.dir_fd, "first")
        storage.log_append(self.dir_fd, "second")
        with open(self.target, "rb") as handle:
            lines = handle.read().decode().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("first", lines[0])
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)

    def test_an_oversized_log_is_rotated_not_appended_forever(self):
        with open(self.target, "wb") as handle:
            handle.write(b"x" * (storage.MAX_LOG_BYTES + 1024))
            handle.write(b"tail marker")
        storage.log_append(self.dir_fd, "after rotation")
        self.assertLessEqual(os.stat(self.target).st_size, storage.MAX_LOG_KEEP_BYTES + 4096)
        with open(self.target, "rb") as handle:
            self.assertIn(b"after rotation", handle.read())

    def test_logging_never_raises(self):
        spare = self.open_state()
        os.close(spare)
        storage.log_append(spare, "closed descriptor")  # must not raise
        storage.log_append(-1, "invalid descriptor")  # must not raise either


class StateDocumentTests(TempHomeTest):
    def test_state_round_trips(self):
        document = json.dumps({"connected": True, "running": False}).encode()
        with storage.state_dir() as dir_fd:
            storage.write_file(dir_fd, storage.STATE_FILE, document, 4096)
            self.assertEqual(load_state(dir_fd), {"connected": True, "running": False})


if __name__ == "__main__":
    unittest.main()
