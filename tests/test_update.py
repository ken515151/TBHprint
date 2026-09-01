import hashlib
from unittest import mock

import pytest

from tbhprint import api as apimod
from tbhprint import config as cfgmod
from tbhprint import update as updatemod


def _client():
    server = cfgmod.Server(url="https://shop.techbenchhub.co.uk", token="tok", agent_uuid="u")
    return apimod.Client(server, session=mock.Mock())


class Resp:
    def __init__(self, status=200, body=None, headers=None, chunks=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self._chunks = chunks or [b"binary-content"]

    def json(self):
        return self._body

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- manifest parsing ----------------------------------------------------------

def test_manifest_from_response_no_update():
    assert updatemod.UpdateManifest.from_response({"version": None}) is None


def test_manifest_from_response_parses_fields():
    data = {"version": "0.3.0", "url": "https://x/update/asset.exe", "sha256": "ABCDEF", "notes": "fixes"}
    manifest = updatemod.UpdateManifest.from_response(data)
    assert manifest.version == "0.3.0"
    assert manifest.sha256 == "abcdef"  # lowercased
    assert manifest.notes == "fixes"


def test_manifest_from_response_requires_url_and_sha256():
    with pytest.raises(updatemod.UpdateError):
        updatemod.UpdateManifest.from_response({"version": "0.3.0"})
    with pytest.raises(updatemod.UpdateError):
        updatemod.UpdateManifest.from_response({"version": "0.3.0", "url": "https://x/a"})


def test_check_calls_the_update_endpoint():
    client = _client()
    client.session.get.return_value = Resp(200, {"version": "0.4.0", "url": "https://shop.techbenchhub.co.uk/a",
                                                 "sha256": "aa", "notes": ""})
    manifest = updatemod.check(client, platform_name="windows")
    assert manifest.version == "0.4.0"
    args, kwargs = client.session.get.call_args
    assert args[0] == "https://shop.techbenchhub.co.uk/api/print/v1/update"
    assert kwargs["params"] == {"platform": "windows", "version": mock.ANY}


def test_check_up_to_date_returns_none():
    client = _client()
    client.session.get.return_value = Resp(200, {"version": None})
    assert updatemod.check(client) is None


# -- sha256 verification --------------------------------------------------------

def test_verify_sha256_matches_and_mismatches(tmp_path):
    path = tmp_path / "asset.bin"
    path.write_bytes(b"hello world")
    good = hashlib.sha256(b"hello world").hexdigest()
    assert updatemod.verify_sha256(str(path), good) is True
    assert updatemod.verify_sha256(str(path), "0" * 64) is False
    assert updatemod.verify_sha256(str(path), good.upper()) is True  # case-insensitive


# -- download allowlist ----------------------------------------------------------

def test_download_update_refuses_off_host_url(tmp_path):
    client = _client()
    with pytest.raises(updatemod.UpdateError, match="paired host"):
        updatemod.download_update(client, "https://evil.example.com/a.exe", str(tmp_path / "a.exe"))


def test_download_update_writes_the_file(tmp_path):
    client = _client()
    client.session.get.return_value = Resp(200, chunks=[b"exe-bytes-1", b"exe-bytes-2"])
    dest = tmp_path / "asset.exe"
    updatemod.download_update(client, "https://shop.techbenchhub.co.uk/api/print/v1/update/download/x", str(dest))
    assert dest.read_bytes() == b"exe-bytes-1exe-bytes-2"


# -- install-when-idle: never installs mid-job ----------------------------------

def test_install_when_idle_waits_then_installs():
    manifest = updatemod.UpdateManifest(version="1.0", url="https://x/a", sha256="a")
    active = {"value": True}
    slept = []

    def is_job_active():
        return active["value"]

    def fake_sleep(s):
        slept.append(s)
        active["value"] = False  # becomes idle after one poll

    with mock.patch("tbhprint.update._install_windows") as install, \
         mock.patch("tbhprint.update.sys.platform", "win32"):
        result = updatemod.install_when_idle(manifest, "/tmp/x.exe", is_job_active=is_job_active,
                                             max_wait_s=100, poll_s=1, sleep=fake_sleep)
    assert result is True
    assert slept == [1]
    install.assert_called_once_with("/tmp/x.exe")


def test_install_when_idle_gives_up_after_max_wait_without_installing():
    manifest = updatemod.UpdateManifest(version="1.0", url="https://x/a", sha256="a")

    with mock.patch("tbhprint.update._install_windows") as install, \
         mock.patch("tbhprint.update._install_linux") as install_linux:
        result = updatemod.install_when_idle(manifest, "/tmp/x.exe", is_job_active=lambda: True,
                                             max_wait_s=2, poll_s=1, sleep=lambda s: None)
    assert result is False
    install.assert_not_called()
    install_linux.assert_not_called()


def test_install_linux_writes_deb_checksum_and_touches_requested(tmp_path):
    manifest = updatemod.UpdateManifest(version="1.0", url="https://x/a.deb", sha256="deadbeef")
    src = tmp_path / "downloaded.deb"
    src.write_bytes(b"deb-contents")
    update_dir = tmp_path / "update"
    updatemod._install_linux(manifest, str(src), str(update_dir))
    dest = update_dir / "downloaded.deb"
    assert dest.read_bytes() == b"deb-contents"
    assert (update_dir / "downloaded.deb.sha256").read_text().startswith("deadbeef")
    assert (update_dir / "requested").exists()


# -- the full cycle: check -> download -> verify -> install ---------------------

def test_check_and_install_refuses_bad_sha256_and_does_not_install(tmp_path):
    client = _client()
    client.session.get.side_effect = [
        Resp(200, {"version": "2.0", "url": "https://shop.techbenchhub.co.uk/dl/x.exe",
                   "sha256": "0" * 64, "notes": ""}),
        Resp(200, chunks=[b"not-the-right-bytes"]),
    ]
    with mock.patch("tbhprint.update.install_when_idle") as install:
        manifest = updatemod.check_and_install(client, str(tmp_path), is_job_active=lambda: False)
    assert manifest.version == "2.0"
    install.assert_not_called()
    # The bad download should not be left lying around.
    assert not any(tmp_path.iterdir())


def test_check_and_install_happy_path_calls_install_when_idle(tmp_path):
    payload = b"the-installer-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    client = _client()
    client.session.get.side_effect = [
        Resp(200, {"version": "2.0", "url": "https://shop.techbenchhub.co.uk/dl/x.exe",
                   "sha256": sha, "notes": "bugfixes"}),
        Resp(200, chunks=[payload]),
    ]
    installing = []
    with mock.patch("tbhprint.update.install_when_idle") as install:
        manifest = updatemod.check_and_install(client, str(tmp_path), is_job_active=lambda: False,
                                                on_installing=installing.append)
    assert manifest.version == "2.0"
    install.assert_called_once()
    assert installing and installing[0].version == "2.0"


def test_check_and_install_returns_none_when_up_to_date(tmp_path):
    client = _client()
    client.session.get.return_value = Resp(200, {"version": None})
    assert updatemod.check_and_install(client, str(tmp_path), is_job_active=lambda: False) is None


def test_check_and_install_logs_and_returns_none_on_check_failure(tmp_path):
    client = _client()
    client.session.get.return_value = Resp(500, {"message": "server error"})
    assert updatemod.check_and_install(client, str(tmp_path), is_job_active=lambda: False) is None
