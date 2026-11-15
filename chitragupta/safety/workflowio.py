"""Durable workflow files and OS-held locks, restricted to the data home's workflow tree.

No age-based lock stealing: a paused/live writer keeps its lock; process death releases it.
The lock file is never deleted (deleting it would permit two locks on different inodes).
"""
import contextlib
import json
import os
import threading
import time
import uuid

from .. import common
from . import paths

_mutex = threading.Lock()
BUSY = "Not saved: workflow storage is busy; your draft is kept and nothing was sent. Retry shortly."


def _check(path):
    paths.check(path, caller="workflow")
    root = os.path.realpath(common.hp("data", "workflows"))
    if not common.path_within(os.path.realpath(path), root):
        raise ValueError("workflow path outside workflow storage")
    return path


@contextlib.contextmanager
def lock(timeout=5.0):
    """One short workflow transaction across threads/processes; never across model work."""
    end = time.monotonic() + timeout
    if not _mutex.acquire(timeout=timeout):
        raise TimeoutError(BUSY)
    stream = None
    acquired = False
    try:
        path = _check(common.hp("data", "workflows", ".writer.lock"))
        paths.makedirs(os.path.dirname(path), caller="workflow")
        stream = open(path, "a+b")
        if os.path.getsize(path) == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= end:
                    raise TimeoutError(BUSY)
                time.sleep(0.02)
        yield
    finally:
        if stream is not None:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        _mutex.release()


def durable_json(path, value):
    """Flush payload before atomic replace; flush parent on POSIX. Caller holds lock.

    Windows flushes file buffers; directory fsync is unavailable there. This is crash
    consistency, not a guarantee against hardware which ignores flush requests.
    """
    path = _check(path)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("workflow record exceeds 8 MiB")
    paths.makedirs(os.path.dirname(path), caller="workflow")
    temp = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(_check(temp), "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        paths.replace(temp, path, caller="workflow")
        if os.name != "nt":
            fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(temp):
            paths.delete(temp, caller="workflow")


def move(src, dst):
    """Quarantine/repair move inside the workflow tree only; never deletes evidence. Caller holds lock."""
    _check(src)
    _check(dst)
    paths.makedirs(os.path.dirname(dst), caller="workflow")
    paths.replace(src, dst, caller="workflow recovery")
