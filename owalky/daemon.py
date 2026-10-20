"""The background daemon: one BLE link, one control socket, one state document.

The pad only accepts a control point write from a central that has claimed control,
and reconnecting takes seconds, so the link is held for as long as the plugin is
connected. Commands from the socket run on the same event loop as the link, so belt
commands cannot interleave.

Two things about this pad are not visible in the protocol:

* Notifications are not proof of movement. The pad keeps echoing an idle speed
  after it stops, so ``running`` and ``paused`` follow the commands the panel sends
  and the notification stream only supplies speed and distance.
* Resume re-claims control, replays the last speed, then sends 0x07.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import struct
import time

from .activity import MIN_SESSION_METERS, record_session
from .ftms import (
    CONTROL_POINT_UUID,
    DEFAULT_MODE,
    OP_PAUSE,
    OP_REQUEST_CONTROL,
    OP_START_RESUME,
    OP_STOP,
    TREADMILL_DATA_UUID,
    TreadmillSample,
    clamp_speed,
    encode_speed,
    max_speed_for,
)
from .identity import write_record
from .ipc import MAX_COMMAND_BYTES, MAX_REPLY_BYTES
from .state import clean_state, load_config, load_state, save_state
from .storage import PID_FILE, SOCKET_FILE, StorageError, log_append, remove_file, state_dir, state_path

SCAN_ATTEMPTS = 20
SCAN_TIMEOUT = 1.0
LINK_POLL_SECONDS = 0.5
STOP_SETTLE_SECONDS = 0.8
COMMAND_DELAY_SECONDS = 0.2
PUBLISH_INTERVAL_SECONDS = 1.0

#: The pad notifies about every 1.4s while it is idle. Silence for four
#: intervals means the link is gone even if the Bluetooth stack still calls it
#: connected, which is what happens when the pad is powered off.
MEASUREMENT_TIMEOUT_SECONDS = 6.0

COMMANDS = ("ping", "status", "start", "pause", "resume", "stop", "disconnect", "speed")


class PadDaemon:
    """Owns the BLE link, serves the control socket, and publishes the state."""

    def __init__(self, mac: str) -> None:
        self.mac = mac
        self.client = None
        self.speed_kmh: float | None = None
        self.speed_payload: bytes | None = None
        self.distance_m = 0
        #: Set on the first distance notification, so a start command can tell a
        #: known odometer from one the daemon has never read.
        self._distance_seen = False
        #: The workout, from Start until the belt stops. ``session_start_m`` is
        #: the odometer reading the session is measured from; it is rebound when
        #: the pad resets its odometer mid-session.
        self.session_active = False
        self.session_start_m: int | None = None
        self.session_distance_m = 0
        self.closing = False
        self._published_at = 0.0
        self._last_measurement = 0.0

    # -- state
    def publish(self, *, min_interval: float = 0.0, **updates: object) -> None:
        """Merge ``updates`` into the published state.

        Measurement notifications arrive several times a second, so those writes
        pass a minimum interval. A command-driven write is immediate.
        """
        now = time.monotonic()
        if min_interval and self._published_at and now - self._published_at < min_interval:
            return
        self._published_at = now
        with state_dir() as dir_fd:
            save_state(dir_fd, updates)

    def log(self, message: str) -> None:
        """Append one line to the debug log."""
        with state_dir() as dir_fd:
            log_append(dir_fd, message)

    def snapshot(self) -> dict:
        """The current published state, cleaned for the panel."""
        with state_dir() as dir_fd:
            return clean_state(load_state(dir_fd))

    # -- BLE
    @staticmethod
    def bleak():
        """Import bleak, or say how to install it.

        Deferred so that every subcommand except the daemon runs without
        python-bleak installed. The panel shows this message to the user.
        """
        try:
            from bleak import BleakClient, BleakScanner  # noqa: PLC0415 - see above
        except ImportError as exc:
            raise RuntimeError("python-bleak is missing: sudo pacman -S python-bleak") from exc
        return BleakClient, BleakScanner

    async def _find_device(self):
        """Scan for the pad, which only advertises for about 30 seconds."""
        _client_class, scanner = self.bleak()

        for attempt in range(SCAN_ATTEMPTS):
            device = None
            try:
                device = await scanner.find_device_by_address(self.mac, timeout=SCAN_TIMEOUT)
            except Exception as exc:  # the scanner raises a long tail of D-Bus errors
                self.log(f"scan error: {exc}")
            if device is not None:
                self.log(f"found the pad on scan {attempt + 1}")
                return device
            await asyncio.sleep(0.5)
        return None

    async def _open_link(self) -> None:
        client_class, _scanner = self.bleak()
        self.log("looking for the pad")
        device = await self._find_device()
        if device is None:
            self.publish(connected=False, running=False, error="pad not advertising")
            raise RuntimeError("pad not advertising: power-cycle it, then press Connect")

        client = client_class(device.address, timeout=15)
        try:
            await client.connect()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.disconnect()
            self.publish(connected=False, running=False, error="not connected")
            raise RuntimeError(f"could not connect to the pad: {exc}") from exc
        if not client.is_connected:
            with contextlib.suppress(Exception):
                await client.disconnect()
            self.publish(connected=False, running=False, error="not connected")
            raise RuntimeError("could not connect to the pad")

        self.client = client
        self.log("connected")
        self.publish(connected=True, running=False, paused=False, error="")
        with contextlib.suppress(Exception):
            await client.start_notify(TREADMILL_DATA_UUID, self._on_measurement)
            self.log("subscribed to treadmill data")
        await client.write_gatt_char(CONTROL_POINT_UUID, OP_REQUEST_CONTROL, response=False)
        self.publish(connected=True, running=False, paused=False)
        self.log("control point claimed, waiting for Start")

    def _on_measurement(self, _sender: object, data: bytearray) -> None:
        """Fold one Treadmill Data notification into the published state."""
        self._last_measurement = time.monotonic()
        if self.closing:
            return  # the link is going down; a late notification must not undo that
        sample = TreadmillSample.decode(bytes(data))
        if sample is None:
            return
        updates: dict = {"connected": True}
        if sample.speed_kmh is not None:
            updates["speed"] = sample.speed_kmh
        if sample.distance_m is not None:
            self._distance_seen = True
            self.distance_m = sample.distance_m
            updates["distance"] = sample.distance_m
            if self.session_active:
                self._update_session(sample.distance_m)
                updates["sessionDistance"] = self.session_distance_m
        self.publish(min_interval=PUBLISH_INTERVAL_SECONDS, **updates)

    # -- the workout in progress
    def _begin_session(self) -> None:
        """Start measuring a workout, without resetting one already in progress."""
        if self.session_active:
            return
        self.session_active = True
        self.session_distance_m = 0
        # A fresh daemon has not read the pad's odometer yet. Taking the current
        # value as the baseline would then count whatever the pad last reported
        # as this workout, so the first notification after Start sets it instead.
        self.session_start_m = self.distance_m if self._distance_seen else None

    def _update_session(self, distance_m: int) -> None:
        """Fold one odometer reading into the workout distance.

        The pad may reset its odometer while the belt is running. A reading below
        the baseline is treated as that reset, not as negative distance: the
        baseline moves back so the metres already covered are kept.
        """
        if self.session_start_m is None:
            self.session_start_m = distance_m
        elif distance_m < self.session_start_m:
            self.session_start_m = distance_m - self.session_distance_m
        self.session_distance_m = max(0, distance_m - self.session_start_m)

    def _finish_session(self) -> int:
        """Record the workout, and return the metres it covered.

        Called for Stop, Disconnect, a dropped link and shutdown, so a workout is
        not lost because the panel closed or the pad was switched off. It is
        idempotent: the state is cleared before the write, and a later call sees
        no session.
        """
        if not self.session_active:
            return 0
        meters = self.session_distance_m
        self.session_active = False
        self.session_start_m = None
        self.session_distance_m = 0
        if meters < MIN_SESSION_METERS:
            return 0
        try:
            with state_dir() as dir_fd:
                document = record_session(dir_fd, meters)
                log_append(dir_fd, f"session recorded: {meters} m, {document['trainingDays']} total")
        except (OSError, StorageError, ValueError) as exc:
            self.log(f"session not recorded: {exc}")
            return 0
        return meters

    def ceiling(self) -> float:
        """The speed ceiling for the mode the user has selected."""
        return max_speed_for(load_config().get("mode", DEFAULT_MODE))

    async def _write_control(self, payload: bytes) -> None:
        if self.client is None:
            raise RuntimeError("no BLE link: press Connect first")
        await self.client.write_gatt_char(CONTROL_POINT_UUID, payload, response=False)

    async def _stop_belt(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self._write_control(OP_STOP)
                await asyncio.sleep(COMMAND_DELAY_SECONDS)
        self._finish_session()
        self.publish(
            running=False,
            paused=False,
            connected=False,
            sessionActive=False,
            sessionDistance=0,
        )

    # -- commands
    async def command(self, line: str) -> str:
        """Handle one command line and return one reply line.

        A command outside :data:`COMMANDS` is refused before its arguments are read.
        """
        parts = line.split()
        if not parts:
            return "err empty command"
        name, arguments = parts[0], parts[1:]
        if name not in COMMANDS:
            return "err unknown command"
        if name == "ping":
            return "ok"
        if name == "status":
            return "ok " + json.dumps(self.snapshot(), separators=(",", ":"))
        if name == "disconnect":
            await self._stop_belt()
            self.closing = True
            return "ok disconnecting"
        return await self._linked_command(name, arguments)

    async def _linked_command(self, name: str, arguments: list[str]) -> str:
        """Run a belt command, which needs a live link to mean anything."""
        if self.client is None:
            return "err no BLE link"
        try:
            await self._belt_command(name, arguments)
        except Exception as exc:
            self.log(f"{name} failed: {exc}")
            return f"err {name} failed: {exc}"
        return "ok"

    async def _belt_command(self, name: str, arguments: list[str]) -> None:
        """Run one belt command. Only reached with a live link."""
        if name == "start":
            await self._write_control(OP_START_RESUME)
            await asyncio.sleep(COMMAND_DELAY_SECONDS)
            self._begin_session()
            self.publish(
                running=True,
                paused=False,
                speed=self.speed_kmh or 1.0,
                sessionActive=True,
                sessionDistance=self.session_distance_m,
            )
            self.log("started")
        elif name == "resume":
            await self._write_control(OP_REQUEST_CONTROL)
            await asyncio.sleep(COMMAND_DELAY_SECONDS)
            if self.speed_payload:
                await self._write_control(self.speed_payload)
                await asyncio.sleep(COMMAND_DELAY_SECONDS)
            await self._write_control(OP_START_RESUME)
            self._begin_session()
            self.publish(
                running=True,
                paused=False,
                sessionActive=True,
                sessionDistance=self.session_distance_m,
            )
            self.log("resumed")
        elif name == "pause":
            await self._write_control(OP_PAUSE)
            self.publish(paused=True)
            self.log("paused")
        elif name == "stop":
            await self._write_control(OP_STOP)
            await asyncio.sleep(STOP_SETTLE_SECONDS)
            meters = self._finish_session()
            self.publish(
                running=False,
                paused=False,
                distance=self.distance_m,
                sessionActive=False,
                sessionDistance=0,
            )
            self.log(f"stopped, staying connected ({meters} m this session)")
        elif name == "speed":
            if not arguments:
                raise ValueError("speed needs a value in km/h")
            self.speed_kmh = clamp_speed(arguments[0], self.ceiling())
            self.speed_payload = encode_speed(self.speed_kmh)
            await self._write_control(self.speed_payload)
            self.publish(speed=self.speed_kmh)
            self.log(f"speed {self.speed_kmh} km/h")

    # -- lifecycle
    async def run(self) -> int:
        """Serve the socket until the link drops, or Disconnect is requested."""
        socket_file = os.path.join(state_path(), SOCKET_FILE)
        with state_dir() as dir_fd:
            remove_file(dir_fd, SOCKET_FILE)
            server = await asyncio.start_unix_server(self._serve_client, path=socket_file, limit=MAX_COMMAND_BYTES)
            os.chmod(socket_file, 0o600)
            identity = write_record(dir_fd)
        self.log(f"daemon {identity[0]} listening on {socket_file}")
        self.publish(
            connected=False,
            running=False,
            paused=False,
            sessionActive=False,
            sessionDistance=0,
            error="",
        )
        try:
            await self._open_link()
            await self._hold_link()
        except Exception as exc:
            self.log(f"daemon stopped: {exc}")
            self.publish(connected=False, running=False, error=str(exc)[:120])
            return 1
        finally:
            await self._shutdown(server)
        return 0

    async def _hold_link(self) -> None:
        """Watch the link until it drops, keeping the socket served meanwhile."""
        while not self.closing:
            await asyncio.sleep(LINK_POLL_SECONDS)
            reason = self.link_loss_reason()
            if reason is None:
                continue
            self.log(reason)
            meters = self._finish_session()
            self.publish(
                connected=False,
                running=False,
                paused=False,
                sessionActive=False,
                sessionDistance=0,
                error=reason,
            )
            if meters:
                self.log(f"link lost with {meters} m recorded")
            return

    def link_loss_reason(self) -> str | None:
        """Why the link counts as gone, or None while it is alive.

        ``is_connected`` alone is not enough: powering the pad off leaves the
        bearer up on the Bluetooth stack, which keeps reporting a connection
        that no longer answers. Silence from the pad settles it.
        """
        if self.client is None:
            return None
        if not self.client.is_connected:
            return "link lost"
        if not self._last_measurement:
            return None
        if time.monotonic() - self._last_measurement > MEASUREMENT_TIMEOUT_SECONDS:
            return "the pad stopped sending data"
        return None

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one connection: same-uid peer, one command, one reply."""
        try:
            if not peer_is_us(writer):
                await reply(writer, "err peer uid")
                return
            try:
                line = (await reader.readline()).decode("ascii", "replace").strip()
            except (ValueError, asyncio.LimitOverrunError):
                await reply(writer, "err command too long")
                return
            if len(line) > MAX_COMMAND_BYTES:
                await reply(writer, "err command too long")
                return
            await reply(writer, await self.command(line))
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _shutdown(self, server: asyncio.AbstractServer) -> None:
        """Stop the belt, drop the link, and remove the socket and pid file."""
        with contextlib.suppress(Exception):
            await self._stop_belt()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            self.client = None
        # The last word belongs to the shutdown, not to a notification that
        # arrived while the link was closing.
        self.publish(
            connected=False,
            running=False,
            paused=False,
            sessionActive=False,
            sessionDistance=0,
        )
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        with state_dir() as dir_fd:
            remove_file(dir_fd, SOCKET_FILE)
            remove_file(dir_fd, PID_FILE)
        self.log("daemon stopped")


def peer_is_us(writer: asyncio.StreamWriter) -> bool:
    """Check the connecting process's uid: the socket is 0600, this is exact."""
    raw_socket = writer.get_extra_info("socket")
    if raw_socket is None:
        return False
    credentials = raw_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid == os.geteuid()


async def reply(writer: asyncio.StreamWriter, text: str) -> None:
    """Write one reply line, capped so a client never has to buffer more."""
    writer.write(text.encode("utf-8", "replace")[: MAX_REPLY_BYTES - 1] + b"\n")
    with contextlib.suppress(ConnectionError, OSError):
        await writer.drain()


def run_daemon(mac: str) -> int:
    """Run the daemon in the foreground until it is asked to stop."""
    daemon = PadDaemon(mac)

    async def supervise() -> int:
        loop = asyncio.get_running_loop()
        for number in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(number, request_stop, daemon)
        return await daemon.run()

    return asyncio.run(supervise())


def request_stop(daemon: PadDaemon) -> None:
    """Ask the daemon to finish: the belt is stopped and the link closed."""
    daemon.closing = True
