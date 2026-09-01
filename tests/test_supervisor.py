import threading
import time

from tbhprint import supervisor as supervisormod


class FakeChild:
    """A fake subprocess.Popen: starts "alive" (poll() -> None) until
    terminate()/kill() is called, or the test flips it dead itself."""

    def __init__(self, pid=1):
        self.pid = pid
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def die(self):
        self._alive = False

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


def test_restarts_on_child_exit_with_backoff():
    children = []
    spawned = threading.Event()

    def spawn():
        child = FakeChild(pid=len(children) + 1)
        children.append(child)
        spawned.set()
        return child

    sup = supervisormod.Supervisor(spawn, check_alive=lambda: True,
                                   backoff_start=0.01, backoff_cap=0.02,
                                   watchdog_interval=1000, watchdog_timeout=1000)
    sup.start()
    try:
        assert spawned.wait(timeout=2)
        first = children[0]
        first.die()  # simulate a crash
        # Wait for a restart (a second child spawned).
        deadline = time.monotonic() + 2
        while len(children) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(children) >= 2
    finally:
        sup.stop()


def test_stop_kills_the_running_child():
    child = FakeChild()
    sup = supervisormod.Supervisor(lambda: child, check_alive=lambda: True,
                                   backoff_start=0.01, backoff_cap=0.02)
    sup.start()
    deadline = time.monotonic() + 2
    while sup.child is None and time.monotonic() < deadline:
        time.sleep(0.01)
    sup.stop()
    assert child.terminated


def test_watchdog_kills_and_restarts_when_status_stops_answering():
    children = []

    def spawn():
        child = FakeChild(pid=len(children) + 1)
        children.append(child)
        return child

    answered = {"value": True}

    sup = supervisormod.Supervisor(spawn, check_alive=lambda: answered["value"],
                                   backoff_start=0.01, backoff_cap=0.02,
                                   watchdog_interval=0.02, watchdog_timeout=0.05)
    sup.start()
    try:
        deadline = time.monotonic() + 2
        while not children and time.monotonic() < deadline:
            time.sleep(0.01)
        assert children
        # Now the agent stops answering `status` (wedged, but the process
        # itself never exits) - the watchdog should kill it, which the
        # restart loop then notices and replaces.
        answered["value"] = False
        deadline = time.monotonic() + 2
        while not children[0].terminated and time.monotonic() < deadline:
            time.sleep(0.02)
        assert children[0].terminated
        deadline = time.monotonic() + 2
        while len(children) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(children) >= 2
    finally:
        sup.stop()


def test_watchdog_does_not_kill_a_healthy_child():
    child = FakeChild()
    sup = supervisormod.Supervisor(lambda: child, check_alive=lambda: True,
                                   backoff_start=0.01, backoff_cap=0.02,
                                   watchdog_interval=0.02, watchdog_timeout=0.05)
    sup.start()
    try:
        time.sleep(0.3)
        assert not child.terminated
    finally:
        sup.stop()


def test_rotate_log_if_large(tmp_path):
    path = tmp_path / "tbhprint.log"
    path.write_bytes(b"x" * 100)
    supervisormod.rotate_log_if_large(str(path), max_bytes=1000)
    assert path.exists()  # untouched: below the threshold

    path.write_bytes(b"y" * 5000)
    supervisormod.rotate_log_if_large(str(path), max_bytes=1000, backups=3)
    assert not path.exists()
    assert (tmp_path / "tbhprint.log.1").exists()
    assert (tmp_path / "tbhprint.log.1").read_bytes() == b"y" * 5000

    # A second rotation should push .1 -> .2 and start a fresh .1.
    path.write_bytes(b"z" * 5000)
    supervisormod.rotate_log_if_large(str(path), max_bytes=1000, backups=3)
    assert (tmp_path / "tbhprint.log.2").read_bytes() == b"y" * 5000
    assert (tmp_path / "tbhprint.log.1").read_bytes() == b"z" * 5000
