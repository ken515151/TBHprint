import subprocess
from unittest import mock

import pytest

from tbhprint.backends import PrintError, get_backend
from tbhprint.backends import cups, windows


def test_get_backend_by_name_and_auto():
    assert get_backend("cups") is cups
    assert get_backend("windows") is windows
    with mock.patch("tbhprint.backends.sys.platform", "win32"):
        assert get_backend("auto") is windows
    with mock.patch("tbhprint.backends.sys.platform", "linux"):
        assert get_backend("auto") is cups
    with pytest.raises(ValueError):
        get_backend("laser-cat")


def test_cups_submit_parses_request_id():
    proc = subprocess.CompletedProcess(["lp"], 0, stdout="request id is HP_LaserJet-42 (1 file(s))\n", stderr="")
    with mock.patch("tbhprint.backends.cups._run", return_value=proc) as run:
        assert cups.submit("HP_LaserJet", "/tmp/x.pdf", copies=2, options=["sides=one-sided"], title="Label #1") == "HP_LaserJet-42"
    argv = run.call_args.args[0]
    assert argv[:5] == ["lp", "-d", "HP_LaserJet", "-n", "2"]
    assert "-o" in argv and "sides=one-sided" in argv
    proc = subprocess.CompletedProcess(["lp"], 1, stdout="", stderr="lp: The printer or class does not exist.")
    with mock.patch("tbhprint.backends.cups._run", return_value=proc):
        with pytest.raises(PrintError, match="does not exist"):
            cups.submit("nope", "/tmp/x.pdf")


def test_cups_list_printers_handles_no_destinations():
    proc = subprocess.CompletedProcess(["lpstat"], 1, stdout="", stderr="lpstat: No destinations added.")
    with mock.patch("tbhprint.backends.cups._run", return_value=proc):
        assert cups.list_printers() == []
    proc = subprocess.CompletedProcess(["lpstat"], 0, stdout="HP accepting requests since Mon\nQL not accepting\n", stderr="")
    with mock.patch("tbhprint.backends.cups._run", return_value=proc):
        assert cups.list_printers() == ["HP"]


# -- windows: parse_options ---------------------------------------------------

def test_parse_options_known_keys():
    parsed = windows.parse_options(["duplex=long", "paper=A4", "orientation=landscape", "fit=none"])
    assert parsed == {"duplex": "long", "paper": "A4", "orientation": "landscape", "fit": "none"}


def test_parse_options_defaults_and_ignores_junk():
    parsed = windows.parse_options(["duplex=sideways", "bogus", "", None, "fit=maybe"])
    assert parsed == {"duplex": None, "paper": None, "orientation": "auto", "fit": "fit"}


def test_parse_options_empty_list():
    assert windows.parse_options(None) == {"duplex": None, "paper": None, "orientation": "auto", "fit": "fit"}


# -- windows: pack_dib_rows ----------------------------------------------------

def test_pack_dib_rows_no_padding_needed():
    # width=4 -> row_bytes=12, already a multiple of 4: no repacking.
    raw = bytes(range(12)) * 3  # 3 rows of 12 bytes
    packed, row_size = windows.pack_dib_rows(4, 3, 12, raw)
    assert row_size == 12
    assert packed == raw


def test_pack_dib_rows_adds_padding():
    # width=5 -> row_bytes=15, padded to 16.
    width, height, stride = 5, 2, 15
    raw = bytes(range(stride)) + bytes(range(100, 100 + stride))
    packed, row_size = windows.pack_dib_rows(width, height, stride, raw)
    assert row_size == 16
    assert len(packed) == row_size * height
    assert packed[0:15] == raw[0:15]
    assert packed[15:16] == b"\x00"
    assert packed[16:31] == raw[15:30]


# -- windows: compute_page_transform (the pure fit/rotate/scale maths) --------

def _device(**overrides):
    base = dict(dpi_x=300.0, dpi_y=300.0, printable_w_px=2550, printable_h_px=3300,
               physical_w_px=2550, physical_h_px=3300, offset_x_px=0, offset_y_px=0)
    base.update(overrides)
    return windows.DeviceGeometry(**base)


def test_compute_page_transform_portrait_page_no_rotation_needed():
    # US Letter page (612x792pt) on a portrait printable area: no rotation.
    device = _device()
    t = windows.compute_page_transform(612, 792, device, orientation="auto", fit="fit")
    assert t.rotate_degrees == 0
    assert t.dest_w <= device.printable_w_px
    assert t.dest_h <= device.printable_h_px
    # fit-to-page should use (most of) the printable width or height
    assert t.dest_w == device.printable_w_px or t.dest_h == device.printable_h_px


