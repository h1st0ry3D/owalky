"""The daemon's view of the link, without any Bluetooth hardware.

Both cases here were reported from the panel: a fresh daemon that has not found
the pad yet, and a pad that is powered off while the Bluetooth stack still calls
the connection up.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from owalky import daemon as daemon_module
from owalky import ftms
from owalky.activity import MIN_SESSION_METERS, load_activity
from owalky.cli import clear_connection_state, spawn_daemon
from owalky.daemon import MEASUREMENT_TIMEOUT_SECONDS, PadDaemon
from owalky.identity import write_record
from owalky.state import load_state, save_state
from owalky.storage import PID_FILE, SOCKET_FILE, state_dir, state_path, write_file

MAC = "AA:BB:CC:DD:EE:FF"


class StubClient:
    """Stands in for a bleak client that stays connected and says nothing."""

    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.writes: list[bytes] = []

    async def write_gatt_char(self, _characteristic, payload, **_keywords):
        self.writes.append(payload)

    async def start_notify(self, *_arguments, **_keywords):
        return None

    async def disconnect(self):
        self.is_connected = False


class LinkLossTests(unittest.TestCase):
    def daemon_with(self, client: StubClient | None, measured_ago: float | None) -> PadDaemon:
        instance = PadDaemon(MAC)
        instance.client = client
        if measured_ago is not None:
            instance._last_measurement = time.monotonic() - measured_ago
        return instance

    def test_a_live_link_reports_no_loss(self):
        instance = self.daemon_with(StubClient(), measured_ago=1.0)
        self.assertIsNone(instance.link_loss_reason())

    def test_a_dropped_ble_link_is_reported(self):
        instance = self.daemon_with(StubClient(connected=False), measured_ago=0.0)
        self.assertEqual(instance.link_loss_reason(), "link lost")

    def test_silence_is_loss_even_while_bluetooth_says_connected(self):
        # This is the powered-off pad: the bearer stays up, nothing answers.
        instance = self.daemon_with(StubClient(connected=True), measured_ago=MEASUREMENT_TIMEOUT_SECONDS + 1)
        self.assertEqual(instance.link_loss_reason(), "the pad stopped sending data")

    def test_a_pad_that_has_never_spoken_is_not_called_lost(self):
        instance = self.daemon_with(StubClient(connected=True), measured_ago=None)
        self.assertIsNone(instance.link_loss_reason())

    def test_a_measurement_clears_the_silence(self):
        instance = self.daemon_with(StubClient(), measured_ago=99)
        instance._on_measurement(None, bytearray.fromhex("00006400"))
        self.assertIsNone(instance.link_loss_reason())


class DaemonStartTests(unittest.TestCase):
    """A daemon that cannot find the pad must not leave a stale connection behind."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch("owalky.storage.home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)
        with state_dir() as dir_fd:
            write_file(
                dir_fd,
                "state.json",
                json.dumps({"connected": True, "running": True, "paused": True, "speed": 3.0}).encode(),
                65536,
            )
        self.addCleanup(self.remove_socket)

    def remove_socket(self):
        path = os.path.join(state_path(), SOCKET_FILE)
        if os.path.exists(path):
            os.unlink(path)

    def test_state_is_cleared_before_the_daemon_scans(self):
        async def scenario() -> int:
            instance = PadDaemon(MAC)
            with (
                mock.patch.object(instance, "bleak", return_value=(StubClient, None)),
                mock.patch.object(instance, "_find_device", return_value=None),
            ):
                return await instance.run()

        self.assertEqual(asyncio.run(scenario()), 1)
        with state_dir() as dir_fd:
            state = load_state(dir_fd)
            self.assertIs(state.get("connected"), False)
            self.assertIs(state.get("running"), False)
            self.assertIs(state.get("paused"), False)
            self.assertIn("not advertising", state.get("error", ""))
            self.assertFalse(os.path.exists(os.path.join(state_path(), PID_FILE)))

    def test_the_daemon_leaves_no_process_behind_on_failure(self):
        async def scenario() -> None:
            instance = PadDaemon(MAC)
            with (
                mock.patch.object(instance, "bleak", return_value=(StubClient, None)),
                mock.patch.object(instance, "_find_device", return_value=None),
            ):
                await instance.run()

        asyncio.run(scenario())
        self.assertFalse(os.path.exists(os.path.join(state_path(), SOCKET_FILE)))


