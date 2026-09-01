# TBHprint distribution, Windows printing, supervision and updates

Owner rulings (2026-09-01) this design exists to satisfy:

- **Windows = an .exe installer.** No command line, ever. No additional
  software: the server renders the PDF, the agent puts it on paper by
  itself.
- **Linux = a .deb** (Mint 21/22, LMDE 6, Ubuntu, Debian), with git clone
  and pip as developer routes.
- **Reliability and ease of install over everything else.** "I'm not
  looking for the fewest lines of code." Self-contained beats dependency;
  supervised beats hoping; auto-update and offline detection are part of
  this build, not follow-ups.
- The tray icon is never the only door: first run opens Settings; there
  is a normal application entry on both platforms.

Everything below is binding for the builders. Where a builder must
deviate, they stop and say so before building around it.

---

## 1. Python floor: 3.10

`requires-python = ">=3.10"`. Mint 21 (Ubuntu 22.04) ships 3.10. No
3.11-only stdlib (`tomllib`, `typing.Self`, `except*`, `datetime.UTC`,
`StrEnum`). CI runs the tests on 3.10 and 3.12.

## 2. Windows print backend: pypdfium2 + pywin32, nothing external

`tbhprint/backends/windows.py` is rewritten. SumatraPDF and the shell
`print` verb are removed everywhere (code, README, docs, config example).

- `list_printers()` → `win32print.EnumPrinters(PRINTER_ENUM_LOCAL |
  PRINTER_ENUM_CONNECTIONS)` names. Default printer via
  `win32print.GetDefaultPrinter()` (used when config has none).
- `submit(printer, pdf_path, copies, options)`:
  1. Open the printer, take its DEVMODE (`GetPrinter(h, 2)['pDevMode']`),
     apply options, `CreateDC("WINSPOOL", printer, devmode)`.
  2. `StartDoc` with `{'DocName': title, 'Output': None}` → the job id
     becomes `backend_job_id` (cancel = `SetJob(..., JOB_CONTROL_DELETE)`).
  3. For each copy, for each page: `pypdfium2` renders the page at the
     device DPI (`LOGPIXELSX/Y`), auto-rotates when the page's aspect
     does not match the printable area (label printers), scales to fit
     the printable area (`HORZRES/VERTRES`, honouring
     `PHYSICALOFFSETX/Y`), draws with `StretchDIBits`, `StartPage` /
     `EndPage`.
  4. `EndDoc`. Any exception → `AbortDoc`, then `PrintError` with the
     real message (driver name + Win32 error text).
- Copies are looped by us, never via `dmCopies` (drivers ignore it).
- Options (per-printer `options` list, same shape as today): `duplex=long`
  | `duplex=short` (`dmDuplex`), `paper=<name>` (matched against
  `DeviceCapabilities(DC_PAPERNAMES)`, sets `dmPaperSize`),
  `orientation=portrait|landscape` (default `auto` = rotate to fit),
  `fit=none` (1:1 at 100%, cropped, for pre-sized labels; default `fit`).
- Rendering runs on a worker thread with the existing job timeout; a
  hung driver is reported as a failed job, never a hung agent.
- `Microsoft Print to PDF` is the automated test target: `StartDoc` with
  `'Output': <tmp path>` writes the spooled result to a file. The test
  prints a generated 2-page PDF (one portrait, one landscape) and asserts
  the output PDF has 2 pages and non-blank raster content. Skipped when
  that printer is absent. Pure fit/rotate maths gets normal unit tests.

`pypdfium2` and `pywin32` are Windows-only dependencies
(`sys_platform == 'win32'` markers). Linux keeps CUPS `lp` (PDF native).

## 3. Process model

### Windows: tray = supervisor, agent = child

`tbhprint tray` no longer embeds the daemon. It:

1. Takes the tray single-instance lock (`Local\TBHprint-tray` mutex). If
   already held, forwards its request (e.g. `--open settings`) to the
   running tray over the tray channel (below) and exits 0.
2. Starts the agent as a child: `pythonw.exe -m tbhprint run --supervised`
   (stdout/stderr → the rotating log file). Restarts it on exit with
   backoff 1, 2, 4 … 30 s, forever. Restart also when the child is alive
   but the control channel has not answered `status` for 60 s (kill,
   then restart). The child is killed on tray quit.
3. Agent single-instance: `Local\TBHprint-agent` mutex on Windows,
   `flock` on `<state>/agent.lock` on Linux; a second agent exits with a
   clear log line instead of double-printing.
