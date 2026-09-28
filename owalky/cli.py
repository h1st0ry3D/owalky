"""The command line the QML panel calls, once per action.

Each subcommand prints one short line, or nothing, and exits with a status the panel
branches on. No subcommand builds a shell string, and every value is passed through
:mod:`owalky.ftms` before it reaches an argument.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import time

from . import __version__
from .daemon import run_daemon
from .ftms import clamp_speed
from .identity import daemon_alive, stop_daemon
from .ipc import DaemonUnavailable, send_command
from .state import load_state, resolve_mac, save_config, save_state, status_document
from .storage import (
    LOG_FILE,
    MAX_CONFIG_BYTES,
    MAX_LOG_BYTES,
    MAX_LOG_TAIL_BYTES,
    MAX_LOG_TAIL_LINES,
    StorageError,
    home_dir,
    log_append,
    read_file_tail,
    state_dir,
    write_file,
)

HELPER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "owalky_helper.py")
BLUETOOTHCTL = "/usr/bin/bluetoothctl"
CONNECT_TIMEOUT = 6.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REJECTED = 2
EXIT_MISSING_DEPENDENCY = 3


def build_parser() -> argparse.ArgumentParser:
    """Describe every subcommand, including the one the panel hides."""
    parser = argparse.ArgumentParser(
        prog="owalky_helper.py",
        description="Control an Owalky walking pad over Bluetooth LE (FTMS).",
    )
    parser.add_argument(
        "--mac",
        help="pad MAC address; otherwise $OWALKY_MAC, then config.json",
    )
    parser.add_argument("--version", action="version", version=f"owalky {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    subcommands.add_parser("status", help="print one line of JSON for the panel")
    subcommands.add_parser("connect", help="start the daemon and claim the pad")
    subcommands.add_parser("start", help="start the belt")
    subcommands.add_parser("pause", help="pause the belt")
    subcommands.add_parser("resume", help="resume the belt")
    subcommands.add_parser("stop", help="stop the belt, staying connected")
    subcommands.add_parser("disconnect", help="stop the belt and close the link")
    speed = subcommands.add_parser("speed", help="set the target speed")
    speed.add_argument("value", help=f"km/h, clamped to {clamp_speed(0)}-{clamp_speed(99)}")
    log_parser = subcommands.add_parser("log", help="print the tail of the debug log")
    log_parser.add_argument(
        "lines", nargs="?", type=int, default=40, help=f"lines to print, at most {MAX_LOG_TAIL_LINES}"
    )
    subcommands.add_parser("log-clear", help="truncate the debug log")
    subcommands.add_parser("config-set", help="store configuration read from stdin as JSON")
    subcommands.add_parser("daemon", description=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return the process exit status."""
    arguments = build_parser().parse_args(argv)
    handlers = {
        "status": command_status,
        "connect": command_connect,
        "disconnect": command_disconnect,
        "log": command_log,
        "log-clear": command_log_clear,
        "config-set": command_config_set,
        "daemon": command_daemon,
    }
    handler = handlers.get(arguments.command)
    if handler is not None:
        return handler(arguments)
    return command_forward(arguments)


# -- subcommands
def command_status(_arguments: argparse.Namespace) -> int:
    """Print the panel's whole view of the plugin as one line of JSON."""
    print(json.dumps(status_document(), separators=(",", ":")))
    return EXIT_OK


def command_connect(arguments: argparse.Namespace) -> int:
    """Start the daemon in its own session; it claims the pad from there."""
    mac = resolve_mac(arguments.mac)
    spawn_daemon(mac)
    print("connecting to the pad …")
    return EXIT_OK


def command_disconnect(arguments: argparse.Namespace) -> int:
    """Close the link: ask the daemon to stop the belt, then let it exit."""
    mac = resolve_mac(arguments.mac)
    with state_dir() as dir_fd:
        if not daemon_alive(dir_fd):
            return bluetoothctl_disconnect(mac)
        log_append(dir_fd, "disconnect requested")
        result = forward("disconnect")
        stop_daemon(dir_fd)
    print("disconnecting" if result == EXIT_OK else "disconnect failed")
    return result


def command_log(arguments: argparse.Namespace) -> int:
    """Print the tail of the debug log, bounded at the producer."""
    lines = min(max(arguments.lines, 1), MAX_LOG_TAIL_LINES)
    with state_dir() as dir_fd:
        tail = read_file_tail(dir_fd, LOG_FILE, MAX_LOG_TAIL_BYTES, lines)
    print(tail or "no log entries yet")
    return EXIT_OK


def command_log_clear(_arguments: argparse.Namespace) -> int:
    """Truncate the debug log through an atomic replacement."""
    with state_dir() as dir_fd:
        write_file(dir_fd, LOG_FILE, b"", MAX_LOG_BYTES)
    print("log cleared")
    return EXIT_OK


def command_config_set(_arguments: argparse.Namespace) -> int:
    """Store the configuration the panel sends on stdin, as one JSON object."""
    save_config(read_stdin_json(MAX_CONFIG_BYTES))
    print("configuration saved")
    return EXIT_OK


