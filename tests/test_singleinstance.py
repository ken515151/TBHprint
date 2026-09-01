import sys
import uuid

import pytest

from tbhprint import singleinstance


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows mutex path")
def test_windows_mutex_second_acquire_raises_already_running():
    name = f"Local\\TBHprint-test-{uuid.uuid4().hex}"
    first = singleinstance.SingleInstanceLock(name)
    second = singleinstance.SingleInstanceLock(name)
    first.acquire()
    try:
        with pytest.raises(singleinstance.AlreadyRunning):
            second.acquire()
    finally:
        first.release()
    # Released: a third lock can now take the same name.
    third = singleinstance.SingleInstanceLock(name)
    third.acquire()
    third.release()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows mutex path")
def test_windows_mutex_context_manager_releases_on_exit():
    name = f"Local\\TBHprint-test-{uuid.uuid4().hex}"
    with singleinstance.SingleInstanceLock(name):
        inner = singleinstance.SingleInstanceLock(name)
        with pytest.raises(singleinstance.AlreadyRunning):
            inner.acquire()
    # Context manager released it on exit.
    again = singleinstance.SingleInstanceLock(name)
    again.acquire()
    again.release()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock path")
def test_posix_flock_second_acquire_raises_already_running(tmp_path):
    path = str(tmp_path / "agent.lock")
    first = singleinstance.SingleInstanceLock("ignored-on-posix", lock_path=path)
    second = singleinstance.SingleInstanceLock("ignored-on-posix", lock_path=path)
    first.acquire()
    try:
        with pytest.raises(singleinstance.AlreadyRunning):
            second.acquire()
    finally:
        first.release()
    third = singleinstance.SingleInstanceLock("ignored-on-posix", lock_path=path)
    third.acquire()
    third.release()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock path")
def test_posix_flock_requires_lock_path():
    lock = singleinstance.SingleInstanceLock("ignored-on-posix", lock_path=None)
    with pytest.raises(ValueError):
        lock.acquire()
