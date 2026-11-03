from unittest import mock

import pytest

from tbhprint import api as apimod
from tbhprint import config as cfgmod
from tbhprint import control
from tbhprint import update as updatemod
from tbhprint.daemon import build


def _daemon(tmp_path):
    config_path = str(tmp_path / "config.json")
    daemon, store, pipeline = build(config_path, state_dir=str(tmp_path / "state"), dry_run=True)
    return daemon, store, pipeline, config_path


# -- Daemon.pair() -------------------------------------------------------------

def test_daemon_pair_saves_config_and_reloads_transports(tmp_path):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    try:
        assert not daemon.cfg.server.is_paired
        pairing_response = {
            "agent_uuid": "u-1", "token": "tok-1", "name": "Front desk", "tenant": "shop",
            "channel": "private-tenant.shop.print-agent.u-1",
            "reverb": {"key": "k", "host": "ws.techbenchhub.co.uk", "port": 443, "scheme": "https"},
        }
        with mock.patch("tbhprint.daemon.apimod.pair", return_value=pairing_response) as fake_pair, \
             mock.patch.object(daemon, "restart_transports") as fake_restart:
            redacted = daemon.pair("shop.techbenchhub.co.uk", "abcd2345", "Front desk")
        fake_pair.assert_called_once()
        assert fake_pair.call_args.args[0] == "https://shop.techbenchhub.co.uk"
        fake_restart.assert_called_once()
        assert daemon.cfg.server.is_paired
        assert daemon.cfg.server.agent_uuid == "u-1"
        assert redacted["server"]["token"] == "*" * 8
        # And it was actually saved to disk.
        reloaded = cfgmod.load(config_path)
        assert reloaded.server.agent_uuid == "u-1"
    finally:
        store.close()


def test_daemon_pair_defaults_url_scheme_and_name(tmp_path):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    try:
        pairing_response = {"agent_uuid": "u-2", "token": "tok-2"}
        with mock.patch("tbhprint.daemon.apimod.pair", return_value=pairing_response) as fake_pair, \
             mock.patch.object(daemon, "restart_transports"):
            daemon.pair("shop.techbenchhub.co.uk", "abcd2345", None)
        assert fake_pair.call_args.args[0] == "https://shop.techbenchhub.co.uk"
        assert daemon.cfg.server.agent_name  # fell back to the machine name, never blank
    finally:
        store.close()


def test_daemon_pair_propagates_api_error(tmp_path):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    try:
        with mock.patch("tbhprint.daemon.apimod.pair", side_effect=apimod.ApiError("pair: expired code", 422, retryable=False)):
            with pytest.raises(apimod.ApiError, match="expired"):
                daemon.pair("shop.techbenchhub.co.uk", "expired1", None)
        assert not daemon.cfg.server.is_paired  # nothing was saved
    finally:
        store.close()


# -- control.Dispatcher wiring for "pair" and "update" ---------------------------

def test_dispatcher_pair_requires_url_and_code():
    dispatcher = control.Dispatcher(daemon=mock.Mock())
    resp = dispatcher.handle({"cmd": "pair", "code": "abcd2345"})
    assert resp["ok"] is False and "url" in resp["error"]


def test_dispatcher_pair_calls_daemon_pair():
    fake_daemon = mock.Mock()
    fake_daemon.pair.return_value = {"server": {"agent_name": "Front desk"}}
    dispatcher = control.Dispatcher(daemon=fake_daemon)
    resp = dispatcher.handle({"cmd": "pair", "url": "https://shop.example.com", "code": "abcd2345", "name": "Front desk"})
    assert resp == {"ok": True, "data": {"server": {"agent_name": "Front desk"}}}
    fake_daemon.pair.assert_called_once_with("https://shop.example.com", "abcd2345", "Front desk")


def test_dispatcher_update_passes_check_only():
    fake_daemon = mock.Mock()
    fake_daemon.check_for_update.return_value = {"version": None}
    dispatcher = control.Dispatcher(daemon=fake_daemon)
    resp = dispatcher.handle({"cmd": "update", "check_only": True})
    assert resp == {"ok": True, "data": {"version": None}}
    fake_daemon.check_for_update.assert_called_once_with(check_only=True)


