"""The hardware flow, run against a real pad. Skipped unless a MAC is given.

    OWALKY_TEST_MAC=AA:BB:CC:DD:EE:FF python3 -m unittest tests.test_hardware -v
    ./run_tests.sh --hardware --mac AA:BB:CC:DD:EE:FF

This drives the belt, so do not run it while walking on the pad. The pad advertises
for about 30 seconds after it is power-cycled and accepts one central at a time, so
disconnect LightBlue, the vendor app and any other BLE tool first.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import unittest

from owalky import ftms

MAC = os.environ.get("OWALKY_TEST_MAC", "")
BLUETOOTHCTL = "/usr/bin/bluetoothctl"


def bluetooth(*arguments: str) -> None:
    """Run bluetoothctl with a fixed argv, if it is installed."""
    if not os.path.exists(BLUETOOTHCTL):
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [BLUETOOTHCTL, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )


@unittest.skipUnless(MAC, "set OWALKY_TEST_MAC to run the hardware flow")
class HardwareFlowTests(unittest.TestCase):
    """The sequence this pad accepts, end to end."""

    maxDiff = None

    def setUp(self):
        try:
            import bleak  # noqa: F401, PLC0415 - a missing package is a skip, not an error
        except ImportError:
            self.skipTest("python-bleak is not installed")
        self.mac = ftms.normalise_mac(MAC)
        bluetooth("disconnect", self.mac)

    def tearDown(self):
        bluetooth("disconnect", self.mac)

    def test_full_flow(self):
        asyncio.run(self.flow())

    async def flow(self) -> None:
        from bleak import BleakClient  # noqa: PLC0415 - only reached once bleak is known to exist

        samples: list[ftms.TreadmillSample] = []

        def on_measurement(_sender: object, data: bytearray) -> None:
            sample = ftms.TreadmillSample.decode(bytes(data))
            if sample is not None:
                samples.append(sample)

        async with BleakClient(self.mac, timeout=15) as client:
            self.assertTrue(client.is_connected, "could not connect to the pad")
            await client.start_notify(ftms.TREADMILL_DATA_UUID, on_measurement)
            await self.control(client, ftms.OP_REQUEST_CONTROL)
            await self.control(client, ftms.encode_speed(1.0))
            await self.control(client, ftms.OP_START_RESUME)
            # This pad wants the speed repeated straight after Start.
            await self.control(client, ftms.encode_speed(1.0))
            await asyncio.sleep(3)
            self.assertTrue(samples, "no treadmill data notifications after Start")
            self.assertIsNotNone(samples[-1].distance_m, "no distance in the notifications")

            for speed in (1.1, 1.6, 2.0):
                await self.control(client, ftms.encode_speed(speed))
                await asyncio.sleep(2)

            await self.control(client, ftms.OP_PAUSE)
            await asyncio.sleep(2)
            await self.control(client, ftms.OP_START_RESUME)
            await self.control(client, ftms.encode_speed(2.0))
            await asyncio.sleep(2)
            await self.control(client, ftms.OP_STOP)
            await asyncio.sleep(2)
            with contextlib.suppress(Exception):
                await client.stop_notify(ftms.TREADMILL_DATA_UUID)

    @staticmethod
    async def control(client, payload: bytes) -> None:
        """Write one control point payload and give the pad a moment to react."""
        await client.write_gatt_char(ftms.CONTROL_POINT_UUID, payload, response=False)
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
