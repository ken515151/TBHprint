"""Printer backends: one module per OS printing system.

Each backend exposes the same small surface the pipeline calls:

  list_printers() -> list[str]
  submit(printer, path, *, copies, options, title, timeout) -> backend job id (str)
  cancel(backend_job_id, timeout) -> None
  job_outcome(backend_job_id, timeout) -> "active" | "ok" | "failed" | "unknown"   (optional)
  PrintError

Standard OS drivers only (FEATURES.md §13): CUPS via `lp` on Linux/macOS,
pypdfium2 + the Windows spooler (GDI) on Windows. No raw ESC/POS, no
drawer kick.
"""

from __future__ import annotations

import sys
from types import ModuleType


class PrintError(RuntimeError):
    """A backend could not submit, cancel or query a job."""


def get_backend(name: str = "auto") -> ModuleType:
    if name == "auto":
        name = "windows" if sys.platform.startswith("win") else "cups"
    if name == "windows":
        from . import windows
        return windows
    if name == "cups":
        from . import cups
        return cups
    raise ValueError(f"unknown backend {name!r}")
