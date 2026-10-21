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
        self.assertEqual(ftms.SPEED_MIN_KMH, 1.0)
        self.assertEqual(ftms.SPEED_MAX_KMH, 12.0)
        self.assertEqual(ftms.clamp_speed(0.0), 1.0)
        self.assertEqual(ftms.clamp_speed(0.5), 1.0)
        self.assertEqual(ftms.clamp_speed(-4), 1.0)
        self.assertEqual(ftms.clamp_speed(99), 12.0)

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
    def test_speed_is_present_when_flag_zero_is_clear(self):
        # flags 0x0000: instantaneous speed at bytes 2-3, 1.00 km/h
        sample = ftms.TreadmillSample.decode(bytes.fromhex("00006400"))
        self.assertEqual(sample.speed_kmh, 1.0)
        self.assertIsNone(sample.distance_m)
        self.assertIsNone(sample.elapsed_s)

    def test_distance_comes_after_the_speed_field(self):
        # flags 0x0004: speed then total distance (300 m)
        sample = ftms.TreadmillSample.decode(bytes.fromhex("040064002c0100"))
        self.assertEqual(sample.speed_kmh, 1.0)
        self.assertEqual(sample.distance_m, 300)

    def test_the_pad_idle_payload_is_read_at_the_spec_offsets(self):
        # Captured from the Mobvoi pad: flags 0x2484, speed 3.24 km/h, the rest
        # zero after the session was stopped. Bit 13 is reserved and carries
        # three vendor bytes after the elapsed time.
        sample = ftms.TreadmillSample.decode(
            bytes.fromhex("8424440100000000000000000000000000")
        )
        self.assertEqual(sample.flags, 0x2484)
        self.assertEqual(sample.speed_kmh, 3.24)
        self.assertEqual(sample.distance_m, 0)
        self.assertEqual(sample.energy_kcal, 0)
        self.assertEqual(sample.elapsed_s, 0)

    def test_energy_and_elapsed_time_are_decoded_when_announced(self):
        # flags: distance, expended energy, elapsed time (0x0484)
        # speed 3.00, distance 1200, energy 45 kcal, 200 kcal/h, 3 kcal/min,
        # elapsed 600 s.
        payload = bytes.fromhex("84042c01b004002d00c800035802")
        sample = ftms.TreadmillSample.decode(payload)
        self.assertEqual(sample.speed_kmh, 3.0)
        self.assertEqual(sample.distance_m, 1200)
        self.assertEqual(sample.energy_kcal, 45)
        self.assertEqual(sample.elapsed_s, 600)

    def test_the_pad_reserved_bit_and_vendor_tail_are_ignored(self):
        # flags 0x2484: distance, energy, elapsed, plus the pad's reserved bit
        # 13 and its three extra bytes.
        payload = bytes.fromhex("84242c01b004002d00c800035802aabbcc")
        sample = ftms.TreadmillSample.decode(payload)
        self.assertEqual(sample.speed_kmh, 3.0)
        self.assertEqual(sample.distance_m, 1200)
        self.assertEqual(sample.energy_kcal, 45)
        self.assertEqual(sample.elapsed_s, 600)

    def test_more_data_without_speed_still_reads_distance(self):
        # bit 0 set means no instantaneous speed; bit 2 means distance follows.
        sample = ftms.TreadmillSample.decode(bytes.fromhex("05002c0100"))
        self.assertIsNone(sample.speed_kmh)
        self.assertEqual(sample.distance_m, 300)

    def test_no_announced_fields_is_valid(self):
        sample = ftms.TreadmillSample.decode(bytes.fromhex("0100"))
        self.assertEqual(sample.flags, 1)
        self.assertIsNone(sample.speed_kmh)
        self.assertIsNone(sample.distance_m)

    def test_truncated_and_oversized_payloads_are_refused(self):
        for payload in (b"", b"\x01", bytes.fromhex("04002c01"), bytes(200)):
            with self.subTest(payload=payload.hex()):
                self.assertIsNone(ftms.TreadmillSample.decode(payload))

    def test_an_announced_field_that_does_not_fit_is_refused(self):
        # bits 0 and 2 clear/set: speed and distance are announced; only one
        # speed byte follows.
        self.assertIsNone(ftms.TreadmillSample.decode(bytes.fromhex("040064")))


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
