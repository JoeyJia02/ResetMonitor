"""Process-scoped file locks, automatically released when a process exits."""

import errno
import os
from pathlib import Path


class LockBusy(RuntimeError):
    """Another process already owns the monitor lock."""


class ProcessLock:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._file = None

    def acquire(self) -> "ProcessLock":
        if self._file is not None:
            raise RuntimeError("This ProcessLock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise LockBusy("Another monitor process holds the lock") from None
            raise
        except BaseException:
            handle.close()
            raise
        self._file = handle
        return self

    def release(self) -> None:
        if self._file is None:
            return
        handle, self._file = self._file, None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
