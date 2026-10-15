"""Files written whole (atomic, fsync'd) and held by one writer at a time."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError, AttributeError):   # durable rename where the OS allows
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def replace_file(path, write: Callable[[str], None]) -> None:
    """Write ``path`` through a unique temp file beside it, fsync it, then rename it over.

    A reader sees the old file or the new one, never a partial write. ``write``
    receives the temp path and creates the file. On any error the temp file is
    removed and the old file is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        write(temp)
        with open(temp, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    _fsync_dir(path.parent)


def write_text_atomic(path, text: str) -> None:
    def _write(temp: str) -> None:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)

    replace_file(path, _write)


def write_bytes_atomic(path, data: bytes) -> None:
    def _write(temp: str) -> None:
        with open(temp, "wb") as handle:
            handle.write(data)

    replace_file(path, _write)


@contextmanager
def locked(path, wait: bool = True) -> Iterator[bool]:
    """Hold ``path`` for one writer at a time, across threads and processes.

    The lock is taken on ``<path>.lock``. Yields whether it is held: with
    ``wait=False`` a lock someone else holds is not waited for.
    """
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        hold = _hold_windows if os.name == "nt" else _hold_posix
        with hold(handle, wait) as acquired:
            yield acquired


@contextmanager
def _hold_posix(handle, wait: bool) -> Iterator[bool]:
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _hold_windows(handle, wait: bool) -> Iterator[bool]:  # pragma: no cover - Windows only
    import msvcrt

    handle.seek(0)
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            if not wait and exc.errno in (errno.EACCES, errno.EDEADLOCK):
                yield False
                return
            if exc.errno != errno.EDEADLOCK:
                raise
    try:
        yield True
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
