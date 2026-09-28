"""Where the plugin keeps its files, and the rules for touching them.

Paths come from the password database, not from ``$HOME`` or the XDG variables,
which any same-user process can set. Each component of a chain is opened
``O_NOFOLLOW`` and checked for type and ownership before the next is resolved, so a
symlink in the chain stops the walk instead of redirecting it.

Reads use one descriptor, validated before any byte is read from it:
``O_NOFOLLOW`` rejects a symlink, ``O_NONBLOCK`` keeps a fifo from hanging the
caller, ``fstat`` bounds the size, and the read stops at the limit. Writes go to an
exclusive ``0600`` temporary that is renamed into place, which replaces a symlink
at the destination instead of writing through it.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import re
import secrets
import stat
import time
from collections.abc import Iterator

STATE_DIR_NAME = "owalky"
CONFIG_DIR_NAME = "owalky"

STATE_FILE = "state.json"
LOG_FILE = "owalky.log"
PID_FILE = "daemon.pid"
SOCKET_FILE = "daemon.sock"
CONFIG_FILE = "config.json"

MAX_STATE_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 8 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_LOG_KEEP_BYTES = 64 * 1024
MAX_LOG_TAIL_BYTES = 8 * 1024
MAX_LOG_TAIL_LINES = 200
MAX_SMALL_FILE_BYTES = 4096

_COMPONENT = re.compile(r"\A[A-Za-z0-9._-]+\Z")


class StorageError(Exception):
    """A file or directory was refused, or could not be used safely."""


def home_dir() -> str:
    """The user's home directory, according to the password database."""
    return pwd.getpwuid(os.geteuid()).pw_dir


def dir_components(variable: str, parent: tuple[str, ...], leaf: str) -> list[str]:
    """Components of an XDG directory, relative to the home directory.

    The variable is honoured only when it points inside the home directory, so
    :func:`open_dir` can always start from the passwd entry.

    ``leaf`` is always appended: the variable names the parent, and the plugin's
    own directory is the only one this code may create, tighten or sweep.
    """
    prefix = home_dir().rstrip("/") + "/"
    configured = os.environ.get(variable, "")
    if configured and os.path.isabs(configured):
        candidate = os.path.normpath(configured)
        if candidate.startswith(prefix):
            parts = [part for part in candidate[len(prefix) :].split("/") if part]
            if parts:
                return parts + [leaf]
    return [*parent, leaf]


def state_components() -> list[str]:
    """Components of the state directory, ``~/.local/state/owalky`` by default."""
    return dir_components("XDG_STATE_HOME", (".local", "state"), STATE_DIR_NAME)


def config_components() -> list[str]:
    """Components of the configuration directory, ``~/.config/owalky``."""
    return dir_components("XDG_CONFIG_HOME", (".config",), CONFIG_DIR_NAME)


def state_path() -> str:
    """Absolute path of the state directory, for display and for bind()."""
    return os.path.join(home_dir(), *state_components())


def config_path() -> str:
    """Absolute path of the configuration directory."""
    return os.path.join(home_dir(), *config_components())


def log_path() -> str:
    """Absolute path of the debug log."""
    return os.path.join(state_path(), LOG_FILE)