# -- Daemon.check_for_update() ---------------------------------------------------

def test_check_for_update_returns_none_when_unpaired(tmp_path):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    try:
        assert daemon.client is None
        assert daemon.check_for_update() is None
    finally:
        store.close()


def test_check_for_update_check_only_uses_update_check(tmp_path):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    try:
        daemon.client = mock.Mock()
        manifest = updatemod.UpdateManifest(version="1.2.3", url="https://x/a", sha256="a", notes="notes")
        with mock.patch("tbhprint.daemon.updatemod.check", return_value=manifest) as fake_check, \
             mock.patch("tbhprint.daemon.updatemod.check_and_install") as fake_install:
            result = daemon.check_for_update(check_only=True)
        fake_check.assert_called_once()
        fake_install.assert_not_called()
        assert result == {"version": "1.2.3", "notes": "notes"}
    finally:
        store.close()


def test_check_for_update_full_cycle_uses_check_and_install(tmp_path):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    try:
        daemon.client = mock.Mock()
        manifest = updatemod.UpdateManifest(version="1.2.3", url="https://x/a", sha256="a")
        with mock.patch("tbhprint.daemon.updatemod.check_and_install", return_value=manifest) as fake_install:
            result = daemon.check_for_update(check_only=False)
        fake_install.assert_called_once()
        assert fake_install.call_args.kwargs["linux_update_dir"] == daemon.cfg.update.dir
        assert result == {"version": "1.2.3", "notes": ""}
    finally:
        store.close()


# -- 0.1.10: websocket details from every sweep, and the transport header (owner 2026-11-03) ------

OLD_REVERB = {"key": "droplet-key", "host": "ws.techbenchhub.co.uk", "port": 443, "scheme": "https"}
NEW_REVERB = {"key": "cloud-key", "host": "ws-abc.laravel.cloud", "port": 443, "scheme": "https"}
CHANNEL = "private-tenant.shop.print-agent.u-1"


class FakeReverb:
    """Stands in for ReverbTransport: records how the daemon built, started and stopped it."""

    def __init__(self, ws_url, channel, auth_provider, on_job, on_connect, on_state):
        self.ws_url, self.channel = ws_url, channel
        self.on_connect, self.on_state = on_connect, on_state
        self.started = self.stopped = self.connected = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.connected = False


def _paired_daemon(tmp_path, reverb=OLD_REVERB, mode="auto"):
    daemon, store, pipeline, config_path = _daemon(tmp_path)
    daemon.cfg.server = cfgmod.Server(url="https://shop.techbenchhub.co.uk", token="tok-1", agent_uuid="u-1",
                                      agent_name="Front desk", tenant="shop", channel=CHANNEL,
                                      reverb=cfgmod.reverb_from_wire(reverb))
    daemon.cfg.transport.mode = mode
    daemon.client = daemon._new_client()
    daemon.poller = mock.Mock()
    if mode != "poll" and reverb.get("host") and reverb.get("key"):
        daemon.reverb = daemon._new_reverb()
        daemon.reverb.start()
    return daemon, store, config_path


@pytest.fixture
def fake_reverb():
    with mock.patch("tbhprint.daemon.ReverbTransport", FakeReverb):
        yield


def test_server_details_unchanged_do_nothing(tmp_path, fake_reverb):
    daemon, store, config_path = _paired_daemon(tmp_path)
    try:
        before = daemon.reverb
        with mock.patch("tbhprint.daemon.cfgmod.save") as fake_save:
            daemon._on_server_details(dict(OLD_REVERB), CHANNEL)
            daemon._on_server_details({"key": "droplet-key", "host": "ws.techbenchhub.co.uk"}, None)  # defaults
        fake_save.assert_not_called()
        assert daemon.reverb is before and not before.stopped
    finally:
        store.close()


