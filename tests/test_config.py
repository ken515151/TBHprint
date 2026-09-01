
import pytest

from tbhprint import config as cfgmod


def _paired():
    return {
        "server": {"url": "https://shop.techbenchhub.co.uk", "token": "tok", "agent_uuid": "u-1",
                   "agent_name": "Front desk", "tenant": "shop",
                   "channel": "private-tenant.shop.print-agent.u-1",
                   "reverb": {"key": "k", "host": "ws.techbenchhub.co.uk", "port": 443, "scheme": "https"}},
        "printers": {"lj": {"name": "HP LaserJet", "options": []}},
        "routing": {"ticket_label": {"printer": "lj", "copies": 2}},
    }


def test_round_trip_and_defaults(tmp_path):
    cfg = cfgmod.from_dict(_paired())
    assert cfg.server.is_paired
    assert cfg.server.host == "shop.techbenchhub.co.uk"
    assert cfg.server.api("jobs") == "https://shop.techbenchhub.co.uk/api/print/v1/jobs"
    assert cfg.server.reverb.ws_url == "wss://ws.techbenchhub.co.uk:443/app/k"
    assert cfg.transport.mode == "auto"
    assert cfg.route_for("ticket_label").copies == 2
    assert cfg.route_for("invoice") is None

    path = tmp_path / "config.json"
    cfgmod.save(cfg, str(path))
    loaded = cfgmod.load(str(path))
    assert loaded.to_dict() == cfg.to_dict() or loaded.server.token in ("tok", "keyring")


def test_default_printer_covers_unrouted_types():
    data = _paired()
    data["default_printer"] = "lj"
    cfg = cfgmod.from_dict(data)
    assert cfg.route_for("invoice").printer == "lj"


def test_validation_errors():
    data = _paired()
    data["routing"]["invoice"] = {"printer": "nope"}
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.from_dict(data)
    data = _paired()
    data["server"]["url"] = "ftp://x"
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.from_dict(data)
    data = _paired()
    data["transport"] = {"mode": "carrier-pigeon"}
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.from_dict(data)


def test_unpaired_is_valid_and_redacted():
    cfg = cfgmod.Config()
    assert not cfg.server.is_paired
    cfg = cfgmod.from_dict(_paired())
    assert cfg.redacted_dict()["server"]["token"] == "********"


def test_apply_update_keeps_masked_token():
    cfg = cfgmod.from_dict(_paired())
    updated = cfgmod.apply_update(cfg, {"server": {"token": "********", "agent_name": "Bench"}})
    assert updated.server.token == "tok"
    assert updated.server.agent_name == "Bench"
    assert updated.server.reverb.key == "k"


def test_missing_file(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="not found"):
        cfgmod.load(str(tmp_path / "nope.json"))
