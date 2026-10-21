"""Backup and restore of the activity document.

The exported file is the activity file itself; a restore validates a closed
schema, keeps the document it replaces, and refuses anything that is not a
backup. The chosen path is walked from the root, so the tests plant a symlink, a
world-writable directory and a relative path.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from owalky import activity, backup, cli, storage


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.home = self._home.name
        patch = mock.patch.object(storage, "home_dir", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        self.leaf = os.path.join(self.home, ".local", "state", "owalky")
        self.backups = os.path.join(self.leaf, storage.BACKUP_DIR_NAME)
        self.chosen_dir = self.path("chosen")
        os.makedirs(self.chosen_dir)

    def path(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)

    def target(self, name: str) -> str:
        return os.path.join(self.chosen_dir, name)

    def record(self, meters: int = 2400) -> dict:
        with storage.state_dir() as dir_fd:
            return activity.record_session(dir_fd, meters, 600, 42, estimated=True)

    def activity_bytes(self) -> bytes:
        with storage.state_dir() as dir_fd:
            return storage.read_file(dir_fd, storage.ACTIVITY_FILE, storage.MAX_ACTIVITY_BYTES)

    def write_choice(self, name: str, payload: object) -> str:
        target = self.target(name)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))
        return target


class ExportTests(TempHomeTest):
    def test_an_export_is_the_activity_file_byte_for_byte(self):
        self.record()
        target = self.target("walk.json")
        reply = backup.export_activity(target)
        self.assertEqual(reply, {"ok": True, "name": "walk.json"})
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), self.activity_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_an_export_replaces_an_existing_file_and_leaves_no_temporary(self):
        self.record()
        target = self.write_choice("walk.json", {"old": True})
        backup.export_activity(target)
        self.assertEqual(os.listdir(self.chosen_dir), ["walk.json"])

    def test_there_is_nothing_to_export_before_the_first_workout(self):
        target = self.target("walk.json")
        with self.assertRaises(ValueError):
            backup.export_activity(target)
        self.assertFalse(os.path.exists(target))

    def test_a_name_with_a_space_is_a_name_not_a_grammar(self):
        self.record()
        target = self.target("my walk.json")
        self.assertEqual(backup.export_activity(target)["name"], "my walk.json")
        self.assertTrue(os.path.exists(target))

    def test_a_relative_path_is_refused(self):
        self.record()
        for path in ("walk.json", "", "/", None, 7):
            with self.subTest(path=path), self.assertRaises(storage.StorageError):
                backup.export_activity(path)

    def test_traversal_is_refused(self):
        self.record()
        with self.assertRaises(storage.StorageError):
            backup.export_activity(os.path.join(self.chosen_dir, "..", "escape.json"))
        self.assertFalse(os.path.exists(self.path("escape.json")))

    def test_a_world_writable_directory_is_refused(self):
        self.record()
        open_dir = self.path("shared")
        os.mkdir(open_dir)
        os.chmod(open_dir, 0o777)
        with self.assertRaises(storage.StorageError):
            backup.export_activity(os.path.join(open_dir, "walk.json"))
        self.assertEqual(os.listdir(open_dir), [])

    def test_a_sticky_directory_is_allowed(self):
        # /tmp is the one shared directory that is safe; a private copy of it
        # exercises the same rule without writing to the real /tmp.
        self.record()
        shared = self.path("sticky")
        os.mkdir(shared)
        os.chmod(shared, 0o1777)
        backup.export_activity(os.path.join(shared, "walk.json"))
        self.assertTrue(os.path.exists(os.path.join(shared, "walk.json")))

    def test_an_export_replaces_a_symlink_at_the_chosen_file_without_following_it(self):
        self.record()
        victim = self.path("victim.json")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("must survive")
        os.symlink(victim, self.target("walk.json"))
        backup.export_activity(self.target("walk.json"))
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "must survive")
        self.assertFalse(os.path.islink(self.target("walk.json")))

    def test_a_symlinked_directory_on_the_way_is_followed_and_checked(self):
        self.record()
        real = self.path("real")
        os.mkdir(real)
        link = self.path("link")
        os.symlink(real, link)
        backup.export_activity(os.path.join(link, "walk.json"))
        self.assertTrue(os.path.exists(os.path.join(real, "walk.json")))

    def test_a_relative_symlink_is_resolved_against_its_own_folder(self):
        self.record()
        real = self.path("nested", "real")
        os.makedirs(real)
        os.symlink("nested/real", self.path("relative"))
        backup.export_activity(self.path("relative", "walk.json"))
        self.assertTrue(os.path.exists(os.path.join(real, "walk.json")))
        self.assertFalse(os.path.exists(self.path("real", "walk.json")))


class RestoreTests(TempHomeTest):
    def load(self) -> dict:
        with storage.state_dir() as dir_fd:
            return activity.load_activity(dir_fd)

    def test_a_restore_replaces_the_activity_and_reports_the_count(self):
        self.record()
        exported = self.target("walk.json")
        backup.export_activity(exported)

        with storage.state_dir() as dir_fd:
            activity.record_session(dir_fd, 900)
        self.assertEqual(len(self.load()["history"]), 2)

        reply = backup.restore_activity(exported)
        self.assertEqual(reply, {"ok": True, "sessions": 1})
        document = self.load()
        self.assertEqual(len(document["history"]), 1)
        self.assertEqual(document["history"][0]["meters"], 2400)

    def test_the_activity_a_restore_replaces_is_kept_first(self):
        self.record()
        exported = self.target("walk.json")
        backup.export_activity(exported)

        with storage.state_dir() as dir_fd:
            activity.record_session(dir_fd, 900)
        replaced = self.activity_bytes()
        backup.restore_activity(exported)

        kept = os.listdir(self.backups)
        self.assertEqual(len(kept), 1)
        self.assertRegex(kept[0], r"^pre-restore_\d{8}_\d{6}-[0-9a-f]{8}\.json$")
        with open(os.path.join(self.backups, kept[0]), "rb") as handle:
            self.assertEqual(handle.read(), replaced)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.backups, kept[0])).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.backups).st_mode), 0o700)

    def test_only_the_newest_copies_are_kept(self):
        self.record()
        exported = self.target("walk.json")
        backup.export_activity(exported)
        os.makedirs(self.backups, exist_ok=True)
        for index in range(backup.SNAPSHOT_KEEP + 2):
            name = "pre-restore_2026010%d_120000-%08x.json" % (index % 10, index)
            kept = os.path.join(self.backups, name)
            with open(kept, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.utime(kept, (1_600_000_000 + index, 1_600_000_000 + index))

        backup.restore_activity(exported)

        self.assertEqual(len(os.listdir(self.backups)), backup.SNAPSHOT_KEEP)

    def test_a_name_the_plugin_did_not_write_is_left_alone(self):
        self.record()
        exported = self.target("walk.json")
        backup.export_activity(exported)
        os.makedirs(self.backups, exist_ok=True)
        notes = os.path.join(self.backups, "notes.txt")
        with open(notes, "w", encoding="utf-8") as handle:
            handle.write("mine")
        os.chmod(notes, 0o600)

        backup.restore_activity(exported)

        self.assertIn("notes.txt", os.listdir(self.backups))

    def test_a_document_that_is_not_a_backup_is_refused(self):
        self.record()
        before = self.activity_bytes()
        for payload in ({"evil": True}, [1, 2], "not json"):
            with self.subTest(payload=payload):
                target = self.write_choice("wrong.json", payload)
                with self.assertRaises(ValueError):
                    backup.restore_activity(target)
                self.assertEqual(self.activity_bytes(), before)
                self.assertFalse(os.path.exists(self.backups))

    def test_an_empty_activity_does_not_erase_a_real_history(self):
        self.record()
        before = self.activity_bytes()
        target = self.write_choice("empty.json", activity.empty_activity())
        with self.assertRaises(ValueError):
            backup.restore_activity(target)
        self.assertEqual(self.activity_bytes(), before)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(ValueError):
            backup.restore_activity(self.target("absent.json"))

    def test_a_symlinked_choice_is_refused(self):
        self.record()
        before = self.activity_bytes()
        real = self.write_choice("real.json", json.loads(before.decode()))
        link = self.target("link.json")
        os.symlink(real, link)
        with self.assertRaises(storage.StorageError):
            backup.restore_activity(link)
        self.assertEqual(self.activity_bytes(), before)

    def test_an_oversized_file_is_refused(self):
        self.record()
        before = self.activity_bytes()
        target = self.target("huge.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{" + " " * storage.MAX_ACTIVITY_BYTES + "}")
        with self.assertRaises(storage.StorageError):
            backup.restore_activity(target)
        self.assertEqual(self.activity_bytes(), before)

    def test_a_damaged_activity_document_is_reduced_to_the_known_schema(self):
        target = self.write_choice(
            "walk.json",
            {
                "lastTrainingDate": "2026-09-09T12:00:00Z",
                "trainingDays": 3,
                "evil": "dropped",
                "history": [
                    {"time": "2026-09-09T12:00:00Z", "meters": 1200, "seconds": 600, "calories": 30},
                    {"time": "not a time", "meters": 900},
                ],
            },
        )
        reply = backup.restore_activity(target)
        self.assertEqual(reply, {"ok": True, "sessions": 1})
        document = self.load()
        self.assertNotIn("evil", document)
        self.assertEqual(len(document["history"]), 1)


class CommandTests(TempHomeTest):
    def run_backup(self, mode: str, path: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(cli, "read_stdin_json", return_value={"path": path}):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main_entry(["backup", mode])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_export_prints_one_result_line(self):
        self.record()
        target = self.target("walk.json")
        code, stdout, _ = self.run_backup("export", target)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(stdout), {"ok": True, "name": "walk.json"})

    def test_export_without_activity_is_rejected_with_the_reason(self):
        code, _, stderr = self.run_backup("export", self.target("walk.json"))
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("nothing to back up", stderr)

    def test_restore_of_a_non_backup_is_rejected_with_the_reason(self):
        target = self.write_choice("wrong.json", {"evil": True})
        code, _, stderr = self.run_backup("restore", target)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("not an Owalky activity backup", stderr)

    def test_a_request_without_a_path_is_rejected(self):
        with mock.patch.object(cli, "read_stdin_json", return_value={}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                code = cli.main_entry(["backup", "export"])
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("needs a path", stderr.getvalue())


class SnapshotNameTests(unittest.TestCase):
    def test_names_are_sortable_and_unique(self):
        names = {backup.snapshot_name() for _ in range(20)}
        self.assertEqual(len(names), 20)
        for name in names:
            self.assertRegex(name, r"^pre-restore_\d{8}_\d{6}-[0-9a-f]{8}\.json$")


if __name__ == "__main__":
    unittest.main()
