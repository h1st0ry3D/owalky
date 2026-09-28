"""Signalling by identity, and the control socket the command line speaks.

The identity tests point a record at the test runner's own pid, so a missing check
would kill the runner instead of passing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
import unittest
from unittest import mock

from owalky import identity, ipc, storage
from owalky.daemon import PadDaemon


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patch = mock.patch.object(storage, "home_dir", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)

    def open_state(self) -> int:
        return storage.open_dir(storage.state_components())


class ProcessIdentityTests(unittest.TestCase):
    def test_the_current_process_has_an_identity(self):
        live = identity.process_identity(os.getpid())
        self.assertIsNotNone(live)
        self.assertEqual(live[0], os.getpid())
        self.assertEqual(live[2], os.geteuid())

    def test_an_unknown_pid_has_none(self):
        self.assertIsNone(identity.process_identity(2**22 - 1))


class DaemonRecordTests(TempHomeTest):
    def setUp(self):
        super().setUp()
        self.dir_fd = self.open_state()
        self.addCleanup(os.close, self.dir_fd)

    def write_raw(self, document: bytes) -> None:
        storage.write_file(self.dir_fd, storage.PID_FILE, document, 4096)

    def test_a_record_for_this_process_counts_as_alive(self):
        identity.write_record(self.dir_fd)
        self.assertTrue(identity.daemon_alive(self.dir_fd))

    def test_a_malformed_record_is_not_alive(self):
        for document in (b"", b"{}", b"[]", b'{"pid": "x"}', b'{"pid": 1e999}'):
            with self.subTest(document=document):
                self.write_raw(document)
                self.assertFalse(identity.daemon_alive(self.dir_fd))

    def test_a_record_with_the_wrong_start_time_is_refused_and_nothing_is_signalled(self):
        # the runner is the target, so a missing check kills it rather than passing
        identity.write_record(self.dir_fd)
        record = identity.read_record(self.dir_fd)
        self.assertIsNotNone(record)
        mismatched = {"pid": record[0], "startTime": record[1] + 1, "uid": record[2]}
        storage.write_file(self.dir_fd, storage.PID_FILE, json.dumps(mismatched).encode(), 4096)
        self.assertFalse(identity.daemon_alive(self.dir_fd))
        self.assertFalse(identity.stop_daemon(self.dir_fd))
        self.assertEqual(identity.process_identity(os.getpid())[:2], record[:2])
        self.assertFalse(os.path.exists(os.path.join(storage.state_path(), storage.PID_FILE)))

    def test_a_stale_record_is_cleaned_up_rather_than_signalled(self):
        self.write_raw(b'{"pid": 2147483646, "startTime": 1, "uid": 0}')
        self.assertFalse(identity.stop_daemon(self.dir_fd))
        self.assertIsNone(identity.read_record(self.dir_fd))

    def test_signalling_a_dead_record_reports_failure(self):
        self.write_raw(b'{"pid": 2147483646, "startTime": 1, "uid": 0}')
        record = identity.read_record(self.dir_fd)
        self.assertFalse(identity.signal_owned(record, signal.SIGTERM))


class ControlSocketTests(TempHomeTest):
    """Drive the daemon's socket server, and the real client, without hardware."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(ipc, "state_path", return_value=self._home.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.socket_file = os.path.join(self._home.name, storage.SOCKET_FILE)

    def exchange(self, commands: list[str]) -> list[str]:
        """Serve PadDaemon's handler and talk to it with the production client.

        One event loop serves both, with the blocking client call in a thread.
        The socket's mode is read while it is bound, since closing the server
        unlinks it.
        """
        return self.scenario(commands)[0]

    def scenario(self, commands: list[str]) -> tuple[list[str], int]:
        daemon = PadDaemon("AA:BB:CC:DD:EE:FF")

        async def run() -> tuple[list[str], int]:
            server = await asyncio.start_unix_server(
                daemon._serve_client, path=self.socket_file, limit=ipc.MAX_COMMAND_BYTES
            )
            os.chmod(self.socket_file, 0o600)
            mode = os.stat(self.socket_file).st_mode & 0o777
            try:
                replies = [await asyncio.to_thread(ipc.send_command, command, 2.0) for command in commands]
            finally:
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
            return replies, mode

        return asyncio.run(run())

    def raw_exchange(self, payload: bytes) -> str:
        """Send bytes the production client would refuse to send."""

        async def run() -> str:
            daemon = PadDaemon("AA:BB:CC:DD:EE:FF")
            server = await asyncio.start_unix_server(
                daemon._serve_client, path=self.socket_file, limit=ipc.MAX_COMMAND_BYTES
            )
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket_file)
                writer.write(payload)
                await writer.drain()
                reply = await reader.readline()
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            finally:
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
            return reply.decode().strip()

        return asyncio.run(run())

    def test_ping_is_answered_without_a_link(self):
        self.assertEqual(self.exchange(["ping"]), ["ok"])

    def test_belt_commands_are_refused_without_a_link(self):
        replies = self.exchange(["start", "pause", "resume", "stop", "speed 1.5", "disconnect"])
        self.assertEqual(replies, ["err no BLE link"] * 5 + ["ok disconnecting"])

    def test_unknown_and_empty_commands_are_refused(self):
        self.assertEqual(
            self.exchange(["rm -rf /", "", "   "]),
            ["err unknown command", "err empty command", "err empty command"],
        )

    def test_the_server_refuses_a_command_past_the_cap(self):
        # the client refuses to send this, so the server is driven over a raw
        # connection
        self.assertEqual(self.raw_exchange(b"speed " + b"9" * 200 + b"\n"), "err command too long")

    def test_the_socket_is_private(self):
        _, mode = self.scenario(["ping"])
        self.assertEqual(mode, 0o600)

    def test_the_client_reports_a_missing_daemon(self):
        with self.assertRaises(ipc.DaemonUnavailable):
            ipc.send_command("ping", timeout=1)

    def test_the_client_refuses_an_oversized_command(self):
        with self.assertRaises(ValueError):
            ipc.send_command("speed " + "9" * 200)


class DaemonCommandTests(TempHomeTest):
    """The command grammar, checked without a BLE link."""

    def test_every_command_is_answered(self):
        daemon = PadDaemon("AA:BB:CC:DD:EE:FF")
        for command in ("ping", "status", "start", "stop", "nonsense", ""):
            with self.subTest(command=command):
                reply = asyncio.run(daemon.command(command))
                self.assertTrue(reply.startswith(("ok", "err")), reply)

    def test_status_is_a_small_json_document(self):
        reply = asyncio.run(PadDaemon("AA:BB:CC:DD:EE:FF").command("status"))
        self.assertLess(len(reply), 512)
        self.assertEqual(json.loads(reply.removeprefix("ok ")), {})


if __name__ == "__main__":
    unittest.main()