class SpawnResetTests(unittest.TestCase):
    """``connect`` must clear the previous session's state before forking.

    ``Popen`` is stubbed out on purpose. A real spawn would fork a daemon that
    writes to the real state directory and unlinks the real control socket, which
    breaks the daemon the user is actually connected through.
    """

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        for target in ("owalky.storage.home_dir", "owalky.cli.home_dir"):
            patch = mock.patch(target, return_value=self._home.name)
            patch.start()
            self.addCleanup(patch.stop)
        with state_dir() as dir_fd:
            write_file(
                dir_fd,
                "state.json",
                json.dumps({"connected": True, "running": True, "paused": True}).encode(),
                65536,
            )
        # The fake Popen records this test process as the daemon, so the
        # readiness poll succeeds at once and no child process is ever forked.
        spawn = mock.patch("owalky.cli.subprocess.Popen", side_effect=self._record_this_process)
        spawn.start()
        self.addCleanup(spawn.stop)

    def _record_this_process(self, *_arguments, **_keywords):
        with state_dir() as dir_fd:
            write_record(dir_fd)

    def test_clear_connection_state_drops_a_stale_connection(self):
        with state_dir() as dir_fd:
            clear_connection_state(dir_fd)
            state = load_state(dir_fd)
        self.assertIs(state.get("connected"), False)
        self.assertIs(state.get("running"), False)
        self.assertIs(state.get("paused"), False)
        self.assertEqual(state.get("error"), "")

    def test_spawn_daemon_clears_the_state_before_forking(self):
        spawn_daemon(MAC)
        with state_dir() as dir_fd:
            state = load_state(dir_fd)
        self.assertIs(state.get("connected"), False)
        self.assertIs(state.get("running"), False)