def test_server_details_with_a_new_host_save_and_reconnect(tmp_path, fake_reverb, caplog):
    daemon, store, config_path = _paired_daemon(tmp_path)
    try:
        old = daemon.reverb
        old.connected = True
        daemon._on_reverb_connect(old)
        assert daemon._transport_header() == "reverb"
        with caplog.at_level("INFO", logger="tbhprint.daemon"):
            daemon._on_server_details(dict(NEW_REVERB), CHANNEL)
        assert old.stopped
        assert daemon.reverb is not old and daemon.reverb.started
        assert daemon.reverb.ws_url == "wss://ws-abc.laravel.cloud:443/app/cloud-key"
        assert daemon.reverb.channel == CHANNEL
        assert daemon.transport_name == "poller" and daemon._transport_header() == "poller"
        daemon.poller.set_mode.assert_called_with("fallback")
        saved = cfgmod.load(config_path)
        assert saved.server.reverb.host == "ws-abc.laravel.cloud" and saved.server.reverb.key == "cloud-key"
        assert daemon.cfg.server.token == "tok-1"                       # the in-memory token is untouched
        assert any("host ws.techbenchhub.co.uk -> ws-abc.laravel.cloud (key changed: yes" in r.getMessage()
                   for r in caplog.records)
        # The replaced socket can no longer move the daemon's state.
        old.on_connect()
        old.on_state("connected")
        assert daemon.transport_name == "poller"
        # A second sweep with the same block is the hot path again.
        current = daemon.reverb
        with mock.patch("tbhprint.daemon.cfgmod.save") as fake_save:
            daemon._on_server_details(dict(NEW_REVERB), CHANNEL)
        fake_save.assert_not_called()
        assert daemon.reverb is current
    finally:
        store.close()


def test_server_details_reach_an_agent_paired_without_them(tmp_path, fake_reverb):
    daemon, store, config_path = _paired_daemon(tmp_path, reverb={})
    try:
        assert daemon.reverb is None
        daemon._on_server_details(dict(NEW_REVERB), CHANNEL)
        assert daemon.reverb is not None and daemon.reverb.started
        assert daemon.reverb.ws_url == "wss://ws-abc.laravel.cloud:443/app/cloud-key"
        assert cfgmod.load(config_path).server.reverb.host == "ws-abc.laravel.cloud"
    finally:
        store.close()


def test_server_details_with_a_new_channel_alone_save_and_reconnect(tmp_path, fake_reverb):
    daemon, store, config_path = _paired_daemon(tmp_path)
    try:
        old = daemon.reverb
        daemon._on_server_details(dict(OLD_REVERB), "private-tenant.shop.print-agent.u-2")
        assert old.stopped and daemon.reverb is not old and daemon.reverb.started
        assert daemon.reverb.channel == "private-tenant.shop.print-agent.u-2"
        assert cfgmod.load(config_path).server.channel == "private-tenant.shop.print-agent.u-2"
    finally:
        store.close()


def test_server_details_in_poll_mode_are_saved_without_a_websocket(tmp_path, fake_reverb):
    daemon, store, config_path = _paired_daemon(tmp_path, mode="poll")
    try:
        daemon._on_server_details(dict(NEW_REVERB), CHANNEL)
        assert daemon.reverb is None
        assert cfgmod.load(config_path).server.reverb.host == "ws-abc.laravel.cloud"
    finally:
        store.close()


def test_transport_header_follows_the_websocket(tmp_path, fake_reverb):
    daemon, store, config_path = _paired_daemon(tmp_path)
    try:
        header = apimod.TRANSPORT_HEADER
        assert daemon.client._headers()[header] == "poller"          # socket not up yet
        daemon.reverb.connected = True
        daemon.reverb.on_connect()                                    # as the transport calls it
        assert daemon.client._headers()[header] == "reverb"
        assert daemon.status()["transport"] == "reverb"
        daemon.reverb.connected = False
        daemon._on_transport_state("disconnected")
        assert daemon.client._headers()[header] == "poller"
        assert daemon.status()["transport"] == "poller"
        # And a stale name never outruns a dead socket: connected=False alone already reads poller.
        daemon.transport_name = "reverb"
        assert daemon.client._headers()[header] == "poller"
    finally:
        store.close()
