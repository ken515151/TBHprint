# TBHprint — the TechBenchHub print agent

A small background program for a shop PC. TechBenchHub queues a print job
(ticket label at intake, booking sheet, invoice, collection form…); TBHprint
receives it over an authenticated websocket, fetches the PDF, prints it to the
printer you routed that document type to, and reports back — so Settings →
Printing in TechBenchHub shows *printed* or *failed (reason)* for every job.

Windows, Linux (CUPS), macOS (CUPS). Standard OS printer drivers only — no
raw ESC/POS, no cash drawers, no printer-specific code. If it prints from a
browser it prints from TBHprint.

## Install (shop PC)

**Windows:** in TechBenchHub, go to **Settings → Printing** and download
`TBHprint-Setup-<version>.exe`. Run it - no admin prompt, nothing else to
install. The Settings window opens on its own once it's done; enter the
8-character pairing code shown on that same TechBenchHub page (valid 10
minutes, single use) and pick which printer each document type should go
to. TBHprint then runs quietly in the tray from then on, starting itself
at logon.

**Linux** (Mint 21/22, LMDE 6, Ubuntu, Debian): download
`tbhprint_<version>_all.deb` from the same TechBenchHub page and install
it (`sudo apt install ./tbhprint_<version>_all.deb`, or open it with your
distro's package installer). The tray icon appears the next time you log
in - or open **TBHprint** from the application menu straight away, which
opens the same Settings window the tray does. Either way, enter the
pairing code there.

Both installers add themselves to Settings → Printing's list of agents;
revoking an agent there kills its token immediately.

## Install (developer)

Python 3.10+.

```
git clone https://github.com/<org>/TBHprint
cd TBHprint
pip install -e .[dev,tray,keyring]
```

Config lives at `%LOCALAPPDATA%\TBHprint\config.json` on a Windows
install (per-user, no admin needed) or `/etc/tbhprint/config.json` on
Linux (system-wide, owned by the `tbhprint` service user).

## Pair with your shop (command line)

In TechBenchHub: **Settings → Printing → Add agent** shows an 8-character
code (valid 10 minutes, single use). On the shop PC:

```
tbhprint pair https://yourshop.techbenchhub.co.uk ABCD2345 --name "Front desk"
```

That stores the server URL, this agent's own bearer token and the websocket
details in the config file. Revoking the agent in Settings kills that
token immediately.

## Route documents to printers

```
tbhprint printers                                  # what the OS knows
tbhprint route ticket_label   --printer "Brother QL-800" --copies 2
tbhprint route booking_sheet  --printer "HP LaserJet"
tbhprint route invoice        --printer "HP LaserJet"
tbhprint route collection_form --printer "HP LaserJet"
tbhprint test-print "HP LaserJet"
```

Document types: `ticket_label`, `booking_sheet`, `collection_form`,
`invoice`, `receipt`, `estimate`, `credit_note`, `purchase_order`. A type
with no route fails the job with "no printer routed" so the reason is
visible in TechBenchHub rather than silently dropped.

## Tray applet (Windows and Linux)

```
pip install -e .[tray]  # pystray + Pillow; tkinter ships with Python (Linux: apt install python3-tk)
tbhprint tray           # Windows: supervises the agent as a child process
                        # Linux: connects to the systemd agent
tbhprint settings       # open the Settings window in the running tray (starts it if needed)
```

The icon's colour is the agent's health (green connected, amber polling
or paused, red error, grey not paired). The menu has pause/resume, "check
for jobs now", a test-print per printer, and the four windows: Status,
Print history (reprint / cancel), Settings (pair with the shop, route each
document type to a printer, copies, test print) and Log.

On a machine installed via the .exe/.deb above, the tray already starts
itself (Windows: at logon, per-user; Linux: as a systemd-owned service
plus a per-session autostart entry) - there's nothing to set up. Running
straight from a `pip install`, start it at login yourself: Windows has no
scheduled task to install (`tbhprint tray` from a Startup shortcut, or
just run it); on Linux drop `packaging/tbhprint-tray.desktop` into
`~/.config/autostart/` and see `packaging/tbhprint.service` for the
systemd unit the daemon itself runs under.

## Run (headless)

```
tbhprint run                 # foreground, logs to stdout
tbhprint run --dry-run       # everything except the actual print
tbhprint status
tbhprint history --limit 20
tbhprint reprint <uuid>
tbhprint pause / resume
tbhprint update --check-only # ask TechBenchHub for a newer version
```

## How delivery works (docs/PROTOCOL.md)

1. `pair` → `POST /api/print/v1/pair` → agent uuid + token + Reverb details.
2. Realtime: websocket to Reverb (Pusher protocol), private channel
   `private-tenant.<tenant>.print-agent.<uuid>` authorised with the token via
   `POST /api/print/v1/broadcasting/auth`. Event `print.job` carries the job.
3. Catch-up: on every (re)connect, and every `poll_interval_s` while the
   websocket is down, `GET /api/print/v1/jobs` lists every job still open
   for this agent. Jobs are deduplicated by their server-issued uuid.
4. Each job: `ack received` → fetch `document_url` (must be on the paired
   host, HTTPS, PDF, ≤ 25 MB) → print → `ack printed` or `ack failed`
   with the reason.

## Development

```
pip install -e .[dev]
pytest
```

MIT. Architecture carried over from the author's SyncroPrint for Linux
daemon — see NOTICE.