class ShutdownOrderTests(unittest.TestCase):
    """Nothing may republish a connection once the daemon starts stopping."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch("owalky.storage.home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)

    def read_state(self) -> dict:
        with state_dir() as dir_fd:
            return load_state(dir_fd)

    def test_a_notification_after_closing_does_not_resurrect_the_link(self):
        instance = PadDaemon(MAC)
        with state_dir() as dir_fd:
            save_state(dir_fd, {"connected": True, "running": True})
        instance.client = StubClient(connected=True)
        instance.closing = True
        instance._on_measurement(None, bytearray.fromhex("00006400"))
        self.assertIs(self.read_state().get("connected"), True)  # untouched, not re-published
        self.assertIs(instance._last_measurement > 0, True)  # the watchdog clock still runs

    def test_a_notification_while_live_is_published(self):
        instance = PadDaemon(MAC)
        instance.client = StubClient(connected=True)
        instance._on_measurement(None, bytearray.fromhex("00006400"))
        state = self.read_state()
        self.assertIs(state.get("connected"), True)
        self.assertEqual(state.get("speed"), 1.0)

    def test_shutdown_publishes_disconnected_last(self):
        instance = PadDaemon(MAC)
        instance.client = StubClient(connected=True)
        with state_dir() as dir_fd:
            save_state(dir_fd, {"connected": True, "running": True, "paused": True})

        async def scenario():
            await instance._shutdown(
                await asyncio.start_unix_server(
                    instance._serve_client, path=os.path.join(state_path(), SOCKET_FILE), limit=64
                )
            )

        asyncio.run(scenario())
        state = self.read_state()
        self.assertIs(state.get("connected"), False)
        self.assertIs(state.get("running"), False)
        self.assertIs(state.get("paused"), False)
        self.addCleanup(
            lambda: (
                os.path.exists(os.path.join(state_path(), SOCKET_FILE))
                and os.unlink(os.path.join(state_path(), SOCKET_FILE))
            )
        )


def distance_payload(meters: int) -> bytearray:
    """One Treadmill Data notification with speed 0 and a distance.

    Instantaneous speed is present whenever flag bit 0 is clear, so the two
    zero bytes at offset 2 are part of every distance notification.
    """
    return bytearray(
        bytes((0x04, 0x00)) + (0).to_bytes(2, "little") + meters.to_bytes(3, "little")
    )


def speed_payload(kmh: float) -> bytearray:
    """A speed-only Treadmill Data notification."""
    return bytearray(bytes((0x00, 0x00)) + int(round(kmh * 100)).to_bytes(2, "little"))


def metric_payload(
    meters: int, *, energy: int = 0, elapsed: int = 0, speed_centi: int = 300
) -> bytearray:
    """A pad-shaped notification with distance, Total Energy and Elapsed Time.

    Flags 0x2484 is what the Mobvoi pad sends: distance, energy, elapsed time,
    plus its reserved bit 13 and the three vendor bytes that follow it.
    """
    return bytearray(
        bytes((0x84, 0x24))
        + speed_centi.to_bytes(2, "little")
        + meters.to_bytes(3, "little")
        + energy.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + bytes((0,))
        + elapsed.to_bytes(2, "little")
        + b"\x00\x00\x00"
    )


class SessionTrackingTests(unittest.TestCase):
    """The workout distance is the daemon's, not the panel's.

    A shell reload loses every property in Panel.qml, so recording the session
    there would make it disappear. The daemon holds the baseline and writes the
    history when the belt stops.
    """

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch("owalky.storage.home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)

    def measured_daemon(self) -> PadDaemon:
        instance = PadDaemon(MAC)
        # The odometer is read before the workout, so Start has a real baseline.
        instance._on_measurement(None, distance_payload(1000))
        return instance

    def history(self) -> dict:
        with state_dir() as dir_fd:
            return load_activity(dir_fd)

    def test_a_workout_records_the_delta_from_start(self):
        instance = self.measured_daemon()
        instance._begin_session()
        instance._on_measurement(None, distance_payload(1400))
        self.assertEqual(instance.session_distance_m, 400)
        self.assertEqual(instance._finish_session(), 400)
        document = self.history()
        self.assertEqual(document["trainingDays"], 1)
        self.assertEqual(document["history"][-1]["meters"], 400)

    def test_finishing_twice_does_not_record_twice(self):
        instance = self.measured_daemon()
        instance._begin_session()
        instance._on_measurement(None, distance_payload(1600))
        self.assertEqual(instance._finish_session(), 600)
        self.assertEqual(instance._finish_session(), 0)
        self.assertEqual(self.history()["trainingDays"], 1)

    def test_a_short_workout_is_not_recorded(self):
        instance = self.measured_daemon()
        instance._begin_session()
        instance._on_measurement(None, distance_payload(1000 + MIN_SESSION_METERS - 1))
        self.assertEqual(instance._finish_session(), 0)
        self.assertEqual(self.history()["history"], [])

    def test_an_odometer_reset_keeps_the_metres_already_covered(self):
        instance = self.measured_daemon()
        instance._begin_session()
        instance._on_measurement(None, distance_payload(1300))
        instance._on_measurement(None, distance_payload(50))     # pad reset to zero
        self.assertEqual(instance.session_distance_m, 300)
        instance._on_measurement(None, distance_payload(250))
        self.assertEqual(instance.session_distance_m, 500)

    def test_the_published_state_carries_the_workout_distance(self):
        instance = self.measured_daemon()
        instance._begin_session()
        instance.publish(
            running=True,
            paused=False,
            sessionActive=True,
            sessionDistance=instance.session_distance_m,
        )
        instance._published_at = 0.0       # past the measurement write interval
        instance._on_measurement(None, distance_payload(1750))
        with state_dir() as dir_fd:
            document = load_state(dir_fd)
        self.assertIs(document.get("sessionActive"), True)
        self.assertEqual(document.get("sessionDistance"), 750)

    def test_a_start_without_a_reading_takes_the_first_notification_as_baseline(self):
        instance = PadDaemon(MAC)
        instance._begin_session()
        instance._on_measurement(None, distance_payload(5000))
        self.assertEqual(instance.session_distance_m, 0)
        instance._on_measurement(None, distance_payload(5200))
        self.assertEqual(instance.session_distance_m, 200)

    def test_energy_and_elapsed_time_from_the_pad_fill_the_session(self):
        instance = PadDaemon(MAC)
        instance._on_measurement(None, metric_payload(1000, energy=0, elapsed=0))
        instance._begin_session()
        instance._on_measurement(None, metric_payload(1400, energy=20, elapsed=60))
        self.assertEqual(instance.session_distance_m, 400)
        self.assertEqual(instance.session_calories_kcal, 20)
        self.assertIs(instance._calories_from_pad, True)

    def test_a_stale_energy_total_is_subtracted_from_the_session(self):
        instance = PadDaemon(MAC)
        instance._on_measurement(None, metric_payload(1000, energy=5, elapsed=0))
        instance._begin_session()
        instance._on_measurement(None, metric_payload(1100, energy=5, elapsed=10))
        self.assertEqual(instance.session_calories_kcal, 0)
        instance._on_measurement(None, metric_payload(1200, energy=25, elapsed=20))
        self.assertEqual(instance.session_calories_kcal, 20)

    def test_the_recorded_session_carries_time_and_calories(self):
        instance = PadDaemon(MAC)
        instance._on_measurement(None, metric_payload(1000, energy=0, elapsed=0))
        instance._begin_session()
        instance._on_measurement(None, metric_payload(1400, energy=20, elapsed=60))
        instance._session_started -= 600
        self.assertEqual(instance._finish_session(), 400)
        document = self.history()
        entry = document["history"][-1]
        self.assertGreaterEqual(entry["seconds"], 600)
        self.assertEqual(entry["calories"], 20)
        self.assertIs(entry["estimated"], False)

    def test_the_fallback_calorie_estimate_is_marked_estimated(self):
        instance = PadDaemon(MAC)
        instance._on_measurement(None, distance_payload(0))
        instance._begin_session()
        instance._on_measurement(None, distance_payload(400))
        instance._session_started -= 600
        instance._refresh_session_metrics()
        self.assertGreater(instance.session_calories_kcal, 0)
        self.assertIs(instance._calories_from_pad, False)
        instance._published_at = 0.0
        instance._on_measurement(None, distance_payload(500))
        with state_dir() as dir_fd:
            document = load_state(dir_fd)
        self.assertIs(document.get("sessionCaloriesEstimated"), True)
        self.assertGreater(document.get("sessionCalories"), 0)
        self.assertGreaterEqual(document.get("sessionElapsed"), 600)

    def test_speed_integration_is_used_when_the_distance_field_stays_zero(self):
        instance = PadDaemon(MAC)
        instance._on_measurement(None, speed_payload(2.0))
        instance._begin_session()
        instance._last_speed_kmh = 2.0
        for _ in range(6):                     # six 5s samples at 2.0 km/h
            instance._last_speed_at -= 5.0
            instance._on_measurement(None, speed_payload(2.0))
        expected = instance.session_distance_m
        self.assertGreaterEqual(expected, 16)
        self.assertEqual(instance._finish_session(), expected)

    def test_start_defaults_to_one_kmh(self):
        instance = PadDaemon(MAC)
        client = StubClient()
        instance.client = client
        asyncio.run(instance._belt_command("start", []))
        self.assertEqual(client.writes, [ftms.OP_START_RESUME])
        self.assertEqual(instance.speed_kmh, 1.0)

    def test_start_sends_the_target_speed_before_and_after_start(self):
        instance = PadDaemon(MAC)
        client = StubClient()
        instance.client = client
        asyncio.run(instance._belt_command("start", ["2.5"]))
        self.assertEqual(
            client.writes,
            [ftms.OP_START_RESUME, ftms.encode_speed(2.5)],
        )
        self.assertEqual(instance.speed_kmh, 2.5)

    def test_a_zero_energy_field_falls_back_to_the_calorie_estimate(self):
        # This is what the Mobvoi pad does: Expended Energy is announced and
        # always zero while the Total Distance field is zero too.
        instance = PadDaemon(MAC)
        instance._on_measurement(None, metric_payload(0, energy=0, elapsed=0))
        instance._begin_session()
        instance._last_speed_kmh = 2.0
        instance._session_started -= 600
        for _ in range(6):                     # enough samples to pass 10 m
            instance._last_speed_at -= 5.0
            instance._on_measurement(None, metric_payload(0, energy=0, elapsed=0, speed_centi=200))
        self.assertGreater(instance.session_distance_m, 0)
        self.assertIs(instance._calories_from_pad, False)
        self.assertGreater(instance.session_calories_kcal, 0)
        instance._finish_session()
        entry = self.history()["history"][-1]
        self.assertIs(entry["estimated"], True)


class ConstantTests(unittest.TestCase):
    def test_the_silence_window_covers_several_notification_intervals(self):
        # The pad was measured at about one notification every 1.4s.
        self.assertGreaterEqual(MEASUREMENT_TIMEOUT_SECONDS, 4 * 1.4)

    def test_the_poll_is_quicker_than_the_silence_window(self):
        self.assertLess(daemon_module.LINK_POLL_SECONDS, MEASUREMENT_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
