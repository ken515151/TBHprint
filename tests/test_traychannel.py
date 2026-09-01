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
