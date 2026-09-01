"""Configuration owner for TBHprint.

One JSON file; the daemon is its single writer (the CLI edits it through
the control socket while the daemon runs, directly when it doesn't).

Locations (override with --config):
  Windows: %ProgramData%\\TBHprint\\config.json
  Linux:   /etc/tbhprint/config.json
  macOS:   /Library/Application Support/TBHprint/config.json

The bearer token lives in this file (0600 / ProgramData ACLs) unless the
optional `keyring` package is installed, in which case it is stored in the
OS credential store under service "tbhprint" and the file holds "keyring".
"""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

TRANSPORT_MODES = ("auto", "realtime", "poll")
DUPLEX_VALUES = ("off", "long-edge", "short-edge")
BACKENDS = ("auto", "cups", "windows")

KEYRING_SERVICE = "tbhprint"
KEYRING_MARKER = "keyring"


def _windows_base() -> str:
    """Per-user, never ProgramData: the installer is per-user (no admin,
    silent auto-updates), printers are per-user, and %LOCALAPPDATA% is what
    the uninstaller offers to clean up (DISTRIBUTION_DESIGN.md section 5)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "TBHprint")


def default_config_path() -> str:
    if sys.platform.startswith("win"):
        return os.path.join(_windows_base(), "config.json")
    if sys.platform == "darwin":
        return "/Library/Application Support/TBHprint/config.json"
    return "/etc/tbhprint/config.json"


def default_state_dir() -> str:
    if sys.platform.startswith("win"):
        return _windows_base()
    if sys.platform == "darwin":
        return "/Library/Application Support/TBHprint"
    return "/var/lib/tbhprint"


class ConfigError(ValueError):
    """Raised when the config file is missing required data or malformed."""


@dataclass
class Reverb:
    key: str = ""
    host: str = ""
    port: int = 443
    scheme: str = "https"

    @property
    def ws_url(self) -> str:
        proto = "wss" if self.scheme == "https" else "ws"
        return f"{proto}://{self.host}:{self.port}/app/{self.key}"


@dataclass
class Server:
    url: str = ""            # https://shop.techbenchhub.co.uk (no trailing slash)
    token: str = ""          # bearer token, or KEYRING_MARKER
    agent_uuid: str = ""
    agent_name: str = ""
    tenant: str = ""
    channel: str = ""        # private-tenant.<tenant>.print-agent.<uuid>
    reverb: Reverb = field(default_factory=Reverb)

    @property
    def is_paired(self) -> bool:
        return bool(self.url and self.token and self.agent_uuid)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    def api(self, path: str) -> str:
        return f"{self.url.rstrip('/')}/api/print/v1/{path.lstrip('/')}"


@dataclass
class Transport:
    mode: str = "auto"
    poll_interval_s: int = 60


@dataclass
class Printer:
    name: str                       # OS printer name (CUPS queue / Windows printer)
    options: list[str] = field(default_factory=list)   # backend-specific extras (lp -o ...)


@dataclass
class Route:
    printer: str
    enabled: bool = True
    copies: int | None = None       # None -> the job's copies
    duplex: str = "off"
    rotate: bool = False

    def lp_options(self) -> list[str]:
        opts = {
            "off": "sides=one-sided",
            "long-edge": "sides=two-sided-long-edge",
            "short-edge": "sides=two-sided-short-edge",
        }[self.duplex]
        out = [opts]
        if self.rotate:
            out.append("orientation-requested=4")
        return out


@dataclass
class Timeouts:
    download_s: int = 120
    print_submit_s: int = 60
    stuck_flag_s: int = 90


@dataclass
class Retention:
    spool_days: int = 7


@dataclass
class UpdateSettings:
    dir: str = "/var/lib/tbhprint/update"   # Linux only; Windows installs straight from the state dir


@dataclass
class Config:
    server: Server = field(default_factory=Server)
    transport: Transport = field(default_factory=Transport)
    backend: str = "auto"
    printers: dict[str, Printer] = field(default_factory=dict)
    routing: dict[str, Route] = field(default_factory=dict)
    default_printer: str | None = None
    timeouts: Timeouts = field(default_factory=Timeouts)
    retention: Retention = field(default_factory=Retention)
    update: UpdateSettings = field(default_factory=UpdateSettings)

    def route_for(self, document_type: str) -> Route | None:
        route = self.routing.get(document_type)
        if route is not None:
            return route
        if self.default_printer and self.default_printer in self.printers:
            return Route(printer=self.default_printer)
        return None

    def printer_for(self, route: Route) -> Printer | None:
        return self.printers.get(route.printer)

    def validate(self) -> None:
        if self.server.url:
            parts = urlsplit(self.server.url)
            if parts.scheme not in ("https", "http") or not parts.hostname:
                raise ConfigError(f"server.url must be an http(s) URL, got {self.server.url!r}")
        if self.transport.mode not in TRANSPORT_MODES:
            raise ConfigError(f"transport.mode must be one of {TRANSPORT_MODES}")
        if self.transport.poll_interval_s < 10:
            raise ConfigError("transport.poll_interval_s must be >= 10")
        if self.backend not in BACKENDS:
            raise ConfigError(f"backend must be one of {BACKENDS}")
        for key, printer in self.printers.items():
            if not printer.name:
                raise ConfigError(f"printers.{key}.name is required")
        for doc_type, route in self.routing.items():
            if route.printer not in self.printers:
                raise ConfigError(f"routing.{doc_type}.printer {route.printer!r} is not a configured printer")
            if route.copies is not None and not (1 <= route.copies <= 99):
                raise ConfigError(f"routing.{doc_type}.copies must be 1..99")
            if route.duplex not in DUPLEX_VALUES:
                raise ConfigError(f"routing.{doc_type}.duplex must be one of {DUPLEX_VALUES}")
        if self.default_printer is not None and self.default_printer not in self.printers:
            raise ConfigError(f"default_printer {self.default_printer!r} is not a configured printer")

    def to_dict(self) -> dict[str, Any]:
        server = vars(self.server).copy()
        server["reverb"] = vars(self.server.reverb).copy()
        return {
            "server": server,
            "transport": vars(self.transport).copy(),
            "backend": self.backend,
            "printers": {k: {"name": p.name, "options": list(p.options)} for k, p in self.printers.items()},
            "routing": {k: vars(r).copy() for k, r in self.routing.items()},
            "default_printer": self.default_printer,
            "timeouts": vars(self.timeouts).copy(),
            "retention": vars(self.retention).copy(),
            "update": vars(self.update).copy(),
        }

    def redacted_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        if d["server"].get("token"):
            d["server"]["token"] = "*" * 8
        return d


def _build(section: type, data: dict[str, Any], where: str):
    allowed = section.__dataclass_fields__
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
    try:
        return section(**data)
    except TypeError as exc:
        raise ConfigError(f"bad {where} section: {exc}") from exc


def from_dict(data: dict[str, Any]) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a JSON object")
    server_data = dict(data.get("server", {}))
    reverb = _build(Reverb, server_data.pop("reverb", {}) or {}, "server.reverb")
    server = _build(Server, server_data, "server")
    server.reverb = reverb
    cfg = Config(
        server=server,
        transport=_build(Transport, data.get("transport", {}), "transport"),
        backend=data.get("backend", "auto"),
        printers={k: _build(Printer, v, f"printers.{k}") for k, v in data.get("printers", {}).items()},
        routing={k: _build(Route, v, f"routing.{k}") for k, v in data.get("routing", {}).items()},
        default_printer=data.get("default_printer"),
        timeouts=_build(Timeouts, data.get("timeouts", {}), "timeouts"),
        retention=_build(Retention, data.get("retention", {}), "retention"),
        update=_build(UpdateSettings, data.get("update", {}), "update"),
    )
    cfg.validate()
    return cfg


def load(path: str | None = None) -> Config:
    path = path or default_config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}") from exc
    cfg = from_dict(data)
    if cfg.server.token == KEYRING_MARKER:
        cfg.server.token = _keyring_get(cfg.server.agent_uuid) or ""
    return cfg


def save(cfg: Config, path: str | None = None) -> None:
    """Atomic write with strict permissions; token to the keyring when available."""
    path = path or default_config_path()
    cfg.validate()
    data = cfg.to_dict()
    token = data["server"].get("token") or ""
    if token and token != KEYRING_MARKER and _keyring_set(cfg.server.agent_uuid, token):
        data["server"]["token"] = KEYRING_MARKER
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    # 0660, not 0600: on a .deb install the file lives in /etc/tbhprint
    # (root:tbhprint, setgid) and the desktop user is in group tbhprint so
    # the CLI/tray can still read the config when the daemon is down. On
    # Windows the per-user %LOCALAPPDATA% ACL is the protection; chmod is
    # mostly a no-op there.
    try:
        os.chmod(tmp, 0o660)
    except OSError:
        pass
    os.replace(tmp, path)


def machine_name() -> str:
    import socket
    return socket.gethostname() or "Print agent"


def server_from_pairing(data: dict[str, Any], server_url: str, agent_name: str) -> Server:
    """The `POST /pair` response -> a `Server` section, shared by the CLI's
    `pair`, the daemon's `pair` control command and the tray's Settings
    window - one place that knows the pairing payload's shape."""
    reverb = data.get("reverb") or {}
    return Server(
        url=server_url,
        token=str(data["token"]),
        agent_uuid=str(data["agent_uuid"]),
        agent_name=str(data.get("name") or agent_name),
        tenant=str(data.get("tenant") or ""),
        channel=str(data.get("channel") or ""),
        reverb=Reverb(key=str(reverb.get("key") or ""), host=str(reverb.get("host") or ""),
                     port=int(reverb.get("port") or 443), scheme=str(reverb.get("scheme") or "https")),
    )


def apply_update(cfg: Config, update: dict[str, Any]) -> Config:
    """Merge a partial update (a masked token means keep the current one)."""
    merged = cfg.to_dict()
    for key, value in update.items():
        if key not in merged:
            raise ConfigError(f"unknown config section: {key}")
        if key in ("printers", "routing"):
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            deep = copy.deepcopy(merged[key])
            deep.update(value)
            merged[key] = deep
        else:
            merged[key] = value
    token = merged.get("server", {}).get("token", "")
    if token and set(token) == {"*"}:
        merged["server"]["token"] = cfg.server.token
    return from_dict(merged)


# -- optional OS keyring ------------------------------------------------------

def _keyring_get(agent_uuid: str) -> str | None:
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, agent_uuid or "agent")
    except Exception:
        return None


def _keyring_set(agent_uuid: str, token: str) -> bool:
    try:
        import keyring  # type: ignore
    except ImportError:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, agent_uuid or "agent", token)
        return True
    except Exception:
        return False
