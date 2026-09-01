"""`tbhprint` command line.

  tbhprint run [--supervised] [--dry-run] [--verbose]   the agent itself (foreground)
  tbhprint pair <server url> <code> --name ...           enrol this PC with a shop
  tbhprint printers                                       printers the OS knows
  tbhprint route <type> --printer <name> [--copies N] [--duplex ...] [--disable]
  tbhprint routes                                         show routing
  tbhprint status | history | reprint <uuid> | pause | resume | test-print <printer> | catch-up
  tbhprint tray [--open WINDOW]                           tray icon (Windows: also supervises the agent)
  tbhprint settings                                       open Settings in the running tray, else start it
  tbhprint update [--check-only]                          check for (and install) an agent update
  tbhprint quit                                            stop the tray + agent
  tbhprint service                                         Linux: systemd install hints; Windows: n/a
  tbhprint --version

Commands other than run/pair/tray/settings/service talk to the running
agent over the control channel; route/printers/pair fall back to editing
the config directly when the agent is not running.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

import requests

from . import __version__, api as apimod, config as cfgmod, control, singleinstance
from .backends import PrintError, get_backend
from .daemon import build

log = logging.getLogger("tbhprint")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tbhprint", description="TechBenchHub print agent")
    parser.add_argument("--config", default=None, help=f"config file (default {cfgmod.default_config_path()})")
    parser.add_argument("--version", action="version", version=f"tbhprint {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the agent in the foreground")
    p.add_argument("--supervised", action="store_true",
                   help="this run is owned by the tray's supervisor (Windows) - informational")
    p.add_argument("--dry-run", action="store_true", help="fetch + route + log, never print")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--backend", choices=cfgmod.BACKENDS, default=None)

    p = sub.add_parser("pair", help="pair with a TechBenchHub shop")
    p.add_argument("server_url")
    p.add_argument("code")
    p.add_argument("--name", default=None, help="agent name shown in Settings (default: this PC's name)")

    sub.add_parser("printers", help="list the printers this PC knows")

    p = sub.add_parser("route", help="route a document type to a printer")
    p.add_argument("document_type")
    p.add_argument("--printer", required=True, help="OS printer name (from `tbhprint printers`)")
    p.add_argument("--copies", type=int, default=None, help="override the job's copies")
    p.add_argument("--duplex", choices=cfgmod.DUPLEX_VALUES, default="off")
    p.add_argument("--rotate", action="store_true")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--option", action="append", default=[], help="backend option (repeatable)")

    sub.add_parser("routes", help="show routing")
    p = sub.add_parser("default-printer", help="printer for document types with no route")
    p.add_argument("printer")

    sub.add_parser("status")
    p = sub.add_parser("history")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--status", default=None)
    p = sub.add_parser("reprint")
    p.add_argument("uuid")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("catch-up", help="ask the server for open jobs now")
    p = sub.add_parser("test-print")
    p.add_argument("printer", help="OS printer name")
    p = sub.add_parser("log")
    p.add_argument("-n", type=int, default=50)

    p = sub.add_parser("tray", help="tray icon (Windows: also supervises the agent as a child process)")
    p.add_argument("--open", dest="open_window", default=None,
                   choices=("settings", "status", "history", "log"),
                   help="open this window (forwarded to an already-running tray, if any)")
    p.add_argument("--dry-run", action="store_true", help="passed to the agent this tray supervises (Windows)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--state-dir", default=None)

    sub.add_parser("settings", help="open Settings in the running tray, else start it")
    p = sub.add_parser("update", help="check for (and install) an agent update")
    p.add_argument("--check-only", action="store_true", help="only check - never download or install")
    sub.add_parser("quit", help="stop the tray + agent")

    p = sub.add_parser("service", help="Linux: systemd install hints (Windows: the installer owns startup)")
    p.add_argument("action", nargs="?", choices=("install", "remove"), default=None)

    args = parser.parse_args(argv)
    config_path = args.config or cfgmod.default_config_path()

    handler = {
        "run": cmd_run, "pair": cmd_pair, "printers": cmd_printers, "route": cmd_route,
        "routes": cmd_routes, "default-printer": cmd_default_printer, "status": cmd_status,
        "history": cmd_history, "reprint": cmd_reprint, "pause": cmd_pause, "resume": cmd_resume,
        "catch-up": cmd_catch_up, "test-print": cmd_test_print, "log": cmd_log, "service": cmd_service,
        "tray": cmd_tray, "settings": cmd_settings, "update": cmd_update, "quit": cmd_quit,
    }[args.cmd]
    try:
        return handler(args, config_path)
    except (cfgmod.ConfigError, apimod.ApiError, control.ControlError, PrintError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"error: could not reach the server: {exc}", file=sys.stderr)
        return 1


# -- run -------------------------------------------------------------------------

def cmd_run(args, config_path: str) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    state_dir = args.state_dir or cfgmod.default_state_dir()
    lock = singleinstance.SingleInstanceLock(singleinstance.AGENT_MUTEX_NAME,
                                             lock_path=os.path.join(state_dir, "agent.lock"))
    try:
        lock.acquire()
    except singleinstance.AlreadyRunning:
        log.error("another tbhprint agent is already running against state dir %s - exiting", state_dir)
        return 1
    try:
        daemon, store, pipeline = build(config_path, state_dir=state_dir, dry_run=args.dry_run,
                                        backend_name=args.backend)
        server = control.ControlServer(control.Dispatcher(daemon))
        server.start()
        pipeline.start()
        daemon.start_transports()
        threading.Thread(target=daemon.maintenance_loop, name="maintenance", daemon=True).start()

        stop = threading.Event()

        def _term(signum, frame):
            log.info("received signal %d, shutting down", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        log.info("tbhprint %s up (paired=%s dry_run=%s supervised=%s)",
                 __version__, daemon.cfg.server.is_paired, args.dry_run, args.supervised)
        while not stop.is_set():
            time.sleep(0.5)
        server.stop()
        daemon.stop()
        store.close()
        return 0
    finally:
        lock.release()


# -- tray / settings ---------------------------------------------------------------

def cmd_tray(args, config_path: str) -> int:
    try:
        from .applet import tray as tray_mod
    except ImportError as exc:
        print(f"error: the tray applet needs pystray + Pillow (pip install 'tbhprint[tray]'): {exc}", file=sys.stderr)
        return 1
    return tray_mod.main(config_path, state_dir=args.state_dir, dry_run=args.dry_run,
                         verbose=args.verbose, open_window=args.open_window)


def cmd_settings(args, config_path: str) -> int:
    from . import traychannel
    if traychannel.send("open", window="settings"):
        return 0
    try:
        from .applet import tray as tray_mod
    except ImportError as exc:
        print(f"error: the tray applet needs pystray + Pillow (pip install 'tbhprint[tray]'): {exc}", file=sys.stderr)
        return 1
    return tray_mod.main(config_path, open_window="settings")


def cmd_quit(args, config_path: str) -> int:
    from . import traychannel
    if traychannel.send("quit"):
        print("Stopping TBHprint (tray + agent).")
        return 0
    print("TBHprint is not running.")
    return 1


# -- update ------------------------------------------------------------------------

def cmd_update(args, config_path: str) -> int:
    data = _control(lambda c: c.call("update", check_only=args.check_only))
    version = (data or {}).get("version")
    if not version:
        print("Already up to date.")
        return 0
    notes = f": {data['notes']}" if data.get("notes") else ""
    print(f"Update {version} available{notes}.")
    if args.check_only:
        print("(--check-only: not installing)")
    else:
        print("Installing when no job is active (see `tbhprint log` for progress).")
    return 0


# -- pair --------------------------------------------------------------------------

def cmd_pair(args, config_path: str) -> int:
    """Pairing goes through the daemon (the config's single writer) when
    one is reachable - `tbhprint pair` and the tray's Settings window share
    the same `Daemon.pair()` code. Falls back to writing the config file
    directly only when no daemon answers."""
    name = args.name or cfgmod.machine_name()
    server_url = args.server_url.rstrip("/")
    if not server_url.startswith(("http://", "https://")):
        server_url = "https://" + server_url

    client = control.ControlClient(timeout=30)
    try:
        redacted = client.call("pair", url=server_url, code=args.code, name=name)
    except OSError:
        redacted = None  # no daemon running - pair directly below
    finally:
        client.close()

    if redacted is None:
        data = apimod.pair(server_url, args.code, name)
        cfg = _load_or_empty(config_path)
        cfg.server = cfgmod.server_from_pairing(data, server_url, name)
        cfgmod.save(cfg, config_path)
        redacted = cfg.redacted_dict()

    server_info = redacted["server"]
    print(f"Paired as \"{server_info['agent_name']}\" with {server_url} (agent {server_info['agent_uuid']}).")
    print(f"Config: {config_path}")
    print("Next: `tbhprint printers`, then `tbhprint route ticket_label --printer \"<name>\"`, then `tbhprint run`.")
    return 0


# -- printers / routing ------------------------------------------------------------

def cmd_printers(args, config_path: str) -> int:
    names = _try_control(lambda c: c.call("printers"))
    if names is None:
        cfg = _load_or_empty(config_path)
        names = get_backend(cfg.backend).list_printers()
    if not names:
        print("No printers found.")
        return 0
    for name in names:
        print(name)
    return 0


def cmd_route(args, config_path: str) -> int:
    cfg = _load_or_empty(config_path)
    key = _printer_key(args.printer)
    cfg.printers[key] = cfgmod.Printer(name=args.printer, options=list(args.option))
    cfg.routing[args.document_type] = cfgmod.Route(printer=key, enabled=not args.disable,
                                                   copies=args.copies, duplex=args.duplex, rotate=args.rotate)
    cfgmod.save(cfg, config_path)
    _try_control(lambda c: c.call("reload"))
    print(f"{args.document_type} -> {args.printer}" + (" (disabled)" if args.disable else ""))
    return 0


def cmd_routes(args, config_path: str) -> int:
    cfg = _load_or_empty(config_path)
    if not cfg.routing and not cfg.default_printer:
        print("No routes yet. `tbhprint route ticket_label --printer \"<name>\"`")
        return 0
    for doc_type, route in sorted(cfg.routing.items()):
        printer = cfg.printers.get(route.printer)
        copies = f" x{route.copies}" if route.copies else ""
        print(f"{doc_type:16} -> {printer.name if printer else route.printer}{copies}"
              + ("" if route.enabled else "  (disabled)"))
    if cfg.default_printer:
        print(f"{'(everything else)':16} -> {cfg.printers[cfg.default_printer].name}")
    return 0


def cmd_default_printer(args, config_path: str) -> int:
    cfg = _load_or_empty(config_path)
    key = _printer_key(args.printer)
    cfg.printers.setdefault(key, cfgmod.Printer(name=args.printer))
    cfg.default_printer = key
    cfgmod.save(cfg, config_path)
    _try_control(lambda c: c.call("reload"))
    print(f"default printer -> {args.printer}")
    return 0


def _printer_key(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_") or "printer"


# -- control-channel commands ---------------------------------------------------

def cmd_status(args, config_path: str) -> int:
    data = _control(lambda c: c.call("status"))
    for key, value in data.items():
        print(f"{key:12} {value}")
    return 0


def cmd_history(args, config_path: str) -> int:
    rows = _control(lambda c: c.call("history", limit=args.limit, status=args.status))
    for row in rows:
        error = f"  {row['error']}" if row.get("error") else ""
        print(f"{row['received_at']}  {row['status']:11} {row['document_type']:16} {row['job_id']}{error}")
    return 0


def cmd_reprint(args, config_path: str) -> int:
    _control(lambda c: c.call("reprint", id=args.uuid))
    print("submitted")
    return 0


def cmd_pause(args, config_path: str) -> int:
    _control(lambda c: c.call("pause"))
    print("paused")
    return 0


def cmd_resume(args, config_path: str) -> int:
    _control(lambda c: c.call("resume"))
    print("resumed")
    return 0


def cmd_catch_up(args, config_path: str) -> int:
    data = _control(lambda c: c.call("catch_up"))
    print(f"{data['jobs']} open job(s) fetched")
    return 0


def cmd_test_print(args, config_path: str) -> int:
    key = _printer_key(args.printer)
    result = _try_control(lambda c: c.call("test_print", printer=key))
    if result is None:
        # Agent not running: print directly.
        from .pipeline import _write_test_pdf
        cfg = _load_or_empty(config_path)
        path = os.path.join(cfgmod.default_state_dir(), "testpage.pdf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_test_pdf(path)
        get_backend(cfg.backend).submit(args.printer, path, copies=1, title="TBHprint test page")
    print("test page submitted")
    return 0


def cmd_log(args, config_path: str) -> int:
    for line in _control(lambda c: c.call("get_log_tail", n=args.n)):
        print(line)
    return 0


# -- service -----------------------------------------------------------------------

def cmd_service(args, config_path: str) -> int:
    if sys.platform.startswith("win"):
        print("On Windows the installer owns startup (Start Menu entry / logon tray) -", file=sys.stderr)
        print("there is nothing for `tbhprint service` to install or remove here.", file=sys.stderr)
        return 1
    print("Linux/macOS: install the systemd unit from packaging/tbhprint.service:")
    print("  sudo useradd -r -G lp tbhprint; sudo mkdir -p /etc/tbhprint /var/lib/tbhprint")
    print("  sudo cp packaging/tbhprint.service /etc/systemd/system/ && sudo systemctl enable --now tbhprint")
    print("Tray applet at login: cp packaging/tbhprint-tray.desktop ~/.config/autostart/")
    return 0


# -- helpers -----------------------------------------------------------------------

def _load_or_empty(config_path: str) -> cfgmod.Config:
    try:
        return cfgmod.load(config_path)
    except cfgmod.ConfigError as exc:
        if "not found" in str(exc):
            return cfgmod.Config()
        raise


def _control(fn):
    client = control.ControlClient()
    try:
        return fn(client)
    except OSError as exc:
        raise control.ControlError(f"the agent is not running ({exc}) - start it with `tbhprint run`")
    finally:
        client.close()


def _try_control(fn):
    client = control.ControlClient(timeout=3)
    try:
        return fn(client)
    except (OSError, control.ControlError):
        return None
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
