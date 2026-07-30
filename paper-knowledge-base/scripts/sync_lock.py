"""跨进程同步锁。

提供 `SyncProcessLock` 上下文管理器，确保同一时刻只有一个 Zotero 同步进程在运行。
支持 Windows (msvcrt) 与 POSIX (fcntl) 平台。
"""

from __future__ import annotations

import errno
import logging
import os
import time
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger("sync_zotero")


class SyncProcessLock:
    """Cross-platform advisory lock that serializes all sync processes."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    @staticmethod
    def _try_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, *, blocking: bool = True, poll_seconds: float = 1.0) -> bool:
        if self._handle is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b", buffering=0)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")

        waiting_logged = False
        while True:
            try:
                self._try_lock(handle)
                self._handle = handle
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    handle.close()
                    raise
                if not blocking:
                    handle.close()
                    return False
                if not waiting_logged:
                    logger.info("另一个 Zotero 同步正在运行，等待其完成...")
                    waiting_logged = True
                time.sleep(poll_seconds)

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "SyncProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
