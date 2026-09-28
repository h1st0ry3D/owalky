#!/usr/bin/python3
"""FTMS codec for the Owalky walking pad.

Pure functions, standard library only, no I/O. This is the only place the pad's
opcodes and notification layout are encoded and decoded, so the codec can be tested
without a pad attached. Notes on the protocol are in ``docs/ftms.md``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
CONTROL_POINT_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"
TREADMILL_DATA_UUID = "00002acd-0000-1000-8000-00805f9b34fb"

# Fitness Machine Control Point opcodes, as bytes so callers never build hex.
OP_REQUEST_CONTROL = bytes.fromhex("01")  # 0x01
OP_START_RESUME = bytes.fromhex("07")  # 0x07
OP_STOP = bytes.fromhex("0801")  # 0x08 + control value 0x01
OP_PAUSE = bytes.fromhex("0802")  # 0x08 + control value 0x02
OP_SET_TARGET_SPEED = 0x02  # 0x02 + uint16 speed in 0.01 km/h, little endian

SPEED_MIN_KMH = 1.0
SPEED_MAX_KMH = 12.0
SPEED_STEP_KMH = 0.1
DEFAULT_SPEED_KMH = 1.0

#: Treadmill Data notifications are 10-20 bytes; anything larger is not one.
MAX_NOTIFICATION_BYTES = 64

#: Flags field plus at least one announced field.
MIN_NOTIFICATION_BYTES = 2

#: 0x0001 speed present, 0x0002 instantaneous stride, 0x0004 distance present.
_FLAG_SPEED = 0x0001
_FLAG_STRIDE = 0x0002
_FLAG_DISTANCE = 0x0004

MAC_PATTERN = re.compile(r"\A(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


def normalise_mac(value: object) -> str:
    """Return ``value`` as an upper-case colon-separated BLE address.

    Raises:
        ValueError: the value is not six hex octet pairs. Nothing reaches an argv
            or a socket without passing through here first.
    """
    if not isinstance(value, str):
        raise ValueError("MAC address must be a string")
    text = value.strip()
    if not MAC_PATTERN.match(text):
        raise ValueError(f"not a BLE MAC address: {text[:32]!r}")
    return text.upper()


def is_valid_mac(value: object) -> bool:
    """Return whether ``value`` is a syntactically valid BLE MAC address."""
    try:
        normalise_mac(value)
    except ValueError:
        return False
    return True


def clamp_speed(value: object) -> float:
    """Clamp ``value`` to the pad's range and round it to 0.1 km/h.

    Raises:
        ValueError: the value is not a finite number. Input from the socket or the
            command line is never replaced with a default.
    """
    try:
        speed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"speed is not a number: {value!r}") from exc
    if not math.isfinite(speed):
        raise ValueError("speed must be finite")
    speed = round(speed / SPEED_STEP_KMH) * SPEED_STEP_KMH
    return min(SPEED_MAX_KMH, max(SPEED_MIN_KMH, round(speed, 1)))


def encode_speed(speed: object) -> bytes:
    """Return the Set Target Speed payload for ``speed`` km/h."""
    value = int(round(clamp_speed(speed) * 100))
    return bytes((OP_SET_TARGET_SPEED, value & 0xFF, (value >> 8) & 0xFF))


@dataclass(frozen=True, slots=True)
class TreadmillSample:
    """One decoded Treadmill Data notification.

    A ``None`` field was not in the notification's flags, which is not the same as
    a zero measurement.
    """

    flags: int
    speed_kmh: float | None = None
    distance_m: int | None = None

    @classmethod
    def decode(cls, data: bytes) -> TreadmillSample | None:
        """Decode a notification, or return ``None`` if it is not usable.

        The pad omits fields its flags do not announce, so decoding walks the
        payload in flag order and gives up when an announced field does not fit.
        """
        if not MIN_NOTIFICATION_BYTES <= len(data) <= MAX_NOTIFICATION_BYTES:
            return None
        flags = data[0] | (data[1] << 8)
        offset = 2
        speed: float | None = None
        distance: int | None = None
        if flags & _FLAG_SPEED:
            if len(data) < offset + 2:
                return None
            speed = (data[offset] | (data[offset + 1] << 8)) / 100.0
            offset += 2
        if flags & _FLAG_STRIDE:
            offset += 2  # stride is reported but not used by the panel
        if flags & _FLAG_DISTANCE:
            if len(data) < offset + 3:
                return None
            distance = int.from_bytes(data[offset : offset + 3], "little")
        return cls(flags=flags, speed_kmh=speed, distance_m=distance)
