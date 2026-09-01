"""Windows printing: pypdfium2 renders, pywin32/GDI puts ink on paper.

No external program (SumatraPDF and the shell "print" verb are gone - see
docs/DISTRIBUTION_DESIGN.md section 2). The flow for one job:

  1. Open the printer, read its DEVMODE, apply duplex/paper options, and
     ``CreateDC("WINSPOOL", printer, devmode)``.
  2. ``StartDoc`` (the returned job id becomes our `backend_job_id`, as
     ``"<printer>#<id>"`` so `cancel()` can re-open the right queue).
  3. For each copy, for each page: pypdfium2 rasterises the page at the
     device's DPI, we auto-rotate when the page's aspect does not match
     the printable area (label printers), scale to fit (or not, for
     `fit=none`), and ``StretchDIBits`` it onto the page - ``StartPage`` /
     ``EndPage`` around each.
  4. ``EndDoc``. Any exception aborts the job (``AbortDoc``) and raises
     `PrintError` with the driver name and the real Win32 error text.

Copies are looped by us, never via `dmCopies` - drivers routinely ignore
that field. The whole job runs on a worker thread so a hung driver
becomes a failed print job, never a hung agent (the existing
`print_submit_s` timeout applies).

The pure fit/rotate/scale maths lives in `compute_page_transform()` - it
takes plain numbers in and out, so it is unit-tested without pywin32.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from . import PrintError

log = logging.getLogger("tbhprint.backends.windows")

ORIENTATIONS = ("portrait", "landscape", "auto")
FIT_MODES = ("fit", "none")

_DIB_RGB_COLORS = 0
_SRCCOPY = 0x00CC0020
_BI_RGB = 0

# GetDeviceCaps indices (wingdi.h) - the ones we need.
_HORZRES = 8
_VERTRES = 10
_LOGPIXELSX = 88
_LOGPIXELSY = 90
_PHYSICALWIDTH = 110
_PHYSICALHEIGHT = 111
_PHYSICALOFFSETX = 112
_PHYSICALOFFSETY = 113


# -- pure maths: fit/rotate/scale -> a target rect in device pixels --------

@dataclass(frozen=True)
class DeviceGeometry:
    """What we need from a printer DC's GetDeviceCaps, in device pixels/DPI."""
    dpi_x: float
    dpi_y: float
    printable_w_px: int
    printable_h_px: int
    physical_w_px: int
    physical_h_px: int
    offset_x_px: int
    offset_y_px: int


@dataclass(frozen=True)
class PageTransform:
    """How to render and place one page. `rotate_degrees` goes straight into
    pypdfium2's `render(rotation=...)`; the `dest_*` rect is StretchDIBits's
    destination, in device pixels relative to the *printable* area's origin
    (i.e. already corrected for PHYSICALOFFSETX/Y so it centres on the
    physical sheet, not just on the printable rectangle)."""
    rotate_degrees: int
    dest_x: int
    dest_y: int
    dest_w: int
    dest_h: int


def compute_page_transform(page_w_pt: float, page_h_pt: float, device: DeviceGeometry, *,
                           orientation: str = "auto", fit: str = "fit") -> PageTransform:
    if page_w_pt <= 0 or page_h_pt <= 0:
        raise ValueError(f"page size must be positive, got {page_w_pt}x{page_h_pt}")
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}, got {orientation!r}")
    if fit not in FIT_MODES:
        raise ValueError(f"fit must be one of {FIT_MODES}, got {fit!r}")

    page_is_landscape = page_w_pt > page_h_pt
    printable_is_landscape = device.printable_w_px > device.printable_h_px
    if orientation == "portrait":
        want_landscape = False
    elif orientation == "landscape":
        want_landscape = True
    else:  # auto: rotate whichever way makes the page's aspect match the printable area
        want_landscape = printable_is_landscape
    rotate = page_is_landscape != want_landscape
    eff_w_pt, eff_h_pt = (page_h_pt, page_w_pt) if rotate else (page_w_pt, page_h_pt)

    # "Natural" size at device DPI, pre fit-scale (used only to work out the
    # fit ratio - the actual source pixels come from whatever pypdfium2
    # renders, since StretchDIBits does the final stretch either way).
    render_w_px = max(1.0, eff_w_pt / 72.0 * device.dpi_x)
    render_h_px = max(1.0, eff_h_pt / 72.0 * device.dpi_y)

    if fit == "fit":
        scale = min(device.printable_w_px / render_w_px, device.printable_h_px / render_h_px)
    else:  # "none": 1:1, cropped by the printable area if it doesn't fit
        scale = 1.0

    dest_w = max(1, round(render_w_px * scale))
    dest_h = max(1, round(render_h_px * scale))
    # Centre on the *physical* sheet (not just the printable rect), then
    # translate into printable-area-relative coordinates for drawing.
    dest_x = round((device.physical_w_px - dest_w) / 2) - device.offset_x_px
    dest_y = round((device.physical_h_px - dest_h) / 2) - device.offset_y_px
    return PageTransform(rotate_degrees=90 if rotate else 0, dest_x=dest_x, dest_y=dest_y,
                        dest_w=dest_w, dest_h=dest_h)


