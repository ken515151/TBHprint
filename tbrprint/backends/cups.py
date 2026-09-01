"""CUPS submission via the `lp` command line (Linux, macOS).

Subprocess-based rather than pycups: no compiled dependency, and
lp/lpstat/cancel are stable interfaces. Carried over from SyncroPrint.
"""

from __future__ import annotations

import re
import subprocess

from . import PrintError

_REQUEST_ID_RE = re.compile(r"request id is (\S+)")


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def list_printers(timeout: float = 10) -> list[str]:
    """Names of printers currently accepting jobs (`lpstat -a`)."""
    try:
        proc = _run(["lpstat", "-a"], timeout)
    except FileNotFoundError as exc:
        raise PrintError("lpstat not found - is cups-client installed?") from exc
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        if "no destinations" in err.lower():
            return []
        raise PrintError(f"lpstat -a failed: {err}")
    printers = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "accepting":
            printers.append(parts[0])
    return printers


def submit(printer: str, path: str, *, copies: int = 1,
           options: list[str] | None = None, title: str | None = None,
           timeout: float = 60) -> str:
    """Submit a file to CUPS; returns the request id (e.g. 'HP_LaserJet-42')."""
    argv = ["lp", "-d", printer, "-n", str(max(1, copies))]
    if title:
        argv += ["-t", title[:120]]
    for opt in options or []:
        argv += ["-o", opt]
    argv.append(path)
    try:
        proc = _run(argv, timeout)
    except FileNotFoundError as exc:
        raise PrintError("lp not found - is cups-client installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise PrintError(f"lp timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise PrintError(f"lp failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    match = _REQUEST_ID_RE.search(proc.stdout)
    if not match:
        raise PrintError(f"lp succeeded but no request id in output: {proc.stdout.strip()!r}")
    return match.group(1)


def cancel(backend_job_id: str, timeout: float = 10) -> None:
    proc = _run(["cancel", backend_job_id], timeout)
    if proc.returncode != 0:
        raise PrintError(f"cancel {backend_job_id} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def _job_listed(argv: list[str], backend_job_id: str, timeout: float) -> bool | None:
    proc = _run(argv, timeout)
    if proc.returncode != 0:
        return None
    return any(line.split() and line.split()[0] == backend_job_id for line in proc.stdout.splitlines())


def job_outcome(backend_job_id: str, timeout: float = 10) -> str:
    """"active" (queued/printing), "ok", "failed" (cancelled/aborted by CUPS) or "unknown"."""
    if _job_listed(["lpstat", "-o"], backend_job_id, timeout):
        return "active"
    successful = _job_listed(["lpstat", "-W", "successful", "-o"], backend_job_id, timeout)
    if successful is None:
        return "unknown"
    if successful:
        return "ok"
    if _job_listed(["lpstat", "-W", "completed", "-o"], backend_job_id, timeout):
        return "failed"
    return "unknown"
