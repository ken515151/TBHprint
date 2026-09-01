"""Windows printing.

Preferred: SumatraPDF (free, single exe) - `SumatraPDF.exe -print-to
"<printer>" -print-settings "<n>x" -silent file.pdf` prints any PDF to
any installed printer with no dialog and honours the copies count. Looked
for on PATH, in Program Files and in the per-user install location.

Fallback: the shell "print" verb (`os.startfile(path, "print")`), which
sends the PDF to the DEFAULT printer through whatever PDF app is
associated - one copy, no printer choice. Good enough to get a first
label out; the README says to install SumatraPDF for real use.

Options accepted (per printer, config `options`): "landscape",
"duplex", "duplexshort", "fit" / "noscale" / "shrink", "paper=<n>",
"bin=<n>" - passed through as SumatraPDF print-settings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import PrintError

_SUMATRA_CANDIDATES = (
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "SumatraPDF", "SumatraPDF.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "SumatraPDF", "SumatraPDF.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "SumatraPDF", "SumatraPDF.exe"),
)


def find_sumatra() -> str | None:
    found = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
    if found:
        return found
    for candidate in _SUMATRA_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def list_printers(timeout: float = 15) -> list[str]:
    """Installed printers via PowerShell's Get-Printer (works without pywin32)."""
    if not sys.platform.startswith("win"):
        return []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Printer | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PrintError(f"could not list printers: {exc}") from exc
    if proc.returncode != 0:
        raise PrintError(f"Get-Printer failed: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def build_print_settings(copies: int, options: list[str] | None) -> str:
    parts = [f"{max(1, copies)}x"]
    for opt in options or []:
        opt = opt.strip()
        if opt:
            parts.append(opt)
    return ",".join(parts)


def submit(printer: str, path: str, *, copies: int = 1,
           options: list[str] | None = None, title: str | None = None,
           timeout: float = 60) -> str:
    sumatra = find_sumatra()
    if sumatra:
        argv = [sumatra, "-print-to", printer, "-print-settings",
                build_print_settings(copies, options), "-silent", path]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise PrintError(f"SumatraPDF timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise PrintError(f"SumatraPDF failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
        return f"sumatra-{os.path.basename(path)}"
    if not sys.platform.startswith("win"):
        raise PrintError("Windows backend used on a non-Windows platform")
    # Shell print verb: default printer, one copy per call.
    try:
        for _ in range(max(1, copies)):
            os.startfile(path, "print")  # type: ignore[attr-defined]
    except OSError as exc:
        raise PrintError(f"shell print failed (install SumatraPDF for silent printing): {exc}") from exc
    return f"shell-{os.path.basename(path)}"


def cancel(backend_job_id: str, timeout: float = 10) -> None:
    # Neither path exposes a job handle we can cancel after submission.
    raise PrintError("cancelling a submitted Windows print job is not supported")
