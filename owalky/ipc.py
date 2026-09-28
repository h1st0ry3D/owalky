"""The control socket the command line uses to reach the running daemon.

The daemon serves a 0600 socket inside its own 0700 state directory and checks the
peer's uid with ``SO_PEERCRED`` before reading anything. One connection carries one
length-capped command line in and one reply line out.
"""

from __future__ import annotations

import os
import socket

from .storage import SOCKET_FILE, state_path

MAX_COMMAND_BYTES = 64
MAX_REPLY_BYTES = 255
DEFAULT_TIMEOUT = 10.0


class DaemonUnavailable(Exception):
    """No daemon is listening, so the command was not delivered."""


def send_command(command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Send one command to the daemon and return its one-line reply.

    Raises:
        DaemonUnavailable: nothing accepted the connection.
        ValueError: the command is not one short ASCII line.
    """
    payload = command.encode("ascii")
    if len(payload) + 1 > MAX_COMMAND_BYTES:
        raise ValueError("command too long")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        try:
            client.connect(os.path.join(state_path(), SOCKET_FILE))
        except OSError as exc:
            raise DaemonUnavailable(exc.strerror or str(exc)) from exc
        client.sendall(payload + b"\n")
        client.shutdown(socket.SHUT_WR)
        reply = b""
        while len(reply) <= MAX_REPLY_BYTES:
            chunk = client.recv(MAX_REPLY_BYTES + 1)
            if not chunk:
                break
            reply += chunk
    if len(reply) > MAX_REPLY_BYTES:
        raise DaemonUnavailable("daemon reply too long")
    return reply.decode("utf-8", "replace").strip()
