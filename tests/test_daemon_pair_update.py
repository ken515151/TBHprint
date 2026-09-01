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