def test_compute_page_transform_auto_rotates_landscape_page_on_portrait_device():
    device = _device()  # portrait printable area
    t = windows.compute_page_transform(792, 612, device, orientation="auto", fit="fit")  # landscape page
    assert t.rotate_degrees == 90


def test_compute_page_transform_orientation_forces_rotation():
    device = _device()
    # A portrait page forced to landscape output must rotate.
    t = windows.compute_page_transform(612, 792, device, orientation="landscape", fit="fit")
    assert t.rotate_degrees == 90
    # And a landscape page forced to portrait must also rotate.
    t2 = windows.compute_page_transform(792, 612, device, orientation="portrait", fit="fit")
    assert t2.rotate_degrees == 90


def test_compute_page_transform_fit_none_is_1to1_at_device_dpi():
    device = _device(printable_w_px=10000, printable_h_px=10000, physical_w_px=10000, physical_h_px=10000)
    t = windows.compute_page_transform(72, 72, device, orientation="portrait", fit="none")
    # 1 inch square page at 300 dpi -> 300x300 device pixels, unscaled.
    assert t.dest_w == 300
    assert t.dest_h == 300


def test_compute_page_transform_centres_using_physical_offset():
    # A physical page bigger than the printable area (margins), offset by 100px.
    device = _device(printable_w_px=2350, printable_h_px=3100, physical_w_px=2550, physical_h_px=3300,
                     offset_x_px=100, offset_y_px=100)
    t = windows.compute_page_transform(612, 792, device, orientation="portrait", fit="none")
    # Centred on the *physical* page then translated into printable-relative coords.
    expected_x = round((2550 - t.dest_w) / 2) - 100
    expected_y = round((3300 - t.dest_h) / 2) - 100
    assert t.dest_x == expected_x
    assert t.dest_y == expected_y


def test_compute_page_transform_rejects_bad_input():
    device = _device()
    with pytest.raises(ValueError):
        windows.compute_page_transform(0, 100, device)
    with pytest.raises(ValueError):
        windows.compute_page_transform(100, 100, device, orientation="sideways")
    with pytest.raises(ValueError):
        windows.compute_page_transform(100, 100, device, fit="shrink")


# -- windows: submit/cancel plumbing (mocked pywin32/pypdfium2) ---------------

def test_windows_submit_returns_printer_hash_job_id_and_runs_off_thread():
    with mock.patch("tbhprint.backends.windows.sys.platform", "win32"), \
         mock.patch("tbhprint.backends.windows._print_pdf", return_value="HP LaserJet#7") as fake_print:
        job_id = windows.submit("HP LaserJet", "x.pdf", copies=2, options=["duplex=long"], title="t")
    assert job_id == "HP LaserJet#7"
    fake_print.assert_called_once()
    assert fake_print.call_args.kwargs["copies"] == 2


def test_windows_submit_surfaces_print_error():
    with mock.patch("tbhprint.backends.windows.sys.platform", "win32"), \
         mock.patch("tbhprint.backends.windows._print_pdf", side_effect=PrintError("driver exploded")):
        with pytest.raises(PrintError, match="driver exploded"):
            windows.submit("HP LaserJet", "x.pdf")


def test_windows_submit_times_out_a_hung_driver():
    def hang(*a, **k):
        import time
        time.sleep(5)
        return "never"

    with mock.patch("tbhprint.backends.windows.sys.platform", "win32"), \
         mock.patch("tbhprint.backends.windows._print_pdf", side_effect=hang):
        with pytest.raises(PrintError, match="timed out"):
            windows.submit("HP LaserJet", "x.pdf", timeout=0.2)


def test_windows_submit_off_windows_is_an_error():
    with mock.patch("tbhprint.backends.windows.sys.platform", "linux"):
        with pytest.raises(PrintError):
            windows.submit("P", "x.pdf")


def test_windows_cancel_parses_printer_and_job_id():
    win32print = mock.Mock()
    win32print.JOB_CONTROL_DELETE = 5
    win32print.OpenPrinter.return_value = "hprinter"
    with mock.patch("tbhprint.backends.windows._win32_modules", return_value=(None, None, win32print)):
        windows.cancel("HP LaserJet#7")
    win32print.OpenPrinter.assert_called_once_with("HP LaserJet")
    win32print.SetJob.assert_called_once_with("hprinter", 7, 0, None, 5)
    win32print.ClosePrinter.assert_called_once_with("hprinter")


def test_windows_cancel_rejects_unrecognised_id():
    with pytest.raises(PrintError, match="not a Windows spooler job id"):
        windows.cancel("not-a-real-id")
    with pytest.raises(PrintError, match="not a Windows spooler job id"):
        windows.cancel("printer-with-no-hash")
