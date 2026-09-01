from unittest import mock

import pytest

from tbrprint import api as apimod
from tbrprint import config as cfgmod


def server(url="https://shop.techbenchhub.co.uk"):
    return cfgmod.Server(url=url, token="tok", agent_uuid="u")


class Resp:
    def __init__(self, status=200, body=None, headers=None, chunks=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {"Content-Type": "application/pdf"}
        self._chunks = chunks or [b"%PDF-1.4 fake"]

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_allowed_document_host():
    client = apimod.Client(server())
    assert client.allowed_document_host("https://shop.techbenchhub.co.uk/api/print/v1/jobs/x/document")
    assert not client.allowed_document_host("https://other.techbenchhub.co.uk/x")
    assert not client.allowed_document_host("http://shop.techbenchhub.co.uk/x")  # plaintext to an https server
    dev = apimod.Client(server("http://demo.techbenchhub.test"))
    assert dev.allowed_document_host("http://demo.techbenchhub.test/api/print/v1/jobs/x/document")


def test_headers_carry_bearer_and_version():
    headers = apimod._headers("tok")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-TBRprint-Version"]
    assert headers["X-TBRprint-Platform"] in ("windows", "linux", "macos")


def test_list_jobs_and_ack_and_auth_errors():
    session = mock.Mock()
    session.get.return_value = Resp(200, {"jobs": [{"uuid": "a"}, "junk"]})
    client = apimod.Client(server(), session=session)
    assert client.list_jobs("2026-09-01T00:00:00Z") == [{"uuid": "a"}]
    assert session.get.call_args.kwargs["params"] == {"since": "2026-09-01T00:00:00Z"}

    session.post.return_value = Resp(200, {"job": {"status": "delivered"}, "applied": True})
    assert client.ack("a", "received")["applied"] is True
    assert session.post.call_args.kwargs["json"] == {"state": "received"}

    session.post.return_value = Resp(401, {"message": "Unauthenticated."})
    with pytest.raises(apimod.AuthError):
        client.ack("a", "printed")

    session.get.return_value = Resp(429, {})
    with pytest.raises(apimod.ApiError) as exc:
        client.list_jobs()
    assert exc.value.status == 429 and exc.value.retryable


def test_channel_auth_returns_auth_string():
    session = mock.Mock()
    session.post.return_value = Resp(200, {"auth": "key:sig"})
    client = apimod.Client(server(), session=session)
    assert client.channel_auth("1.1", "private-x") == "key:sig"
    assert session.post.call_args.kwargs["json"] == {"socket_id": "1.1", "channel_name": "private-x"}


def test_download_checks(tmp_path):
    session = mock.Mock()
    client = apimod.Client(server(), session=session)
    url = "https://shop.techbenchhub.co.uk/api/print/v1/jobs/x/document"
    dest = str(tmp_path / "x.pdf")

    session.get.return_value = Resp()
    assert client.download(url, dest) == dest
    assert open(dest, "rb").read().startswith(b"%PDF")

    session.get.return_value = Resp(headers={"Content-Type": "text/html"})
    with pytest.raises(apimod.DownloadError, match="content-type"):
        client.download(url, dest)

    session.get.return_value = Resp(chunks=[b"<html>not a pdf"])
    with pytest.raises(apimod.DownloadError, match="not a PDF"):
        client.download(url, dest)

    session.get.return_value = Resp(410, {"error": "render_failed", "message": "record gone"})
    with pytest.raises(apimod.DownloadError, match="record gone"):
        client.download(url, dest)

    session.get.return_value = Resp(302, headers={"Location": "https://evil.example.com/x"})
    with pytest.raises(apimod.DownloadError, match="redirect"):
        client.download(url, dest)

    with pytest.raises(apimod.DownloadError, match="outside the paired host"):
        client.download("https://evil.example.com/x.pdf", dest)


def test_pair_maps_422_to_final_error():
    with mock.patch("tbrprint.api.requests.post", return_value=Resp(422, {"error": "unknown_code", "message": "expired"})):
        with pytest.raises(apimod.ApiError) as exc:
            apimod.pair("https://shop.techbenchhub.co.uk", "abcd", "Desk")
    assert not exc.value.retryable and "expired" in str(exc.value)
    with mock.patch("tbrprint.api.requests.post", return_value=Resp(201, {"agent_uuid": "u", "token": "t"})) as post:
        assert apimod.pair("https://shop.techbenchhub.co.uk/", "abcd2345", "Desk")["token"] == "t"
    assert post.call_args.args[0] == "https://shop.techbenchhub.co.uk/api/print/v1/pair"
    assert post.call_args.kwargs["json"]["code"] == "ABCD2345"