def pack_dib_rows(width: int, height: int, stride: int, raw: bytes) -> tuple[bytes, int]:
    """pypdfium2 packs bitmap rows tightly (stride == width * bytes-per-pixel);
    Windows DIBs need each row padded to a 4-byte boundary. Returns
    (padded_bytes, padded_row_size)."""
    row_bytes = width * 3
    padded_row = (row_bytes + 3) & ~3
    if padded_row == stride:
        return bytes(raw), padded_row
    out = bytearray(padded_row * height)
    raw = bytes(raw)
    for y in range(height):
        src_off = y * stride
        dst_off = y * padded_row
        out[dst_off:dst_off + row_bytes] = raw[src_off:src_off + row_bytes]
    return bytes(out), padded_row


def parse_options(options: list[str] | None) -> dict[str, Any]:
    """Per-printer `options` strings -> a settings dict. Unknown keys or bad
    values are logged and ignored rather than failing the whole job - a typo
    in one option should not stop printing."""
    parsed: dict[str, Any] = {"duplex": None, "paper": None, "orientation": "auto", "fit": "fit", "output": None}
    for opt in options or []:
        opt = (opt or "").strip()
        if not opt:
            continue
        key, _, value = opt.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "duplex" and value in ("long", "short"):
            parsed["duplex"] = value
        elif key == "paper" and value:
            parsed["paper"] = value
        elif key == "orientation" and value in ORIENTATIONS:
            parsed["orientation"] = value
        elif key == "fit" and value in FIT_MODES:
            parsed["fit"] = value
        elif key == "output" and value:
            # File-backed queues ("Microsoft Print to PDF", XPS writer) pop a
            # Save dialog unless StartDoc is handed an output path - this is
            # how the end-to-end verification prints without a printer, and
            # how a shop can archive to PDF. Never sensible for a real printer.
            parsed["output"] = value
        else:
            log.warning("ignoring unrecognised printer option %r", opt)
    return parsed


# -- ctypes/gdi32 plumbing --------------------------------------------------

class _DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _gdi32():
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.StartDocW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DOCINFOW)]
    gdi32.StartDocW.restype = ctypes.c_int
    gdi32.StartPage.argtypes = [ctypes.c_void_p]
    gdi32.StartPage.restype = ctypes.c_int
    gdi32.EndPage.argtypes = [ctypes.c_void_p]
    gdi32.EndPage.restype = ctypes.c_int
    gdi32.EndDoc.argtypes = [ctypes.c_void_p]
    gdi32.EndDoc.restype = ctypes.c_int
    gdi32.AbortDoc.argtypes = [ctypes.c_void_p]
    gdi32.AbortDoc.restype = ctypes.c_int
    gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.GetDeviceCaps.restype = ctypes.c_int
    gdi32.StretchDIBits.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.DWORD]
    gdi32.StretchDIBits.restype = ctypes.c_int
    return gdi32


def _win32_modules():
    """Lazy import so this module stays importable on non-Windows (the
    package is `sys_platform == 'win32'`-only there, and CI imports every
    backend module regardless of platform)."""
    try:
        import win32con
        import win32gui
        import win32print
    except ImportError as exc:
        raise PrintError("pywin32 is required for Windows printing (pip install pywin32)") from exc
    return win32con, win32gui, win32print


def _win32_error(gdi32, step: str, driver_name: str = "") -> PrintError:
    err = ctypes.get_last_error()
    message = ctypes.FormatError(err) if err else "unknown error"
    where = f" (driver {driver_name})" if driver_name else ""
    return PrintError(f"{step} failed{where}: {message} [WinError {err}]")


