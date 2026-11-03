import pytest
from unittest import mock

from tbhprint import control, traychannel


class FakeApplet:
    def __init__(self):
        self.opened = []
        self.quit_requested = False

    def request_open(self, window):
        self.opened.append(window)

    def request_quit(self):
        self.quit_requested = True


def test_dispatcher_open_and_quit():
    applet = FakeApplet()
    dispatcher = traychannel.TrayDispatcher(applet)

    resp = dispatcher.handle({"cmd": "open", "window": "settings"})
    assert resp == {"ok": True, "data": {"opened": "settings"}}
    assert applet.opened == ["settings"]

    resp = dispatcher.handle({"cmd": "open"})  # defaults to "status"
    assert resp["data"]["opened"] == "status"

    resp = dispatcher.handle({"cmd": "quit"})
    assert resp == {"ok": True, "data": {"quitting": True}}
    assert applet.quit_requested is True


def test_dispatcher_rejects_unknown_command():
    dispatcher = traychannel.TrayDispatcher(FakeApplet())
    resp = dispatcher.handle({"cmd": "bogus"})
    assert resp["ok"] is False


def test_send_returns_false_when_nothing_listening():
    # An address nothing is bound to.
    with mock.patch("tbhprint.traychannel.default_address", return_value=("127.0.0.1", 47999)):
        assert traychannel.send("open", timeout=0.5, window="status") is False


def test_send_round_trips_through_a_real_control_server():
    applet = FakeApplet()
    server = control.ControlServer(traychannel.TrayDispatcher(applet), address=("127.0.0.1", 0))
    server.start()
    try:
        with mock.patch("tbhprint.traychannel.default_address", return_value=("127.0.0.1", server.port)):
            assert traychannel.send("open", window="log") is True
        assert applet.opened == ["log"]
    finally:
        server.stop()


def test_default_address_windows_vs_posix(monkeypatch):
    monkeypatch.setattr(traychannel.sys, "platform", "win32")
    assert traychannel.default_address() == traychannel.WINDOWS_ADDRESS

    monkeypatch.setattr(traychannel.sys, "platform", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    address = traychannel.default_address()
    assert address.endswith("tbhprint-tray.sock")
    assert address.startswith("/run/user/1000")


def test_control_client_never_resends_a_request_whose_reply_timed_out():
    """The daemon has the request and is still working on it; a re-send ran "update" twice (2026-11-03)."""
    import socket
    import threading

    from tbhprint import control

    received = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(2)

    def serve():
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(4096))
            threading.Event().wait(1.0)      # longer than the client's timeout; no reply

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = control.ControlClient(server.getsockname(), timeout=0.2)
    try:
        with pytest.raises(OSError):
            client.call("update")
    finally:
        client.close()
        thread.join(3)
        server.close()
    assert len(received) == 1