def open_dir(parts: list[str]) -> int:
    """Walk ``parts`` from the home directory and return a directory descriptor.

    Missing components are created 0700. The leaf is tightened and swept on every
    call rather than only when it looks wrong: a directory that was once wider can
    hold entries this plugin did not put there.
    """
    if not parts or not all(_COMPONENT.match(part) and part not in (".", "..") for part in parts):
        raise StorageError("refusing directory chain")
    fd = os.open(home_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, name in enumerate(parts):
            leaf = index == len(parts) - 1
            try:
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=fd)
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise StorageError(f"refusing directory component: {name}")
            if leaf:
                os.fchmod(fd, 0o700)
                sweep_dir(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def sweep_dir(dir_fd: int) -> None:
    """Drop entries in the plugin's own directory that it did not create.

    Symlinks and fifos are removed, regular files are forced back to 0600, and a
    directory or a foreign-owned entry raises instead of being deleted.
    """
    for name in os.listdir(dir_fd):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if info.st_uid != os.geteuid():
            raise StorageError(f"refusing foreign entry: {name}")
        if stat.S_ISDIR(info.st_mode):
            raise StorageError(f"unexpected directory in plugin state: {name}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            os.unlink(name, dir_fd=dir_fd)
            continue
        os.chmod(name, 0o600, dir_fd=dir_fd, follow_symlinks=False)


@contextlib.contextmanager
def state_dir() -> Iterator[int]:
    """Open the state directory for the duration of the block."""
    fd = open_dir(state_components())
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def config_dir() -> Iterator[int]:
    """Open the configuration directory for the duration of the block."""
    fd = open_dir(config_components())
    try:
        yield fd
    finally:
        os.close(fd)


def read_file(dir_fd: int, name: str, limit: int) -> bytes | None:
    """Read a plugin-owned file, or return ``None`` if it does not exist.

    A refused file raises rather than reading as empty, so a caller cannot mistake
    a symlink or an oversized file for a document that has not been written yet.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError(f"refusing to read {name}: {exc.strerror}") from exc
    try:
        _require_own_regular_file(fd, name)
        info = os.fstat(fd)
        if info.st_size > limit:
            raise StorageError(f"refusing {name}: larger than {limit} bytes")
        os.set_blocking(fd, True)
        return _read_at_most(fd, limit, name)
    finally:
        os.close(fd)


def read_file_tail(dir_fd: int, name: str, limit: int, lines: int) -> str:
    """Return the last ``limit`` bytes of a plugin-owned file as text.

    The read starts at ``size - limit``, so the cost does not depend on the file
    size, and the result is capped again at ``lines`` lines.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise StorageError(f"refusing to read {name}: {exc.strerror}") from exc
    try:
        _require_own_regular_file(fd, name)
        os.set_blocking(fd, True)
        os.lseek(fd, max(0, os.fstat(fd).st_size - limit), os.SEEK_SET)
        text = _read_at_most(fd, limit, name).decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])
    finally:
        os.close(fd)


def write_file(dir_fd: int, name: str, data: bytes, limit: int) -> None:
    """Publish ``data`` as ``name`` through an exclusive temporary.

    The temporary is created ``O_EXCL`` at 0600, written and fsynced through that
    descriptor, then renamed into place and the directory fsynced, so a file that
    reports "saved" survives a crash.
    """
    if len(data) > limit:
        raise StorageError(f"refusing to write {name}: larger than {limit} bytes")
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=dir_fd)
        raise
    finally:
        os.close(fd)


def remove_file(dir_fd: int, name: str) -> None:
    """Unlink ``name`` if it is there. Absence is not an error."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dir_fd)


def log_append(dir_fd: int, message: str) -> None:
    """Append one timestamped line to the debug log, rotating it when oversized.

    Past :data:`MAX_LOG_BYTES` only the most recent :data:`MAX_LOG_KEEP_BYTES`
    survive. This never raises: a pad that cannot be logged still has to work.
    """
    try:
        fd = os.open(
            LOG_FILE,
            os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            return
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o600)
        if info.st_size > MAX_LOG_BYTES:
            os.lseek(fd, info.st_size - MAX_LOG_KEEP_BYTES, os.SEEK_SET)
            kept = os.read(fd, MAX_LOG_KEEP_BYTES)
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, kept)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n"
        os.write(fd, line.encode("utf-8", "replace")[:4096])
    except OSError:
        pass
    finally:
        os.close(fd)


def _require_own_regular_file(fd: int, name: str) -> None:
    """Refuse anything that is not an owner-only regular file with one link."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise StorageError(f"refusing {name}: expected an owner-only regular file")


def _read_at_most(fd: int, limit: int, name: str) -> bytes:
    """Read up to ``limit`` bytes, and refuse rather than truncate an overflow."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise StorageError(f"refusing {name}: grew past {limit} bytes")
    return data
