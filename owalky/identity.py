"""Signalling a process the plugin started, by identity rather than by number.

A pid is reused and the pid file is writable by any same-user process, so a signal
is sent only after re-reading ``/proc/<pid>/stat`` and matching the start time and
uid recorded while the process was known to be ours. ``pidfd_open`` holds the
process between that check and the signal.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time

from .storage import (
    MAX_SMALL_FILE_BYTES,
    PID_FILE,
    SOCKET_FILE,
    read_file,
    remove_file,
    write_file,
)

Identity = tuple  # (pid, start time, uid)

#: Fields in /proc/<pid>/stat after the command name; the start time is the 20th.
STAT_FIELDS_AFTER_NAME = 20


def process_identity(pid: int) -> Identity | None:
    """Return ``(pid, start time, uid)`` for a live process, or ``None``.

    The command name in ``/proc/<pid>/stat`` is parenthesised and may itself
    contain a closing parenthesis, so the fields after the last one are what the
    offsets below refer to.
    """
    try:
        fd = os.open(f"/proc/{pid}/stat", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    fields = raw.rsplit(b")", 1)[-1].split()
    if len(fields) < STAT_FIELDS_AFTER_NAME:
        return None
    try:
        start_time = int(fields[19])
        uid = os.stat(f"/proc/{pid}").st_uid
    except (OSError, ValueError):
        return None
    return (pid, start_time, uid)


def read_record(dir_fd: int) -> Identity | None:
    """Read the daemon's recorded identity from its pid file."""
    raw = read_file(dir_fd, PID_FILE, MAX_SMALL_FILE_BYTES)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        return (
            _whole_number(record["pid"]),
            _whole_number(record["startTime"]),
            _whole_number(record["uid"]),
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def daemon_alive(dir_fd: int) -> bool:
    """Whether the recorded daemon is still the process that recorded itself."""
    record = read_record(dir_fd)
    return record is not None and process_identity(record[0]) == record


def _whole_number(value: object) -> int:
    """Accept a JSON integer and nothing else.

    ``int(1.5)`` truncates silently and ``int(1e999)`` raises, so the type is
    checked instead of coerced.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected a whole number")
    return value


def write_record(dir_fd: int) -> Identity:
    """Record this process's identity so it can be signalled by identity later."""
    identity = process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("cannot read own process identity")
    payload = json.dumps({"pid": identity[0], "startTime": identity[1], "uid": identity[2]}).encode("utf-8")
    write_file(dir_fd, PID_FILE, payload, MAX_SMALL_FILE_BYTES)
    return identity


def stop_daemon(dir_fd: int, grace: float = 5.0) -> bool:
    """Terminate the daemon if it is still the one that recorded its identity.

    Returns whether a signal was delivered. A stale record is deleted, not
    signalled.
    """
    record = read_record(dir_fd)
    if record is None:
        remove_file(dir_fd, SOCKET_FILE)
        return False
    if process_identity(record[0]) != record:
        remove_file(dir_fd, PID_FILE)
        remove_file(dir_fd, SOCKET_FILE)
        return False
    if not signal_owned(record, signal.SIGTERM):
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if process_identity(record[0]) != record:
            return True
        time.sleep(0.1)
    signal_owned(record, signal.SIGKILL)
    remove_file(dir_fd, PID_FILE)
    remove_file(dir_fd, SOCKET_FILE)
    return True


def signal_owned(record: Identity, number: int) -> bool:
    """Send ``number`` to the recorded process, but only while it is still ours."""
    pid = record[0]
    try:
        pid_fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if process_identity(pid) != record:
            return False
        signal.pidfd_send_signal(pid_fd, number)
        return True
    except (OSError, ProcessLookupError):
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(pid_fd)
