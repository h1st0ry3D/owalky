"""The protocol codec: speed encoding, notification decoding, MAC grammar."""

from __future__ import annotations

import math
import unittest

from owalky import ftms


class SpeedEncodingTests(unittest.TestCase):
    def test_known_speeds_match_the_documented_bytes(self):
        self.assertEqual(ftms.encode_speed(1.0).hex(), "026400")
        self.assertEqual(ftms.encode_speed(1.1).hex(), "026e00")
        self.assertEqual(ftms.encode_speed(2.0).hex(), "02c800")
        self.assertEqual(ftms.encode_speed(12.0).hex(), "02b004")  # 1200 = 0x04b0, little endian

    def test_speeds_are_rounded_to_a_tenth(self):
        self.assertEqual(ftms.clamp_speed(1.04), 1.0)
        self.assertEqual(ftms.clamp_speed(1.06), 1.1)
        self.assertEqual(ftms.clamp_speed("2.34"), 2.3)

    def test_speeds_are_clamped_to_the_pad_range(self):
        self.assertEqual(ftms.clamp_speed(0.0), ftms.SPEED_MIN_KMH)
        self.assertEqual(ftms.clamp_speed(-4), ftms.SPEED_MIN_KMH)
        self.assertEqual(ftms.clamp_speed(99), ftms.SPEED_MAX_KMH)

    def test_non_finite_and_non_numeric_speeds_are_refused(self):
        for value in (float("nan"), math.inf, -math.inf, "fast", None, object()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ftms.clamp_speed(value)

    def test_encoding_always_produces_three_bytes(self):
        for speed in (0.5, 1.0, 6.5, 12.0):
            with self.subTest(speed=speed):
                self.assertEqual(len(ftms.encode_speed(speed)), 3)


class MacGrammarTests(unittest.TestCase):
    def test_valid_addresses_are_upper_cased(self):
        self.assertEqual(ftms.normalise_mac("aa:bb:cc:dd:ee:ff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(ftms.normalise_mac(" AA:BB:CC:DD:EE:FF "), "AA:BB:CC:DD:EE:FF")

    def test_anything_that_is_not_an_address_is_refused(self):
        for value in (
            "",
            "aa:bb:cc:dd:ee",
            "aa:bb:cc:dd:ee:ff:00",
            "aabbccddeeff",
            "aa:bb:cc:dd:ee:gg",
            "aa-bb-cc-dd-ee-ff",
            "-oProxyCommand=x",
            "aa:bb:cc:dd:ee:ff; rm -rf /",
            "aa:bb:cc:dd:ee:ff\nstart",
            "aa:bb:cc:dd:ee:f\xc3\xa9",
            None,
            12345,
        ):
            with self.subTest(value=value):
                self.assertFalse(ftms.is_valid_mac(value))
                with self.assertRaises(ValueError):
                    ftms.normalise_mac(value)


class NotificationDecodingTests(unittest.TestCase):
    def test_speed_only(self):
        sample = ftms.TreadmillSample.decode(bytes.fromhex("01006400"))
        self.assertEqual(sample.speed_kmh, 1.0)
        self.assertIsNone(sample.distance_m)

    def test_distance_only(self):
        sample = ftms.TreadmillSample.decode(bytes.fromhex("04002c0100"))
        self.assertEqual(sample.distance_m, 300)
        self.assertIsNone(sample.speed_kmh)

    def test_stride_field_is_skipped(self):
        # flags 0x0007: speed, stride, distance
        payload = bytes.fromhex("0700640064002c0100")
        sample = ftms.TreadmillSample.decode(payload)
        self.assertEqual(sample.speed_kmh, 1.0)
        self.assertEqual(sample.distance_m, 300)

    def test_no_announced_fields(self):
        sample = ftms.TreadmillSample.decode(bytes.fromhex("0000"))
        self.assertEqual(sample.flags, 0)
        self.assertIsNone(sample.speed_kmh)
        self.assertIsNone(sample.distance_m)

    def test_truncated_and_oversized_payloads_are_refused(self):
        for payload in (b"", b"\x01", bytes.fromhex("0100"), bytes.fromhex("04002c01"), bytes(200)):
            with self.subTest(payload=payload.hex()):
                self.assertIsNone(ftms.TreadmillSample.decode(payload))


class OpcodeTests(unittest.TestCase):
    def test_opcodes_match_the_protocol_notes(self):
        self.assertEqual(ftms.OP_REQUEST_CONTROL.hex(), "01")
        self.assertEqual(ftms.OP_START_RESUME.hex(), "07")
        self.assertEqual(ftms.OP_STOP.hex(), "0801")
        self.assertEqual(ftms.OP_PAUSE.hex(), "0802")
        self.assertEqual(ftms.OP_SET_TARGET_SPEED, 0x02)

    def test_uuids_are_the_ble_base_form(self):
        for uuid in (ftms.SERVICE_UUID, ftms.CONTROL_POINT_UUID, ftms.TREADMILL_DATA_UUID):
            with self.subTest(uuid=uuid):
                self.assertRegex(uuid, r"\A[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


if __name__ == "__main__":
    unittest.main()