4. **Tray channel**: the tray listens on loopback `127.0.0.1:47832`
   (Windows) / `$XDG_RUNTIME_DIR/tbhprint-tray.sock` (Linux) for
   `{"cmd":"open","window":"settings|status|history|log"}` and
   `{"cmd":"quit"}`. `tbhprint settings` (new CLI command) = open the
   Settings window in the running tray, else start the tray with
   `--open settings`. This is what the Start Menu / app-grid entry runs.
5. **First run**: when the daemon reports `unpaired`, the tray opens the
   Settings window on start without being asked.
6. The Tk mainloop never runs printing code: a hung UI cannot stop
   printing (the child keeps going; the supervisor's watchdog thread is
   independent of Tk).

### Linux: systemd owns the agent, the tray is a client

Unchanged model: `tbhprint.service` (system unit, `User=tbhprint`,
`Group=lp`, `Restart=always`, `RestartSec=3`, `StateDirectory=tbhprint`)
runs the daemon; the tray is a user-session autostart connecting to
`/run/tbhprint/control.sock` (mode 0660, group `tbhprint`). The tray never
supervises on Linux.

### Pairing moves into the daemon

New control command `pair {url, code, name}`: the daemon calls the API,
writes the config, reloads, and returns the redacted config. The Settings
window and `tbhprint pair` both use it (the CLI falls back to writing the
config directly only when no daemon is reachable). Reason: on Linux the
config is owned by the service, not the desktop user.

## 4. Auto-update

The agent asks **its own TechBenchHub server** (never GitHub - the repo
is private and agents only ever hold one credential):

```
GET /api/print/v1/update?platform=windows|linux&version=<current>
→ 200 {"version": "0.3.0", "url": ".../api/print/v1/update/download/<asset>",
       "sha256": "...", "notes": "..."}      newer available
→ 200 {"version": null}                       up to date / feed not configured
```

`tbhprint/update.py`: check on start (after the first successful
`status`), then every 6 h, and on `tbhprint update` / the tray's
"Check for updates". Download to the state dir, verify sha256, refuse if
mismatched (log + ErrorLog on the server via the existing failure path is
NOT used - this is local; log + tray notification). Install only when no
job is active; jobs arriving mid-install wait in the server queue and are
caught up on restart.

- **Windows**: run `TBHprint-Setup-<ver>.exe /VERYSILENT /SUPPRESSMSGBOXES
  /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`. Per-user install =
  no UAC. The installer's post-install `[Run]` relaunches the tray.
- **Linux**: the daemon (unprivileged) writes
  `/var/lib/tbhprint/update/<file>.deb` + `.sha256` and touches
  `/var/lib/tbhprint/update/requested`. A root **path unit**
  (`tbhprint-update.path` → `tbhprint-update.service`, `Type=oneshot`)
  verifies the checksum again, runs `apt-get install -y
  /var/lib/tbhprint/update/<file>.deb` (falls back to `dpkg -i`), and
  systemd restarts the daemon. A daily `tbhprint-update.timer` also
  triggers the check via `tbhprint update --check-only` as root. No
  sudoers rules, no polkit.
- Version reported to the server in `X-TBHprint-Version` (already sent)
  so the Printing settings page can show "update available" per agent.

## 5. Windows installer (Inno Setup, per-user, self-contained)

`build/windows/build.ps1` (runs locally and in CI, PowerShell 5.1-safe):

1. Downloads the official python.org **NuGet** package `python` (pinned
   `3.12.x`, sha256 pinned in the script) - a full, signed CPython
   layout including tkinter/Tcl-Tk. (The "embeddable" zip has no
   tkinter; if the NuGet layout ever lacks it the script fails loudly
   rather than shipping without.)
2. `pip install --target dist/win/Lib/site-packages --only-binary=:all:
   --platform win_amd64 --python-version 3.12 -r build/windows/
   requirements.txt` (requests, websockets, pystray, Pillow, pypdfium2,
   pywin32, keyring) plus the tbhprint package itself. pywin32's
   `pywin32_system32/*.dll` are copied beside `python.exe`; `._pth` /
   `sitecustomize` add `win32`, `win32/lib`, `Pythonwin`.
3. Generates `tbhprint.ico` from `icons.render` (Pillow), stamps the
   version from `pyproject.toml`.
4. `ISCC build/windows/tbhprint.iss` → `dist/TBHprint-Setup-<ver>.exe`.

