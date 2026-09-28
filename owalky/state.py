"""The two documents the plugin reads and writes, and how a MAC is resolved.

``state.json`` is published by the daemon, ``config.json`` holds what the user
chose. Both live in a directory this plugin owns, and both are still treated as
input, because a same-user process can write there. Unknown keys are dropped,
numbers must be finite and in range, strings are capped, and a MAC must match the
full address grammar.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import time

from .ftms import clamp_speed, is_valid_mac, normalise_mac
from .identity import daemon_alive
from .storage import (
    CONFIG_FILE,
    MAX_CONFIG_BYTES,
    MAX_STATE_BYTES,
    STATE_FILE,
    StorageError,
    config_dir,
    log_path,
    read_file,
    state_dir,
    write_file,
)

MAX_ERROR_CHARS = 120
MAX_SPEED_KMH = 20.0
MAX_DISTANCE_M = 10_000_000


def clean_state(raw: object) -> dict:
    """Return only the state fields the panel reads, with the types it expects.

    An absent field stays absent rather than becoming a default.
    """
    if not isinstance(raw, dict):
        return {}
    state: dict = {}
    for key in ("connected", "running", "paused"):
        if isinstance(raw.get(key), bool):
            state[key] = raw[key]
    speed = raw.get("speed")
    if (
        isinstance(speed, (int, float))
        and not isinstance(speed, bool)
        and math.isfinite(speed)
        and 0.0 <= float(speed) <= MAX_SPEED_KMH
    ):
        state["speed"] = round(float(speed), 1)
    distance = raw.get("distance")
    if isinstance(distance, int) and not isinstance(distance, bool) and 0 <= distance <= MAX_DISTANCE_M:
        state["distance"] = distance
    for key in ("error", "updated"):
        value = raw.get(key)
        if isinstance(value, str):
            state[key] = value[:MAX_ERROR_CHARS]
    return state


def load_state(dir_fd: int) -> dict:
    """Read the published state, or an empty document when there is none."""
    raw = read_file(dir_fd, STATE_FILE, MAX_STATE_BYTES)
    if raw is None:
        return {}
    try:
        return clean_state(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        return {}


def save_state(dir_fd: int, updates: dict) -> dict:
    """Merge ``updates`` into the state document and publish it atomically."""
    state = load_state(dir_fd)
    state.update(clean_state(updates))
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    write_file(dir_fd, STATE_FILE, payload, MAX_STATE_BYTES)
    return state


def load_config() -> dict:
    """Read the configured MAC and last speed, ignoring anything malformed."""
    with config_dir() as dir_fd:
        raw = read_file(dir_fd, CONFIG_FILE, MAX_CONFIG_BYTES)
    if raw is None:
        return {}
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    config: dict = {}
    if is_valid_mac(document.get("mac")):
        config["mac"] = normalise_mac(document["mac"])
    if document.get("lastSpeed") is not None:
        store_speed(config, document["lastSpeed"])
    return config


def save_config(updates: dict) -> dict:
    """Merge validated ``updates`` into ``config.json`` and publish it atomically."""
    if not isinstance(updates, dict):
        raise StorageError("configuration must be a JSON object")
    unknown = set(updates) - {"mac", "lastSpeed"}
    if unknown:
        raise StorageError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
    config = load_config()
    if "mac" in updates:
        if updates["mac"] in (None, ""):
            config.pop("mac", None)
        else:
            config["mac"] = normalise_mac(updates["mac"])
    if updates.get("lastSpeed") is not None:
        store_speed(config, updates["lastSpeed"])
    with config_dir() as dir_fd:
        payload = json.dumps(config, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        write_file(dir_fd, CONFIG_FILE, payload, MAX_CONFIG_BYTES)
    return config


def store_speed(config: dict, value: object) -> None:
    """Store a validated speed, or leave the stored one alone if it is invalid."""
    with contextlib.suppress(ValueError):
        config["lastSpeed"] = clamp_speed(value)


def resolve_mac(cli_value: str | None) -> str:
    """Return the pad's MAC address: flag first, then environment, then config.

    Raises:
        ValueError: if none of them holds a syntactically valid address.
    """
    if cli_value:
        return normalise_mac(cli_value)
    from_environment = os.environ.get("OWALKY_MAC", "")
    if from_environment:
        return normalise_mac(from_environment)
    configured = load_config().get("mac")
    if configured:
        return normalise_mac(configured)
    raise ValueError("no MAC address configured: set one in the panel, or export OWALKY_MAC")


def status_document() -> dict:
    """The panel's whole view of the plugin as one small JSON document.

    The state file only counts while the recorded daemon is still the process that
    recorded itself.
    """
    with state_dir() as dir_fd:
        state = clean_state(load_state(dir_fd))
        alive = daemon_alive(dir_fd)
    config = load_config()
    return {
        "daemon": alive,
        "connected": bool(alive and state.get("connected")),
        "running": bool(alive and state.get("running")),
        "paused": bool(alive and state.get("paused")),
        "speed": state.get("speed", config.get("lastSpeed", 1.0)),
        "distance": state.get("distance", 0),
        "error": state.get("error", ""),
        "mac": config.get("mac", ""),
        "lastSpeed": config.get("lastSpeed", 1.0),
        "log": log_path(),
    }


__all__ = [
    "clean_state",
    "load_config",
    "load_state",
    "resolve_mac",
    "save_config",
    "save_state",
    "status_document",
]