def _device_geometry(gdi32, hdc: int) -> DeviceGeometry:
    def cap(idx: int) -> int:
        return gdi32.GetDeviceCaps(ctypes.c_void_p(hdc), idx)

    printable_w, printable_h = cap(_HORZRES), cap(_VERTRES)
    return DeviceGeometry(
        dpi_x=cap(_LOGPIXELSX) or 300, dpi_y=cap(_LOGPIXELSY) or 300,
        printable_w_px=printable_w, printable_h_px=printable_h,
        physical_w_px=cap(_PHYSICALWIDTH) or printable_w,
        physical_h_px=cap(_PHYSICALHEIGHT) or printable_h,
        offset_x_px=cap(_PHYSICALOFFSETX), offset_y_px=cap(_PHYSICALOFFSETY))


def _apply_devmode_options(devmode, opts: dict[str, Any], *, win32con, win32print,
                           printer_handle, printer_name: str) -> None:
    if opts["duplex"] == "long":
        devmode.Duplex = win32con.DMDUP_VERTICAL
        devmode.Fields |= win32con.DM_DUPLEX
    elif opts["duplex"] == "short":
        devmode.Duplex = win32con.DMDUP_HORIZONTAL
        devmode.Fields |= win32con.DM_DUPLEX
    paper = opts["paper"]
    if paper:
        info = win32print.GetPrinter(printer_handle, 2)
        port = info["pPortName"]
        try:
            names = win32print.DeviceCapabilities(printer_name, port, win32con.DC_PAPERNAMES)
            ids = win32print.DeviceCapabilities(printer_name, port, win32con.DC_PAPERS)
        except Exception as exc:
            raise PrintError(f"could not query paper sizes for {printer_name!r}: {exc}") from exc
        wanted = paper.strip().lower()
        match = next((paper_id for name, paper_id in zip(names, ids) if name.strip().lower() == wanted), None)
        if match is None:
            raise PrintError(f"paper {paper!r} is not supported by {printer_name!r} "
                            f"(available: {', '.join(n.strip() for n in names)})")
        devmode.PaperSize = match
        devmode.Fields |= win32con.DM_PAPERSIZE


def _print_page(gdi32, hdc: int, page, device: DeviceGeometry, opts: dict[str, Any],
                driver_name: str) -> None:
    width_pt, height_pt = page.get_size()
    transform = compute_page_transform(width_pt, height_pt, device,
                                       orientation=opts["orientation"], fit=opts["fit"])
    scale = max(device.dpi_x, device.dpi_y) / 72.0
    bitmap = page.render(scale=scale, rotation=transform.rotate_degrees, fill_color=(255, 255, 255, 255))
    try:
        packed, row_size = pack_dib_rows(bitmap.width, bitmap.height, bitmap.stride, bitmap.buffer)
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), bitmap.width, -bitmap.height, 1, 24,
                                   _BI_RGB, row_size * bitmap.height, 0, 0, 0, 0)
        buf = (ctypes.c_char * len(packed)).from_buffer_copy(packed)
        if gdi32.StartPage(ctypes.c_void_p(hdc)) <= 0:
            raise _win32_error(gdi32, "StartPage", driver_name)
        result = gdi32.StretchDIBits(ctypes.c_void_p(hdc), transform.dest_x, transform.dest_y,
                                     transform.dest_w, transform.dest_h,
                                     0, 0, bitmap.width, bitmap.height,
                                     ctypes.byref(buf), ctypes.byref(header),
                                     _DIB_RGB_COLORS, _SRCCOPY)
        if result == 0:  # GDI_ERROR
            raise _win32_error(gdi32, "StretchDIBits", driver_name)
        if gdi32.EndPage(ctypes.c_void_p(hdc)) <= 0:
            raise _win32_error(gdi32, "EndPage", driver_name)
    finally:
        bitmap.close()


