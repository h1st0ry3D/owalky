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
        instance._on_measurement(None, bytearray.fromhex("01006400"))
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
        instance._on_measurement(None, bytearray.fromhex("01006400"))
        self.assertIs(self.read_state().get("connected"), True)  # untouched, not re-published
        self.assertIs(instance._last_measurement > 0, True)  # the watchdog clock still runs

    def test_a_notification_while_live_is_published(self):
        instance = PadDaemon(MAC)
        instance.client = StubClient(connected=True)
        instance._on_measurement(None, bytearray.fromhex("01006400"))
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
    """One Treadmill Data notification carrying only the distance field."""
    return bytearray(bytes((0x04, 0x00)) + meters.to_bytes(3, "little"))


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


class ConstantTests(unittest.TestCase):
    def test_the_silence_window_covers_several_notification_intervals(self):
        # The pad was measured at about one notification every 1.4s.
        self.assertGreaterEqual(MEASUREMENT_TIMEOUT_SECONDS, 4 * 1.4)

    def test_the_poll_is_quicker_than_the_silence_window(self):
        self.assertLess(daemon_module.LINK_POLL_SECONDS, MEASUREMENT_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
