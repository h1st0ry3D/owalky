"""Scanning for pads: the FTMS filter, the labels, and the panel's parsing."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from owalky import cli, scan

FTMS = "00001826-0000-1000-8000-00805f9b34fb"
HEART_RATE = "0000180d-0000-1000-8000-00805f9b34fb"

PAD_MAC = "14:4C:1D:FA:89:17"


class StubAdvertisement:
    """The fields the scanner reads, and nothing else."""

    def __init__(self, local_name=None, service_uuids=None, rssi=None):
        self.local_name = local_name
        self.service_uuids = service_uuids
        self.rssi = rssi


class StubDevice:
    """A device as bleak hands one over: an address and a cached name."""

    def __init__(self, address, name=None):
        self.address = address
        self.name = name


class ServiceFilterTests(unittest.TestCase):
    def test_the_fitness_machine_service_is_recognised(self):
        for uuids in ([FTMS], [HEART_RATE, FTMS], {FTMS}, (FTMS,), FTMS, [FTMS.upper()]):
            with self.subTest(uuids=uuids):
                self.assertTrue(scan.advertises_ftms(uuids))

    def test_the_short_form_counts(self):
        self.assertTrue(scan.advertises_ftms(["1826"]))

    def test_anything_else_is_not_a_pad(self):
        for uuids in ([], [HEART_RATE], ["0000feaa-0000-1000-8000-00805f9b34fb"], None, 42, "nope"):
            with self.subTest(uuids=uuids):
                self.assertFalse(scan.advertises_ftms(uuids))


class CandidateTests(unittest.TestCase):
    def test_only_pads_come_back(self):
        found = scan.scan_candidates(
            [
                (PAD_MAC, "Mobvoi TMP", [FTMS], -57),
                ("AA:BB:CC:DD:EE:FF", "Not a pad", [HEART_RATE], -40),
            ]
        )
        self.assertEqual(found, [{"mac": PAD_MAC, "name": "Mobvoi TMP", "rssi": -57}])

    def test_the_nearest_pad_is_first(self):
        found = scan.scan_candidates(
            [
                ("AA:BB:CC:DD:EE:01", "Far", [FTMS], -90),
                ("AA:BB:CC:DD:EE:02", "Near", [FTMS], -40),
                ("AA:BB:CC:DD:EE:03", "Middle", [FTMS], -65),
            ]
        )
        self.assertEqual([device["name"] for device in found], ["Near", "Middle", "Far"])

    def test_a_device_without_a_signal_strength_sorts_last(self):
        found = scan.scan_candidates(
            [
                ("AA:BB:CC:DD:EE:01", "Silent", [FTMS], None),
                ("AA:BB:CC:DD:EE:02", "Loud", [FTMS], -80),
            ]
        )
        self.assertEqual([device["name"] for device in found], ["Loud", "Silent"])
        self.assertIsNone(found[1]["rssi"])

    def test_the_list_is_bounded(self):
        entries = [(f"AA:BB:CC:DD:EE:{index:02X}", f"pad{index}", [FTMS], -50 - index) for index in range(30)]
        self.assertEqual(len(scan.scan_candidates(entries)), scan.MAX_DEVICES)

    def test_an_unusable_address_is_left_out(self):
        found = scan.scan_candidates([("not-an-address", "Pad", [FTMS], -50)])
        self.assertEqual(found, [])

    def test_addresses_are_normalised(self):
        found = scan.scan_candidates([("14:4c:1d:fa:89:17", "Mobvoi TMP", [FTMS], -57)])
        self.assertEqual(found[0]["mac"], PAD_MAC)

    def test_bleak_device_mappings_are_understood(self):
        # The shape bleak 3 returns: address -> (device, advertisement).
        found = scan.scan_candidates(
            {PAD_MAC: (StubDevice(PAD_MAC, "Mobvoi TMP"), StubAdvertisement("Mobvoi TMP", [FTMS], -57))}
        )
        self.assertEqual(found, [{"mac": PAD_MAC, "name": "Mobvoi TMP", "rssi": -57}])

    def test_bleak_device_objects_are_understood(self):
        # The shape older bleak returns: device -> advertisement.
        found = scan.scan_candidates(
            {StubDevice(PAD_MAC, "Mobvoi TMP"): StubAdvertisement(None, [FTMS], -57)}
        )
        self.assertEqual(found[0]["name"], "Mobvoi TMP")
        self.assertEqual(found[0]["mac"], PAD_MAC)

    def test_the_advertised_name_wins_over_the_cached_one(self):
        found = scan.scan_candidates(
            {PAD_MAC: (StubDevice(PAD_MAC, "Stale"), StubAdvertisement("Mobvoi TMP", [FTMS], -57))}
        )
        self.assertEqual(found[0]["name"], "Mobvoi TMP")

    def test_a_pad_that_advertised_repeatedly_is_one_row(self):
        # A pad advertises several times, so the same address arrives more than
        # once. The user picks a pad, not an advertisement, so it is listed once.
        found = scan.scan_candidates(
            [
                (PAD_MAC, "Mobvoi TMP", [FTMS], -57),
                (PAD_MAC, "Mobvoi TMP", [FTMS], -61),
                ("AA:BB:CC:DD:EE:FF", "Mobvoi TMP", [FTMS], -55),
            ]
        )
        self.assertEqual([device["mac"] for device in found], ["AA:BB:CC:DD:EE:FF", PAD_MAC])

    def test_a_repeated_advertisement_keeps_the_strongest_sighting(self):
        found = scan.scan_candidates(
            [
                (PAD_MAC, "Mobvoi TMP", [FTMS], -80),
                (PAD_MAC, "Mobvoi TMP", [FTMS], -57),
                (PAD_MAC, "Mobvoi TMP", [FTMS], -70),
            ]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["rssi"], -57)

    def test_a_repeated_advertisement_keeps_the_name_it_has(self):
        # One sighting can lack a name, so a later sighting that has one supplies it.
        found = scan.scan_candidates(
            [
                (PAD_MAC, None, [FTMS], -57),
                (PAD_MAC, "Mobvoi TMP", [FTMS], -61),
            ]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Mobvoi TMP")

    def test_repeated_addresses_do_not_eat_the_limit(self):
        entries = [(PAD_MAC, "pad", [FTMS], -50)] * 20 + [
            (f"AA:BB:CC:DD:EE:{index:02X}", f"pad{index}", [FTMS], -50 - index) for index in range(10)
        ]
        self.assertEqual(len(scan.scan_candidates(entries)), scan.MAX_DEVICES)


class NameTests(unittest.TestCase):
    def test_control_characters_and_markup_are_dropped(self):
        # A name is attacker-chosen text the shell renders, so nothing that could
        # be markup or a control sequence survives into the label.
        self.assertEqual(scan.clean_name("Mobvoi\x00\x1b[31m TMP"), "Mobvoi[31m TMP")
        self.assertEqual(scan.clean_name("<b>pad</b>"), "bpad/b")

    def test_non_ascii_is_dropped(self):
        self.assertEqual(scan.clean_name("Mobvoié \u4e2d"), "Mobvoi")

    def test_the_length_is_capped(self):
        self.assertEqual(len(scan.clean_name("x" * 200)), scan.MAX_NAME_CHARS)

    def test_a_missing_name_gets_a_readable_placeholder(self):
        for value in (None, "", "   ", "\x00\x00"):
            with self.subTest(value=value):
                self.assertEqual(scan.clean_name(value), scan.FALLBACK_NAME)

    def test_a_pad_without_a_name_still_appears(self):
        found = scan.scan_candidates([(PAD_MAC, None, [FTMS], -57)])
        self.assertEqual(found[0]["name"], scan.FALLBACK_NAME)


class StrengthTests(unittest.TestCase):
    def test_absurd_values_are_bounded(self):
        self.assertEqual(scan.read_rssi(9999), 20)
        self.assertEqual(scan.read_rssi(-9999), -127)

    def test_a_missing_or_nonsense_strength_is_none(self):
        for value in (None, "close", True, float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(scan.read_rssi(value))


class DocumentTests(unittest.TestCase):
    """The document the panel parses stays small however many pads answer."""

    def devices(self) -> list[dict]:
        return scan.scan_candidates(
            [(f"AA:BB:CC:DD:EE:{index:02X}", f"pad {index}", [FTMS], -50) for index in range(20)]
        )

    def test_the_document_is_small(self):
        self.assertLess(len(json.dumps({"devices": self.devices()})), 1024)

    def test_only_three_fields_are_offered(self):
        self.assertEqual(set(self.devices()[0]), {"mac", "name", "rssi"})

    def test_a_name_at_its_cap_still_fits(self):
        devices = scan.scan_candidates([(PAD_MAC, "x" * 500, [FTMS], -57)])
        self.assertLess(len(json.dumps({"devices": devices})), 256)


class ClampTests(unittest.TestCase):
    """The scan window is bounded before the scanner is asked for it.

    ``discover`` is patched rather than ``asyncio.run``, so the timeout that
    actually reaches bleak is what is asserted.
    """

    def discover_timeout(self, seconds: float) -> float:
        async def discover(timeout: float, return_adv: bool = False):
            self.assertTrue(return_adv)
            return [(PAD_MAC, "Mobvoi TMP", [FTMS], -57)]

        with mock.patch("bleak.BleakScanner.discover", side_effect=discover) as scanner:
            self.assertEqual(scan.scan_devices(seconds)[0]["mac"], PAD_MAC)
        return scanner.call_args.kwargs["timeout"]

    def test_a_nonsense_duration_is_clamped_into_the_window(self):
        self.assertEqual(self.discover_timeout(0.0), scan.MIN_SCAN_SECONDS)
        self.assertEqual(self.discover_timeout(-600.0), scan.MIN_SCAN_SECONDS)
        self.assertEqual(self.discover_timeout(9000.0), scan.MAX_SCAN_SECONDS)

    def test_a_reasonable_duration_is_left_alone(self):
        self.assertEqual(self.discover_timeout(4.5), 4.5)


class CommandTests(unittest.TestCase):
    """The subcommand prints one document and stores nothing."""

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        # stderr carries the failure message of a refused command, and is not
        # what the panel parses.
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = cli.main_entry(argv)
        return code, buffer.getvalue()

    def test_a_scan_prints_the_devices_as_json(self):
        found = [{"mac": PAD_MAC, "name": "Mobvoi TMP", "rssi": -57}]
        with mock.patch("owalky.cli.scan_devices", return_value=found):
            code, output = self.run_cli(["scan"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(output.splitlines()[0]), {"devices": found})

    def test_an_empty_scan_says_why(self):
        with mock.patch("owalky.cli.scan_devices", return_value=[]):
            code, output = self.run_cli(["scan"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("power-cycle", output)

    def test_a_scan_never_touches_the_configuration(self):
        with (
            mock.patch("owalky.cli.save_config") as save,
            mock.patch("owalky.cli.scan_devices", return_value=[]),
        ):
            self.run_cli(["scan"])
        save.assert_not_called()

    def test_the_seconds_argument_is_taken_from_the_command_line(self):
        with mock.patch("owalky.cli.scan_devices", return_value=[]) as devices:
            self.run_cli(["scan", "3"])
        devices.assert_called_once_with(3.0)

    def test_a_broken_scanner_reports_through_the_exit_status(self):
        with mock.patch("owalky.cli.scan_devices", side_effect=OSError("no adapter")):
            code, _output = self.run_cli(["scan"])
        self.assertEqual(code, cli.EXIT_FAILED)


if __name__ == "__main__":
    unittest.main()