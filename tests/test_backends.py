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


def test_windows_print_settings_and_sumatra_command(tmp_path):
    assert windows.build_print_settings(2, ["fit", "paper=4"]) == "2x,fit,paper=4"
    fake_exe = tmp_path / "SumatraPDF.exe"
    fake_exe.write_bytes(b"")
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with mock.patch("tbhprint.backends.windows.find_sumatra", return_value=str(fake_exe)), \
         mock.patch("tbhprint.backends.windows.subprocess.run", return_value=completed) as run:
        job_id = windows.submit("Brother QL-800", str(tmp_path / "a.pdf"), copies=3, options=["fit"])
    argv = run.call_args.args[0]
    assert argv[0] == str(fake_exe)
    assert argv[1:5] == ["-print-to", "Brother QL-800", "-print-settings", "3x,fit"]
    assert "-silent" in argv
    assert job_id.startswith("sumatra-")


def test_windows_sumatra_failure_is_a_print_error(tmp_path):
    completed = subprocess.CompletedProcess([], 1, stdout="", stderr="Couldn't print")
    with mock.patch("tbhprint.backends.windows.find_sumatra", return_value="S.exe"), \
         mock.patch("tbhprint.backends.windows.subprocess.run", return_value=completed):
        with pytest.raises(PrintError, match="Couldn't print"):
            windows.submit("P", "x.pdf")


def test_windows_without_sumatra_off_windows_is_an_error():
    with mock.patch("tbhprint.backends.windows.find_sumatra", return_value=None), \
         mock.patch("tbhprint.backends.windows.sys.platform", "linux"):
        with pytest.raises(PrintError):
            windows.submit("P", "x.pdf")
