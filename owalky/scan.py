"""Finding pads that advertise the Fitness Machine Service.

Scanning is a separate concern from the codec in :mod:`owalky.ftms`, but it shares
its one piece of protocol knowledge: the service UUID a treadmill advertises. The
filtering, naming and ordering are pure functions, so the rules can be tested
without a radio, and only :func:`scan_devices` imports bleak.

A pad advertises for about 30 seconds after power-on, so a scan is one short
pass. Nothing is stored here: the panel shows what was found and the user picks
the address, which is validated as a MAC before it reaches the configuration.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Iterator

from .ftms import SERVICE_UUID, is_valid_mac, normalise_mac

#: Long enough to catch the pad's advertisements, short enough not to wait on it.
SCAN_SECONDS = 10.0
MIN_SCAN_SECONDS = 1.0
MAX_SCAN_SECONDS = 30.0

#: Enough to choose between a pad in the next room and one underfoot, no more.
MAX_DEVICES = 8
MAX_NAME_CHARS = 32
FALLBACK_NAME = "Unnamed pad"

#: The 16-bit form, which is how some stacks report the UUID.
SHORT_SERVICE_UUID = SERVICE_UUID[4:8]

#: A reported strength is never beyond what a radio can produce.
_STRONGEST_RSSI = 20
_WEAKEST_RSSI = -127

#: A rich-text renderer acts on these, so they never reach the panel's labels.
_MARKUP_CHARACTERS = "<>&"


def clean_name(value: object) -> str:
    """The advertised name as one short printable ASCII string.

    A BLE name is attacker-chosen text that the shell renders, so control
    characters, non-ASCII and markup delimiters are dropped rather than escaped,
    and what is left is capped. The panel applies the same stripping again on its
    own, because a scan document is only ever input, not trust.
    """
    text = "" if value is None else str(value)
    kept = "".join(
        character
        for character in text
        if " " <= character <= "~" and character not in _MARKUP_CHARACTERS
    )
    return kept.strip()[:MAX_NAME_CHARS] or FALLBACK_NAME


def advertises_ftms(service_uuids: object) -> bool:
    """Whether an advertisement lists the Fitness Machine Service.

    Both the 128-bit base form and the short ``1826`` form count, in either case.
    """
    if isinstance(service_uuids, str):
        service_uuids = [service_uuids]
    if not isinstance(service_uuids, (list, tuple, set, frozenset)):
        return False
    wanted = {SERVICE_UUID.lower(), SHORT_SERVICE_UUID}
    return any(str(uuid).strip().lower() in wanted for uuid in service_uuids)


def read_rssi(value: object) -> int | None:
    """The signal strength as an integer, or None when the stack did not report one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return max(_WEAKEST_RSSI, min(_STRONGEST_RSSI, int(value)))


def _advertisements(entries: object) -> Iterator[tuple]:
    """Yield ``(address, name, service_uuids, rssi)`` from a discovery result.

    Bleak has returned this as a ``{device: advertisement}`` mapping and as a
    ``{address: (device, advertisement)}`` mapping, so both are read. A plain
    iterable of the same tuples is accepted too, which is what the tests feed in.
    """
    pairs: Iterable = entries.items() if hasattr(entries, "items") else entries
    for entry in pairs:
        if isinstance(entry, tuple) and len(entry) == 4:
            yield entry
            continue
        # The newer mapping nests the pair under the address, the older one uses
        # the device as the key and the advertisement as the value.
        device, advertisement = entry[1] if _is_device_advertisement(entry[1]) else entry
        yield (
            getattr(device, "address", device),
            getattr(advertisement, "local_name", None) or getattr(device, "name", None),
            getattr(advertisement, "service_uuids", None),
            getattr(advertisement, "rssi", None),
        )


def _is_device_advertisement(value: object) -> bool:
    """Whether a mapping value is bleak's ``(device, advertisement)`` pair."""
    return isinstance(value, tuple) and len(value) == 2


def strength(device: dict) -> int:
    """The device's signal strength, with an unreported one as the weakest.

    One place, so the ordering and the test that describes it cannot disagree.
    """
    rssi = device.get("rssi")
    return rssi if isinstance(rssi, int) else _WEAKEST_RSSI


def scan_candidates(entries: object) -> list[dict]:
    """The FTMS devices in a discovery result: closest first, one row each.

    Anything without a valid address or without the service in its advertisement is
    left out, so the panel cannot offer a device it has no address for.

    A pad advertises repeatedly, so one address arrives as several entries. They are
    merged on the normalised address: the strongest reported strength wins, and a
    named sighting beats one that only had the placeholder, so a single pad is one row.
    """
    merged: dict[str, dict] = {}
    for address, name, service_uuids, rssi in _advertisements(entries):
        if not advertises_ftms(service_uuids) or not is_valid_mac(address):
            continue
        mac = normalise_mac(address)
        sighting = {"mac": mac, "name": clean_name(name), "rssi": read_rssi(rssi)}
        known = merged.get(mac)
        if known is None:
            merged[mac] = sighting
            continue
        if strength(sighting) > strength(known):
            known["rssi"] = sighting["rssi"]
        if sighting["name"] != FALLBACK_NAME:
            known["name"] = sighting["name"]
    # Strongest signal first, which is nearest. A negative RSSI is stronger the
    # closer it is to zero, so the key is negated, and an unreported strength
    # sorts last rather than outranking a real one.
    candidates = sorted(merged.values(), key=lambda device: (-strength(device), device["mac"]))
    return candidates[:MAX_DEVICES]





def scan_devices(seconds: float = SCAN_SECONDS) -> list[dict]:
    """Scan for ``seconds`` and return the pads that advertised.

    ``seconds`` is clamped rather than refused, so a caller cannot ask for a scan
    that never ends or one too short to see an advertisement.

    Raises:
        ImportError: python-bleak is missing, which the CLI reports as its own exit
            status.
        Exception: whatever the Bluetooth stack raised, which the CLI reports as a
            failed scan.
    """
    # Deferred so every other subcommand runs without python-bleak installed,
    # exactly as the daemon does it.
    from bleak import BleakScanner

    duration = min(max(float(seconds), MIN_SCAN_SECONDS), MAX_SCAN_SECONDS)
    entries = asyncio.run(BleakScanner.discover(timeout=duration, return_adv=True))
    return scan_candidates(entries)