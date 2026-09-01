"""REST client for the TechBenchHub print API (docs/PROTOCOL.md).

  POST /api/print/v1/pair                       one-time code -> token (unauthenticated)
  GET  /api/print/v1/jobs?since=                open jobs for this agent (catch-up)
  POST /api/print/v1/jobs/{uuid}/ack            received | printed | failed(error)
  GET  /api/print/v1/jobs/{uuid}/document       the PDF, rendered on demand
  POST /api/print/v1/broadcasting/auth          Pusher-protocol private channel auth

Every authenticated call carries `Authorization: Bearer <token>` plus the
agent version/platform headers the server uses for its online lamp.
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import Any
from urllib.parse import urlsplit

import requests

from . import __version__
from .config import Server

log = logging.getLogger("tbrprint.api")

_TIMEOUT = (10, 30)
MAX_PDF_BYTES = 25 * 1024 * 1024
_ACCEPTED_CONTENT_TYPES = ("application/pdf", "application/octet-stream", "binary/octet-stream")


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AuthError(ApiError):
    """Token rejected or agent revoked - do not retry without user action."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message, status, retryable=False)


class DownloadError(RuntimeError):
    """The document could not be fetched or is not an acceptable PDF."""


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": f"TBRprint/{__version__} ({platform.system()} {platform.release()})",
        "Accept": "application/json",
        "X-TBRprint-Version": __version__,
        "X-TBRprint-Platform": platform_name(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check(resp: requests.Response, what: str) -> dict[str, Any]:
    if resp.status_code in (401, 403):
        raise AuthError(f"{what}: authentication rejected (HTTP {resp.status_code}) - "
                        "the agent may have been revoked; pair again", resp.status_code)
    if resp.status_code == 429:
        raise ApiError(f"{what}: rate limited (HTTP 429)", 429)
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error") or ""
        except ValueError:
            pass
        retryable = resp.status_code >= 500
        raise ApiError(f"{what}: HTTP {resp.status_code} {detail}".strip(), resp.status_code, retryable)
    try:
        data = resp.json()
    except ValueError as exc:
        raise ApiError(f"{what}: response was not JSON", resp.status_code) from exc
    if not isinstance(data, dict):
        raise ApiError(f"{what}: unexpected response shape", resp.status_code)
    return data


def pair(server_url: str, code: str, name: str) -> dict[str, Any]:
    """Redeem a pairing code. Returns the server's pairing payload:
    {agent_uuid, name, token, tenant, channel, reverb: {key, host, port, scheme}}."""
    url = f"{server_url.rstrip('/')}/api/print/v1/pair"
    resp = requests.post(url, json={"code": code.strip().upper(), "name": name,
                                    "platform": platform_name(), "version": __version__},
                         headers=_headers(), timeout=_TIMEOUT, verify=True)
    if resp.status_code == 422:
        try:
            message = resp.json().get("message") or "pairing code refused"
        except ValueError:
            message = "pairing code refused"
        raise ApiError(f"pair: {message}", 422, retryable=False)
    return _check(resp, "pair")


class Client:
    def __init__(self, server: Server, session: requests.Session | None = None):
        self.server = server
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return _headers(self.server.token)

    def list_jobs(self, since_iso: str | None = None) -> list[dict[str, Any]]:
        params = {"since": since_iso} if since_iso else {}
        resp = self.session.get(self.server.api("jobs"), params=params, headers=self._headers(),
                                timeout=_TIMEOUT, verify=True)
        data = _check(resp, "jobs")
        jobs = data.get("jobs")
        return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []

    def ack(self, job_uuid: str, state: str, error: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"state": state}
        if error:
            body["error"] = error[:2000]
        resp = self.session.post(self.server.api(f"jobs/{job_uuid}/ack"), json=body,
                                 headers=self._headers(), timeout=_TIMEOUT, verify=True)
        return _check(resp, f"ack {state}")

    def channel_auth(self, socket_id: str, channel_name: str) -> str:
        """Pusher private-channel auth string ("key:signature") for the websocket subscribe."""
        resp = self.session.post(self.server.api("broadcasting/auth"),
                                 json={"socket_id": socket_id, "channel_name": channel_name},
                                 headers=self._headers(), timeout=_TIMEOUT, verify=True)
        data = _check(resp, "channel auth")
        auth = data.get("auth")
        if not isinstance(auth, str) or not auth:
            raise ApiError("channel auth: no auth string in response")
        return auth

    def allowed_document_host(self, url: str) -> bool:
        """The channel is untrusted input: only fetch from the paired host, over HTTPS
        (plain HTTP tolerated only when the server itself was paired over HTTP - local dev)."""
        parts = urlsplit(url)
        server_scheme = urlsplit(self.server.url).scheme
        if parts.scheme != "https" and not (parts.scheme == "http" and server_scheme == "http"):
            return False
        return (parts.hostname or "").lower() == self.server.host

    def download(self, url: str, dest: str, *, timeout_s: int = 120,
                 cancelled=None) -> str:
        """Stream a job's PDF to `dest`. Refuses non-allowlisted hosts, non-PDF
        content and oversize bodies; follows redirects only to the same host."""
        if not self.allowed_document_host(url):
            raise DownloadError(f"refusing document URL outside the paired host: {url[:80]}")
        for _ in range(5):
            resp = self.session.get(url, stream=True, headers=self._headers(),
                                    timeout=(10, timeout_s), verify=True, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                resp.close()
                if not self.allowed_document_host(location):
                    raise DownloadError("refusing redirect outside the paired host")
                url = location
                continue
            break
        else:
            raise DownloadError("too many redirects")
        with resp:
            if resp.status_code in (401, 403):
                raise AuthError("document: authentication rejected", resp.status_code)
            if resp.status_code == 410:
                raise DownloadError(_message(resp) or "the server cannot render this document any more")
            if resp.status_code >= 400:
                raise DownloadError(f"document: HTTP {resp.status_code} {_message(resp)}".strip())
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype and ctype not in _ACCEPTED_CONTENT_TYPES:
                raise DownloadError(f"unexpected content-type {ctype!r}")
            size = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if cancelled is not None and cancelled.is_set():
                        raise DownloadError("cancelled")
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise DownloadError("file exceeds 25 MB limit")
                    fh.write(chunk)
        if size == 0:
            raise DownloadError("empty file")
        with open(dest, "rb") as fh:
            if fh.read(4) != b"%PDF":
                raise DownloadError("document is not a PDF")
        return dest


def _message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("error") or "")
    except ValueError:
        return ""
