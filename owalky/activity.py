"""The activity tracker: one session per workout, in metres, and the day chain.

A session is the distance the pad reports while the belt is in use, from Start to
Stop (or Disconnect, or a dropped link). It is stored as whole metres, so the file
never carries a float that would read back differently. The panel formats it as
kilometres.

The document is closed: unknown keys are dropped, numbers must be integers in
range, a timestamp must be the UTC shape this module writes, and a malformed
entry is dropped on its own rather than taking the rest of the history with it.
The panel parses the same shape in ``core/Document.js``.
"""

from __future__ import annotations

import json
import re
import time

from .storage import (
    ACTIVITY_FILE,
    MAX_ACTIVITY_BYTES,
    MAX_METERS,
    read_file,
    write_file,
)

HISTORY_MAX = 400
MAX_TRAINING_DAYS = 1_000_000
MIN_SESSION_METERS = 10
MAX_TS_LEN = 32

#: The same shape as core/Progression.js TS_PATTERN. This module writes whole
#: seconds, and a document from the QML side may carry fractional ones.
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z\Z")


def empty_activity() -> dict:
    """A tracker with no sessions yet."""
    return {"lastTrainingDate": "", "trainingDays": 0, "history": []}


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_TS_LEN:
        return ""
    return value if _TIMESTAMP.match(value) else ""


def _bounded_int(value: object, low: int, high: int, fallback: int) -> int:
    # bool is an int subclass, and a JSON true would otherwise pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value if low <= value <= high else fallback


def clean_activity(payload: object) -> dict:
    """Keep the known keys with the expected shapes and drop everything else."""
    if not isinstance(payload, dict):
        raise ValueError("activity must be a JSON object")
    history: list[dict] = []
    raw_history = payload.get("history")
    if isinstance(raw_history, list):
        for entry in raw_history[-HISTORY_MAX:]:
            if not isinstance(entry, dict):
                continue
            stamp = _timestamp(entry.get("time"))
            if not stamp:
                continue
            meters = _bounded_int(entry.get("meters"), 0, MAX_METERS, 0)
            history.append({"time": stamp, "meters": meters})
    return {
        "lastTrainingDate": _timestamp(payload.get("lastTrainingDate")),
        "trainingDays": _bounded_int(payload.get("trainingDays"), 0, MAX_TRAINING_DAYS, 0),
        "history": history,
    }


def dump_activity(document: dict) -> bytes:
    """The exact bytes the activity file holds."""
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def load_activity(dir_fd: int) -> dict:
    """Read the activity document, or an empty one when there is none.

    A malformed file reads as empty rather than raising. The next recorded
    session replaces it, which is the only recovery this plugin has and is better
    than the treadmill controls failing because a history file went bad.
    """
    raw = read_file(dir_fd, ACTIVITY_FILE, MAX_ACTIVITY_BYTES)
    if raw is None:
        return empty_activity()
    try:
        return clean_activity(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        return empty_activity()


def save_activity(dir_fd: int, activity: dict) -> dict:
    """Validate and publish the document atomically."""
    document = clean_activity(activity)
    write_file(dir_fd, ACTIVITY_FILE, dump_activity(document), MAX_ACTIVITY_BYTES)
    return document


def record_session(dir_fd: int, meters: object, now: float | None = None) -> dict:
    """Append one completed session and return the new document.

    Short sessions are not recorded: the caller uses this only for a distance at
    or above :data:`MIN_SESSION_METERS`, but the check lives here too so a direct
    caller cannot write a zero-length workout into the chain.

    Raises:
        ValueError: the distance is not a whole number of metres in range.
        StorageError: the document could not be written.
    """
    if isinstance(meters, bool) or not isinstance(meters, int):
        raise ValueError("session distance must be whole metres")
    if not MIN_SESSION_METERS <= meters <= MAX_METERS:
        raise ValueError(f"session distance must be {MIN_SESSION_METERS}-{MAX_METERS} metres")

    document = load_activity(dir_fd)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    document["history"] = (document["history"] + [{"time": stamp, "meters": meters}])[-HISTORY_MAX:]
    document["lastTrainingDate"] = stamp
    document["trainingDays"] = min(MAX_TRAINING_DAYS, document["trainingDays"] + 1)
    return save_activity(dir_fd, document)


__all__ = [
    "HISTORY_MAX",
    "MAX_TRAINING_DAYS",
    "MIN_SESSION_METERS",
    "clean_activity",
    "dump_activity",
    "empty_activity",
    "load_activity",
    "record_session",
    "save_activity",
]
