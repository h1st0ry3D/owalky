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

#: With the handle bar down the pad only accepts walking speeds. The bar position
#: is not readable over BLE, so the mode is chosen by the user.
WALK_MAX_KMH = 6.0
SPEED_CEILINGS = {"walk": WALK_MAX_KMH, "run": SPEED_MAX_KMH}
DEFAULT_MODE = "walk"
MODE_NAMES = tuple(SPEED_CEILINGS)

#: Treadmill Data notifications are 10-20 bytes; anything larger is not one.
MAX_NOTIFICATION_BYTES = 64

#: Flags field plus at least one announced field.
MIN_NOTIFICATION_BYTES = 2

#: Treadmill Data flag bits (FTMS 1.0, current GATT Specification). Bit 0 is
#: inverted: it is set when there is more data / no instantaneous speed.
_FLAG_MORE_DATA = 0x0001
_FLAG_AVERAGE_SPEED = 0x0002
_FLAG_DISTANCE = 0x0004
_FLAG_INCLINATION = 0x0008
_FLAG_ELEVATION = 0x0010
_FLAG_INSTANT_PACE = 0x0020
_FLAG_AVERAGE_PACE = 0x0040
_FLAG_ENERGY = 0x0080
_FLAG_HEART_RATE = 0x0100
_FLAG_MET = 0x0200
_FLAG_ELAPSED = 0x0400
_FLAG_REMAINING = 0x0800
_FLAG_FORCE_POWER = 0x1000

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


def max_speed_for(mode: object) -> float:
    """The ceiling for a mode name. An unknown or missing mode means walk."""
    return SPEED_CEILINGS.get(str(mode), SPEED_CEILINGS[DEFAULT_MODE])


def clamp_speed(value: object, maximum: float = SPEED_MAX_KMH) -> float:
    """Clamp ``value`` to the pad's range and round it to 0.1 km/h.

    ``maximum`` is the ceiling for the current mode, never above the pad's own
    maximum.

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
    ceiling = min(float(maximum), SPEED_MAX_KMH)
    speed = round(speed / SPEED_STEP_KMH) * SPEED_STEP_KMH
    return min(ceiling, max(SPEED_MIN_KMH, round(speed, 1)))


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
    elapsed_s: int | None = None
    energy_kcal: int | None = None

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

        def take(size: int) -> int | None:
            """Consume one little-endian field, or None when it does not fit."""
            nonlocal offset
            if len(data) < offset + size:
                return None
            value = int.from_bytes(data[offset : offset + size], "little")
            offset += size
            return value

        speed: float | None = None
        distance: int | None = None
        elapsed: int | None = None
        energy: int | None = None

        # Present when bit 0 is 0. This pad sends bit 0 as 0 and puts the
        # instantaneous speed at bytes 2-3.
        if not flags & _FLAG_MORE_DATA:
            value = take(2)
            if value is None:
                return None
            speed = value / 100.0
        if flags & _FLAG_AVERAGE_SPEED:
            if take(2) is None:
                return None
        if flags & _FLAG_DISTANCE:
            distance = take(3)
            if distance is None:
                return None
        if flags & _FLAG_INCLINATION:
            if take(4) is None:
                return None
        if flags & _FLAG_ELEVATION:
            if take(4) is None:
                return None
        if flags & _FLAG_INSTANT_PACE:
            if take(2) is None:
                return None
        if flags & _FLAG_AVERAGE_PACE:
            if take(2) is None:
                return None
        if flags & _FLAG_ENERGY:
            energy = take(2)
            if energy is None or take(2) is None or take(1) is None:
                return None
        if flags & _FLAG_HEART_RATE:
            if take(1) is None:
                return None
        if flags & _FLAG_MET:
            if take(1) is None:
                return None
        if flags & _FLAG_ELAPSED:
            elapsed = take(2)
            if elapsed is None:
                return None
        if flags & _FLAG_REMAINING:
            if take(2) is None:
                return None
        if flags & _FLAG_FORCE_POWER:
            if take(4) is None:
                return None
        # Bits 13-15 are reserved and announce no field. The pad sets bit 13
        # and appends three vendor bytes after the elapsed time; neither is read.
        return cls(
            flags=flags,
            speed_kmh=speed,
            distance_m=distance,
            elapsed_s=elapsed,
            energy_kcal=energy,
        )
