"""Backup and restore for ``activity.json``, through a path the user picked.

A backup is the activity file exactly as it is, byte for byte, so it is the same
document the plugin would have written and the same one a restore reads back. The
source is the file on disk rather than the panel's copy.

A restore copies the activity it is about to replace into
``~/.local/state/owalky/backups/`` first, so a restore is itself reversible, and
only the newest :data:`SNAPSHOT_KEEP` copies are kept. The incoming document goes
through the same closed schema as a recorded session, so a file that is not a
backup is refused instead of half-applied, and an empty document is not allowed
to erase a real history.

The chosen path comes from the user's own file dialog, which is why it is walked
by :func:`owalky.storage.chosen_path` rather than trusted as a pathname.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time

from .activity import clean_activity, dump_activity
from .storage import (
    ACTIVITY_FILE,
    MAX_ACTIVITY_BYTES,
    chosen_path,
    read_chosen_file,
    read_file,
    snapshot_dir,
    state_dir,
    write_file,
)

#: Safety copies kept in ``backups/``.
SNAPSHOT_KEEP = 10

#: The name this module gives a copy. The name is the only marker there is, so
#: it is the whole rule for what may be pruned later.
_SNAPSHOT = re.compile(r"\Apre-restore_\d{8}_\d{6}-[0-9a-f]{8}\.json\Z")

#: The keys that mark a document as an activity file. A backup always carries
#: all three, so a file that carries none of them is refused as not a backup
#: before the schema has a chance to turn it into an empty one.
_ACTIVITY_KEYS = frozenset({"history", "trainingDays", "lastTrainingDate"})


def snapshot_name() -> str:
    """A sortable name with random bytes, so two restores in one second cannot
    land on the same name."""
    return "pre-restore_%s-%s.json" % (
        time.strftime("%Y%m%d_%H%M%S", time.localtime()),
        secrets.token_hex(4),
    )


def prune_snapshots(dir_fd: int, keep: int = SNAPSHOT_KEEP) -> None:
    """Leave the newest ``keep`` copies.

    Only a name this module's own pattern matches and that is a regular file of
    ours is considered, so a restore cannot delete something it did not write.
    """
    entries: list[tuple[float, str]] = []
    for name in os.listdir(dir_fd):
        if not _SNAPSHOT.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
            entries.append((info.st_mtime, name))
    if len(entries) <= keep:
        return
    entries.sort()
    for _, name in entries[: len(entries) - keep]:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass


def _snapshot(previous: bytes) -> None:
    """Keep the activity a restore is about to replace."""
    with snapshot_dir() as dir_fd:
        write_file(dir_fd, snapshot_name(), previous, MAX_ACTIVITY_BYTES)
        prune_snapshots(dir_fd)


def export_activity(path: object) -> dict:
    """Copy the activity file to ``path``, byte for byte.

    Raises:
        StorageError: the path or the file was refused.
        ValueError: there is nothing recorded to back up yet.
    """
    dir_fd, name = chosen_path(path)
    try:
        with state_dir() as state_fd:
            raw = read_file(state_fd, ACTIVITY_FILE, MAX_ACTIVITY_BYTES)
        if raw is None:
            raise ValueError("nothing to back up yet")
        write_file(dir_fd, name, raw, MAX_ACTIVITY_BYTES)
    finally:
        os.close(dir_fd)
    return {"ok": True, "name": name}


def restore_activity(path: object) -> dict:
    """Replace the activity with the document in ``path``.

    Raises:
        StorageError: the path or the file was refused.
        ValueError: the file is absent, is not an activity document, or holds no
            recorded workouts.
    """
    dir_fd, name = chosen_path(path)
    try:
        raw = read_chosen_file(dir_fd, name, MAX_ACTIVITY_BYTES)
    finally:
        os.close(dir_fd)
    if raw is None:
        raise ValueError("that file is not there")

    try:
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("that file is not an Owalky activity backup") from exc
    if not isinstance(payload, dict) or not _ACTIVITY_KEYS.intersection(payload):
        raise ValueError("that file is not an Owalky activity backup")
    document = clean_activity(payload)
    # An empty document is what a first run looks like. Restoring one over a
    # real history would erase it on the strength of a wrong file.
    if not document["history"] and document["trainingDays"] == 0:
        raise ValueError("that file holds no recorded workouts")

    with state_dir() as state_fd:
        previous = read_file(state_fd, ACTIVITY_FILE, MAX_ACTIVITY_BYTES)
        if previous is not None:
            _snapshot(previous)
        write_file(state_fd, ACTIVITY_FILE, dump_activity(document), MAX_ACTIVITY_BYTES)
    return {"ok": True, "sessions": len(document["history"])}


__all__ = [
    "SNAPSHOT_KEEP",
    "export_activity",
    "prune_snapshots",
    "restore_activity",
    "snapshot_name",
]
