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
import errno
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
ACTIVITY_FILE = "activity.json"
LOG_FILE = "owalky.log"
PID_FILE = "daemon.pid"
SOCKET_FILE = "daemon.sock"
CONFIG_FILE = "config.json"
#: Where a restore keeps the activity it is about to replace.
BACKUP_DIR_NAME = "backups"

MAX_STATE_BYTES = 64 * 1024
MAX_ACTIVITY_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 8 * 1024
#: Document limits shared by the state and activity documents.
MAX_METERS = 10_000_000
MAX_SESSION_SECONDS = 1_000_000
MAX_CALORIES = 1_000_000
MAX_LOG_BYTES = 256 * 1024
MAX_LOG_KEEP_BYTES = 64 * 1024
MAX_LOG_TAIL_BYTES = 8 * 1024
MAX_LOG_TAIL_LINES = 200
MAX_SMALL_FILE_BYTES = 4096
#: A chosen path is a path, but it is still a byte budget.
MAX_PATH_LEN = 4096
MAX_PATH_DEPTH = 40
MAX_LINK_HOPS = 8

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


def open_dir(parts: list[str], keep_dirs: tuple[str, ...] = ()) -> int:
    """Walk ``parts`` from the home directory and return a directory descriptor.

    Missing components are created 0700. The leaf is tightened and swept on every
    call rather than only when it looks wrong: a directory that was once wider can
    hold entries this plugin did not put there.

    ``keep_dirs`` names subdirectories that are part of the plugin's own layout.
    They are kept and tightened to 0700 instead of being refused as foreign.
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
                sweep_dir(fd, keep_dirs)
        return fd
    except BaseException:
        os.close(fd)
        raise


def sweep_dir(dir_fd: int, keep_dirs: tuple[str, ...] = ()) -> None:
    """Drop entries in the plugin's own directory that it did not create.

    Symlinks and fifos are removed, regular files are forced back to 0600, and a
    directory or a foreign-owned entry raises instead of being deleted. The one
    exception is a directory named in ``keep_dirs`` and owned by us, which is part
    of the plugin's layout and is tightened instead.
    """
    for name in os.listdir(dir_fd):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if info.st_uid != os.geteuid():
            raise StorageError(f"refusing foreign entry: {name}")
        if stat.S_ISDIR(info.st_mode):
            if name in keep_dirs:
                os.chmod(name, 0o700, dir_fd=dir_fd, follow_symlinks=False)
                continue
            raise StorageError(f"unexpected directory in plugin state: {name}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            os.unlink(name, dir_fd=dir_fd)
            continue
        os.chmod(name, 0o600, dir_fd=dir_fd, follow_symlinks=False)


@contextlib.contextmanager
def state_dir() -> Iterator[int]:
    """Open the state directory for the duration of the block."""
    fd = open_dir(state_components(), keep_dirs=(BACKUP_DIR_NAME,))
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def snapshot_dir() -> Iterator[int]:
    """Open the folder a restore keeps the replaced activity in.

    Same rules as the state directory above it, with the leaf created on demand
    and swept on every call.
    """
    fd = open_dir([*state_components(), BACKUP_DIR_NAME])
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


def read_chosen_file(dir_fd: int, name: str, limit: int) -> bytes | None:
    """Read a file the user picked in their own dialog.

    Unlike :func:`read_file`, the mode is not required to be 0600: a backup may
    have come from a download or a USB stick. It still has to be a regular file
    of ours with one link, within ``limit`` bytes, opened without following a
    symlink at the leaf.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError(f"refusing to read {name}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise StorageError(f"refusing {name}: expected a plain file of yours")
        if info.st_size > limit:
            raise StorageError(f"refusing {name}: larger than {limit} bytes")
        os.set_blocking(fd, True)
        return _read_at_most(fd, limit, name)
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


# -- a path the user picked in a file dialog ---------------------------------
#
# This is the one path that is not under the plugin's state directory. It gets
# its own walk: from the root, one component at a time with held descriptors,
# refusing anything another account can write to. A symlinked component is
# resolved by hand, against the directory that holds the link, and every
# component of the result is checked again, so a link cannot shorten the walk.


def _plain_component(name: object) -> bool:
    """A name that can only mean one entry in a directory.

    Deliberately more permissive than :data:`_COMPONENT`: a folder called
    ``My Documents`` is ordinary, and the name is only ever passed to ``open``
    with a directory descriptor, never joined into a path string.
    """
    return (
        isinstance(name, str)
        and 0 < len(name) <= 255
        and name not in (".", "..")
        and "/" not in name
        and "\0" not in name
    )


def _link_components(base: list[str], target: str) -> list[str]:
    """The components a symlink target stands for, applied to what has been
    walked already. ``..`` pops the way the kernel pops it, and a target that
    climbs past the root is refused rather than clamped to it."""
    out = list(base)
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise StorageError("link target climbs above the root")
            out.pop()
            continue
        if not _plain_component(part):
            raise StorageError("refusing a link in the chosen path")
        out.append(part)
    return out


def _check_chosen_dir(dir_fd: int, name: str) -> None:
    """A directory on the way to a chosen path.

    It has to be a real directory, owned by us or by root for the system ones
    above the home, and not writable by another account: that is what would let
    something redirect the write sideways. The sticky bit is the exception,
    which is what makes /tmp the one shared directory that is safe to write
    into.
    """
    info = os.fstat(dir_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise StorageError(f"not a directory: {name}")
    if info.st_uid not in (0, os.geteuid()):
        raise StorageError(f"not yours: {name}")
    if info.st_mode & 0o022 and not info.st_mode & 0o1000:
        raise StorageError(f"{name} is writable by other accounts")


def open_chosen_dir(parts: list[str]) -> int:
    """Directory descriptor for the folder a chosen path names.

    Walked one component at a time from the root with held descriptors. A
    symlinked component is resolved here, unlike in the state chain, because
    this path came out of the user's own file dialog and a Documents directory
    symlinked onto a mounted disk is ordinary. The resolution is bounded, and
    the target is checked like any other component, so the walk cannot be led
    into a directory another account can write to.
    """
    if not parts or len(parts) > MAX_PATH_DEPTH or not all(_plain_component(part) for part in parts):
        raise StorageError("refusing directory chain")
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = os.dup(root_fd)
    try:
        pending = list(parts)
        # The components opened so far, from the root. A relative symlink
        # target is applied to these, not to nothing.
        walked: list[str] = []
        hops = 0
        while pending:
            name = pending.pop(0)
            try:
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError as exc:
                # A symlinked component fails here as ENOTDIR (with O_DIRECTORY)
                # or ELOOP (without). Anything else, and anything that turns out
                # not to be a link once looked at, is a real error: a plain file
                # where a directory belongs, a name that is not there at all.
                if exc.errno not in (errno.ENOTDIR, errno.ELOOP):
                    raise
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    raise exc from None
                if not stat.S_ISLNK(info.st_mode):
                    if exc.errno == errno.ENOTDIR:
                        raise StorageError(f"not a directory: {name}") from None
                    raise
                hops += 1
                if hops > MAX_LINK_HOPS:
                    raise StorageError("too many links in the chosen path") from None
                target = os.readlink(name, dir_fd=fd)
                if not target or len(target) > MAX_PATH_LEN or "\0" in target:
                    raise StorageError("refusing a link in the chosen path") from None
                # `pending` already dropped `name`, so the target replaces it and
                # whatever was queued behind it follows the link. The walk then
                # restarts from the root, so every component of the target is
                # checked again.
                if target.startswith("/"):
                    pending = _link_components([], target) + pending
                else:
                    pending = _link_components(walked, target) + pending
                if len(pending) > MAX_PATH_DEPTH:
                    raise StorageError("chosen path too deep") from None
                os.close(fd)
                fd = os.dup(root_fd)
                walked = []
                continue
            os.close(fd)
            fd = nfd
            _check_chosen_dir(fd, name)
            walked.append(name)
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        os.close(root_fd)


def chosen_path(path: object) -> tuple[int, str]:
    """The directory descriptor and file name for a path the panel hands over.

    The caller owns the descriptor. The leaf is a plain component: no
    separator, no dot, nothing to traverse into.
    """
    if (not isinstance(path, str) or not path.startswith("/") or "\0" in path
            or len(path) > MAX_PATH_LEN):
        raise StorageError("refusing chosen path")
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise StorageError("refusing chosen path")
    name = parts.pop()
    if not _plain_component(name):
        raise StorageError("refusing chosen file name")
    return open_chosen_dir(parts), name


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