def command_daemon(arguments: argparse.Namespace) -> int:
    """Run the daemon in the foreground. Started by ``connect``, not by hand."""
    return run_daemon(resolve_mac(arguments.mac))


def command_forward(arguments: argparse.Namespace) -> int:
    """Forward a belt command to the daemon and report what it answered."""
    command = arguments.command
    if command == "speed":
        speed = clamp_speed(arguments.value)
        save_config({"lastSpeed": speed})
        with state_dir() as dir_fd:
            log_append(dir_fd, f"speed {speed} km/h requested")
        return forward(f"speed {speed:.1f}")
    return forward(command)


# -- helpers
def forward(command: str) -> int:
    """Send one command to the daemon and print its reply.

    Returns:
        The exit status the panel branches on: zero when the daemon accepted
        the command, non-zero when there was no daemon or it refused.
    """
    try:
        reply = send_command(command)
    except DaemonUnavailable as exc:
        print(f"no daemon: {exc}")
        return EXIT_FAILED
    if not reply.startswith("ok"):
        print(reply.removeprefix("err "))
        return EXIT_FAILED
    detail = reply.removeprefix("ok ").strip()
    print(detail or "done")
    return EXIT_OK


def minimal_environment() -> dict:
    """The fixed environment the daemon is restarted with.

    An inherited one would carry whatever the caller exported, PYTHONPATH included.
    """
    return {"PATH": "/usr/bin:/bin", "HOME": home_dir(), "LANG": "C"}


def spawn_daemon(mac: str) -> None:
    """Start the daemon detached, so it survives a shell reload.

    Raises:
        RuntimeError: a daemon is already running, or it did not come up within
            :data:`CONNECT_TIMEOUT` seconds.
    """
    interpreter = sys.executable
    if not os.path.isabs(interpreter):
        raise RuntimeError("cannot start the daemon: interpreter path is not absolute")
    with state_dir() as dir_fd:
        if daemon_alive(dir_fd):
            raise RuntimeError("already connected")
        stop_daemon(dir_fd)
        clear_connection_state(dir_fd)
        log_fd = os.open(
            LOG_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(log_fd, 0o600)
            log_append(dir_fd, f"connect requested for {mac}")
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell, minimal environment
                [interpreter, "-I", HELPER_PATH, "--mac", mac, "daemon"],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                close_fds=True,
                start_new_session=True,
                cwd="/",
                env=minimal_environment(),
            )
        finally:
            os.close(log_fd)
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if daemon_alive(dir_fd):
                return
            time.sleep(0.1)
    raise RuntimeError(start_failure())


def start_failure() -> str:
    """Why the daemon is not there, from the reason it published on its way out."""
    with state_dir() as dir_fd:
        reason = load_state(dir_fd).get("error", "")
    if reason:
        return f"the daemon did not start: {reason}"
    return "the daemon did not start; see the log for details"


def clear_connection_state(dir_fd: int) -> None:
    """Drop the previous session's connection from the published state.

    Nothing is connected until a daemon says so. Without this the panel keeps
    showing the last connection while the new daemon is still scanning for a pad
    that is powered off.
    """
    save_state(dir_fd, {"connected": False, "running": False, "paused": False, "error": ""})


def bluetoothctl_disconnect(mac: str) -> int:
    """Drop a link no daemon owns, so it does not block the next Connect."""
    if not os.path.exists(BLUETOOTHCTL):
        print("no daemon is running and bluetoothctl is not installed")
        return EXIT_FAILED
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [BLUETOOTHCTL, "disconnect", mac],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        print("disconnected with bluetoothctl")
        return EXIT_OK
    print("could not disconnect the pad")
    return EXIT_FAILED


def read_stdin_json(limit: int) -> dict:
    """Read one JSON object from stdin, without waiting for the writer to close.

    The panel writes the payload and leaves stdin open, so a read that waits for
    EOF never returns and the caller hangs. Each chunk is parsed as it arrives and
    the read stops as soon as the document is whole.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(0, min(4096, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        try:
            return _parse_config(b"".join(chunks))
        except ValueError:
            continue
    if total > limit:
        raise ValueError("configuration payload too large")
    if not chunks:
        return {}
    return _parse_config(b"".join(chunks))


def _parse_config(payload: bytes) -> dict:
    """Parse one configuration document, or raise why it cannot be used."""
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON on stdin: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("configuration must be a JSON object")
    return document


def main_entry() -> int:
    """Turn the expected failures into one clean line and an exit status."""
    try:
        return main()
    except (StorageError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REJECTED
    except ImportError:
        print("python-bleak is missing: sudo pacman -S python-bleak", file=sys.stderr)
        return EXIT_MISSING_DEPENDENCY
    except OSError as exc:
        reason = "no space left on device" if exc.errno == errno.ENOSPC else (exc.strerror or str(exc))
        print(reason, file=sys.stderr)
        return EXIT_FAILED