`tbhprint.iss`:

- `AppId={{9D3C6E1A-...}}` fixed GUID; `PrivilegesRequired=lowest`;
  `DefaultDirName={localappdata}\Programs\TBHprint`; `CloseApplications=yes`;
  `RestartApplications=yes`; `SetupIconFile`; `UninstallDisplayIcon`;
  `ArchitecturesAllowed=x64compatible`.
- `[Files]` the whole `dist/win` tree.
- `[Icons]` Start Menu "TBHprint" → `pythonw.exe -m tbhprint settings`
  (icon set), "Uninstall TBHprint".
- `[Registry]` `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  `TBHprint` = `"…\pythonw.exe" -m tbhprint tray` (start at logon).
- `[Run]` post-install: start the tray (`nowait`), which opens Settings
  when unpaired.
- `[UninstallRun]` `python.exe -m tbhprint quit` (stops tray + agent);
  uninstall removes `%LOCALAPPDATA%\TBHprint` state only when the user
  agrees (Inno `MsgBox` in `CurUninstallStepChanged`).
- Config/state/log live in `%LOCALAPPDATA%\TBHprint\` (per-user, no
  admin). Log is rotating (5 × 2 MB).
- Unsigned until the owner buys a code-signing certificate: SmartScreen
  shows "unknown publisher" on the setup exe. `build.ps1` accepts
  `-SignTool` / `-CertThumbprint` so signing is one flag later.

Verification here: install → tray appears → Settings opens → pair with
the local demo tenant → route ticket labels to *Microsoft Print to PDF*
→ print from TechBenchHub → PDF lands → uninstall clean. Repeated for the
update path with two consecutive versions.

## 6. Linux .deb

`build/linux/build.sh` runs in Docker `ubuntu:22.04` (proves the 3.10
floor) and in CI, producing `tbhprint_<ver>_all.deb`
(`Architecture: all` - pure Python; the only compiled dependency, Pillow,
comes from the distro).

- `Depends: python3 (>= 3.10), python3-venv, python3-tk, python3-pil,
  python3-pil.imagetk, python3-gi, gir1.2-ayatanaappindicator3-0.1,
  libayatana-appindicator3-1, adduser`; `Recommends:
  gnome-shell-extension-appindicator`.
- Layout: `/opt/tbhprint/app` (the package source), `/opt/tbhprint/wheels`
  (pure-python wheels: requests + deps, websockets, pystray, keyring),
  `/usr/bin/tbhprint` (exec `/opt/tbhprint/venv/bin/python -m tbhprint
  "$@"`), `/lib/systemd/system/tbhprint.service`, `tbhprint-update.{path,
  service,timer}`, `/usr/share/applications/tbhprint.desktop`
  (`Exec=tbhprint settings`), `/etc/xdg/autostart/tbhprint-tray.desktop`
  (`Exec=tbhprint tray`), `/usr/share/icons/hicolor/*/apps/tbhprint.png`,
  `/etc/tbhprint/config.json` (conffile, 0660 root:tbhprint).
- `postinst`: `adduser --system --group --home /var/lib/tbhprint
  tbhprint`, add to `lp`; add `$SUDO_USER` to group `tbhprint` (so the
  desktop user can talk to the socket; the Settings window explains
  `usermod -aG tbhprint` + re-login if it cannot); `python3 -m venv
  --system-site-packages /opt/tbhprint/venv`; `pip install --no-index
  --find-links /opt/tbhprint/wheels /opt/tbhprint/app`; `systemctl enable
  --now tbhprint tbhprint-update.path tbhprint-update.timer`.
- `prerm`/`postrm`: stop + disable, remove the venv on purge, keep
  `/var/lib/tbhprint` unless purged.
- Verification in Docker: install the .deb on `ubuntu:22.04` and
  `debian:12` images (systemd not running there - assert unit files are
  valid with `systemd-analyze verify`, the venv imports `tbhprint`, and
  `tbhprint --version` works on 3.10 and 3.11). A real Mint desktop test
  belongs to the owner.

## 7. CI (GitHub Actions, private repo)

- `ci.yml` on push/PR: pytest on `ubuntu-22.04` (3.10) and `windows-latest`
  (3.12); the Windows job also runs the print-to-PDF integration test
  (the runner has *Microsoft Print to PDF*).
- `release.yml` on tag `v*`: build the exe on `windows-latest` (Inno Setup
  is preinstalled there) and the .deb on `ubuntu-22.04`; write
  `SHA256SUMS`; create the GitHub Release with the three assets. The
  TechBenchHub server reads that release (see PRINT_AGENT_DESIGN.md
  "Phase 2").
- Version = `pyproject.toml`; the tag must match or the release job fails.

## 8. CLI surface after this build

`run [--supervised]`, `tray [--open WINDOW]`, `settings` (open the window),
`pair`, `printers`, `route`, `routes`, `default-printer`, `status`,
`history`, `reprint`, `pause`, `resume`, `catch-up`, `test-print`, `log`,
`update [--check-only]`, `quit` (stop tray + agent; Windows uninstall uses
it), `service` (Linux: prints the systemd steps for git-clone installs;
Windows: removed - the installer owns startup), `--version`.

## 9. Tests (all must pass on both CI platforms)

Existing 41 + new: fit/rotate maths; supervisor restart/backoff with a
fake child; single-instance locks; tray channel open/quit; `pair` control
command; update manifest parsing, sha256 refusal, "no install while a job
is active"; Windows print-to-PDF integration (skipped elsewhere); Inno
script and .deb control files linted (`iscc /?` not needed - the CI build
is the test).

## As built (2026-09-01)

Everything above shipped, with these reconciliations:

- **Runtime**: the python.org NuGet `python` package has no tkinter, so
  `build.ps1` backfills Tcl/Tk from the official installer's `tcltk.msi`
  (same publisher, same version, sha256-pinned); it still fails loudly if
  tkinter is missing afterwards.
- **No bytecode in the install tree**: every launch uses `-B` (shortcuts,
  Run key, supervisor child + `PYTHONDONTWRITEBYTECODE`), the runtime
  ships `aaa_tbhprint_nobytecode.pth` + `sitecustomize.py`, and the
  uninstaller sweeps `{app}` unconditionally before asking about the state
  directory. A silent uninstall never prompts and keeps
  `%LOCALAPPDATA%\TBHprint`.
- **Linux service** runs with `Group=tbhprint` + `SupplementaryGroups=lp`
  (the socket must be group-tbhprint); `/etc/tbhprint` is `root:tbhprint`
  2770 so the daemon can save pairing atomically.
- **Update path unit** clears the `requested` marker before installing so
  a failing `apt-get` can never loop; a failed `.deb` is kept for
  diagnosis.
- `output=<path>` printer option for file-backed Windows queues (how the
  end-to-end test prints, and how a shop can archive to PDF).
- Windows-only deviations from the owner-facing story: none. Unsigned
  setup exe until a code-signing certificate exists.

Verified 2026-09-01: `.deb` installs on ubuntu:22.04 (Python 3.10) and
debian:12 in Docker; Windows installer install → pair → server-queued
ticket label printed through GDI to *Microsoft Print to PDF* → acked →
Settings opened from the Start-Menu path → supervisor restarted a killed
agent → silent uninstall clean. Real label/laser printers and a real Linux
desktop remain the owner's test.

### Linux, run for real (2026-09-01, Docker ubuntu:22.04 with systemd + CUPS)

A systemd container (`--privileged`, `/sbin/init`) with CUPS + cups-pdf
stood in for a Mint 21 desk. Found and fixed by actually running it:

- The first `.deb` built `tbhprint` on the target with pip; under a
  `--system-site-packages` venv on 22.04 the isolated build env produced
  `UNKNOWN-0.0.0` and the service crash-looped. The package now carries a
  pre-built `tbhprint` wheel and `postinst` only installs wheels (two
  steps: force-reinstall `tbhprint` itself `--no-deps`, then resolve
  `tbhprint[tray]`; Pillow/tk/gi from the distro).
- `pystray`'s Linux deps (`six`, `python-xlib`) are vendored; `keyring`
  is not shipped on Linux (SecretStorage needs compiled `cryptography`).
- The tray's single-instance lock lives in `$XDG_RUNTIME_DIR` beside its
  socket, not in the service's `/var/lib/tbhprint`.

Verified on that box: install → service active, socket `0660 tbhprint` →
paired with the demo tenant → server-queued ticket label printed through
`lp` to the CUPS PDF queue and acked → `kill -9` of the daemon, systemd
restarted it (`NRestarts=1`) → staged `.deb` + marker picked up by the
path unit, checksum re-verified, `apt-get` ran, marker cleared → tray
applet ran as a desktop user under Xvfb and the desktop user drove the
service over the socket via the `tbhprint` group.