def _print_pdf(printer: str, pdf_path: str, *, copies: int, options: list[str] | None,
              title: str | None, output_path: str | None = None) -> str:
    """The real GDI print job. `output_path` is the `StartDoc` DOCINFO
    `Output` override used by the print-to-PDF integration test (and, on a
    printer that always writes to file, could be used the same way for
    real jobs)."""
    import pypdfium2 as pdfium

    win32con, win32gui, win32print = _win32_modules()
    gdi32 = _gdi32()
    opts = parse_options(options)

    h = win32print.OpenPrinter(printer)
    try:
        info = win32print.GetPrinter(h, 2)
        driver_name = info.get("pDriverName") or printer
        devmode = info.get("pDevMode")
        if devmode is None:
            devmode = win32print.DocumentProperties(0, h, printer, None, None, win32con.DM_OUT_BUFFER)
        _apply_devmode_options(devmode, opts, win32con=win32con, win32print=win32print,
                              printer_handle=h, printer_name=printer)
    finally:
        win32print.ClosePrinter(h)

    hdc = win32gui.CreateDC("WINSPOOL", printer, devmode)
    if not hdc:
        raise _win32_error(gdi32, "CreateDC", driver_name)
    try:
        device = _device_geometry(gdi32, hdc)
        doc_name = (title or "TBHprint job")[:255]
        doc_info = _DOCINFOW(ctypes.sizeof(_DOCINFOW), doc_name, output_path or opts["output"], None, 0)
        job_id = gdi32.StartDocW(ctypes.c_void_p(hdc), ctypes.byref(doc_info))
        if job_id <= 0:
            raise _win32_error(gdi32, "StartDoc", driver_name)
        try:
            doc = pdfium.PdfDocument(pdf_path)
            try:
                if len(doc) == 0:
                    raise PrintError(f"{pdf_path} has no pages")
                for _ in range(max(1, copies)):
                    for page_index in range(len(doc)):
                        _print_page(gdi32, hdc, doc[page_index], device, opts, driver_name)
            finally:
                doc.close()
        except Exception:
            gdi32.AbortDoc(ctypes.c_void_p(hdc))
            raise
        if gdi32.EndDoc(ctypes.c_void_p(hdc)) <= 0:
            raise _win32_error(gdi32, "EndDoc", driver_name)
        return f"{printer}#{job_id}"
    finally:
        win32gui.DeleteDC(hdc)


# -- the small surface the pipeline calls -----------------------------------

def list_printers() -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    _, _, win32print = _win32_modules()
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        printers = win32print.EnumPrinters(flags)
    except Exception as exc:
        raise PrintError(f"could not list printers: {exc}") from exc
    return [p[2] for p in printers]


def get_default_printer() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    _, _, win32print = _win32_modules()
    try:
        return win32print.GetDefaultPrinter()
    except Exception as exc:
        log.debug("no default printer: %s", exc)
        return None


def submit(printer: str, path: str, *, copies: int = 1, options: list[str] | None = None,
          title: str | None = None, timeout: float = 60) -> str:
    """Print on a worker thread with the caller's timeout: a hung driver
    becomes a failed job (PrintError), never a hung agent."""
    if not sys.platform.startswith("win"):
        raise PrintError("the Windows backend was used on a non-Windows platform")
    result: dict[str, Any] = {}

    def worker() -> None:
        try:
            result["job_id"] = _print_pdf(printer, path, copies=copies, options=options, title=title)
        except Exception as exc:  # surfaced on the calling thread below
            result["error"] = exc

    thread = threading.Thread(target=worker, name="tbhprint-win-print", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise PrintError(f"print job on {printer!r} timed out after {timeout}s "
                        "(the driver may be hung)")
    if "error" in result:
        exc = result["error"]
        if isinstance(exc, PrintError):
            raise exc
        raise PrintError(f"print failed: {exc}") from exc
    return result["job_id"]


def cancel(backend_job_id: str, timeout: float = 10) -> None:
    """`backend_job_id` is `"<printer>#<spooler job id>"` (see `submit`) -
    Windows spooler job ids are per-printer, so we need the printer name
    back out of our own opaque id to open the right queue."""
    _, _, win32print = _win32_modules()
    printer_name, sep, job_part = backend_job_id.partition("#")
    if not sep or not job_part.isdigit():
        raise PrintError(f"cannot cancel {backend_job_id!r}: not a Windows spooler job id")
    job_id = int(job_part)
    try:
        h = win32print.OpenPrinter(printer_name)
    except Exception as exc:
        raise PrintError(f"cancel {backend_job_id!r}: could not open printer {printer_name!r}: {exc}") from exc
    try:
        win32print.SetJob(h, job_id, 0, None, win32print.JOB_CONTROL_DELETE)
    except Exception as exc:
        raise PrintError(f"cancel {backend_job_id!r} failed: {exc}") from exc
    finally:
        win32print.ClosePrinter(h)
