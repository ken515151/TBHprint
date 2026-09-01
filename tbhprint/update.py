"""Auto-update: ask our own TechBenchHub server for a newer agent build,
download it, verify its checksum, then hand off to the OS installer.

Never GitHub - the repo is private and the agent only ever holds one
credential (its own bearer token), so it asks the server it is paired
with, over the same `api.Client` machinery (host allowlist, TLS, bearer
auth) used for document downloads.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__, api as apimod

log = logging.getLogger("tbhprint.update")

CHECK_INTERVAL_S = 6 * 3600
INSTALL_WAIT_MAX_S = 10 * 60
INSTALL_POLL_S = 15
DEFAULT_LINUX_UPDATE_DIR = "/var/lib/tbhprint/update"


class UpdateError(RuntimeError):
    """A manifest, download or install step failed."""


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    url: str
    sha256: str
    notes: str = ""

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> UpdateManifest | None:
        version = data.get("version")
        if not version:
            return None
        url, sha256 = data.get("url"), data.get("sha256")
        if not isinstance(url, str) or not url or not isinstance(sha256, str) or not sha256:
            raise UpdateError(f"update manifest for {version} is missing url/sha256")
        return cls(version=str(version), url=url, sha256=sha256.lower(), notes=str(data.get("notes") or ""))


# -- server call -------------------------------------------------------------

def check(client: apimod.Client, *, platform_name: str | None = None) -> UpdateManifest | None:
    """One `GET .../update` call. A reachability failure (requests
    exceptions, `ApiError`/`AuthError`) is left to the caller - it already
    knows how to log and back off, same as any other server call."""
    platform_name = platform_name or apimod.platform_name()
    resp = client.session.get(client.server.api("update"),
                              params={"platform": platform_name, "version": __version__},
                              headers=client._headers(), timeout=(10, 30), verify=True)
    data = apimod._check(resp, "update check")
    return UpdateManifest.from_response(data)


def download_update(client: apimod.Client, url: str, dest: str, *, timeout_s: int = 600) -> str:
    """Same allowlist/TLS discipline as `Client.download` (same-host,
    redirects only to the same host) but for an arbitrary binary asset,
    not a PDF."""
    if not client.allowed_document_host(url):
        raise UpdateError(f"refusing update URL outside the paired host: {url[:80]}")
    for _ in range(5):
        resp = client.session.get(url, stream=True, headers=client._headers(),
                                  timeout=(10, timeout_s), verify=True, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            resp.close()
            if not client.allowed_document_host(location):
                raise UpdateError("refusing update redirect outside the paired host")
            url = location
            continue
        break
    else:
        raise UpdateError("too many redirects")
    with resp:
        if resp.status_code >= 400:
            raise UpdateError(f"update download: HTTP {resp.status_code}")
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
    return dest


def verify_sha256(path: str, expected: str) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == expected.strip().lower()


# -- install ------------------------------------------------------------------

def install_when_idle(manifest: UpdateManifest, downloaded_path: str, *, is_job_active: Callable[[], bool],
                      linux_update_dir: str = DEFAULT_LINUX_UPDATE_DIR,
                      max_wait_s: float = INSTALL_WAIT_MAX_S, poll_s: float = INSTALL_POLL_S,
                      sleep: Callable[[float], None] = time.sleep) -> bool:
    """Wait (bounded) for no job to be active, then hand off to the OS
    installer. A shop mid-print is never interrupted: if the wait times
    out we just log and return False so the next 6-hourly cycle tries
    again (the download is already verified and sitting in the state dir,
    so nothing is re-fetched)."""
    waited = 0.0
    while is_job_active():
        if waited >= max_wait_s:
            log.info("update %s ready but a job is still active after %.0fs - will retry next cycle",
                     manifest.version, max_wait_s)
            return False
        sleep(poll_s)
        waited += poll_s
    if sys.platform.startswith("win"):
        _install_windows(downloaded_path)
    else:
        _install_linux(manifest, downloaded_path, linux_update_dir)
    return True


def _install_windows(installer_path: str) -> None:
    # /FORCECLOSEAPPLICATIONS: the Restart Manager cannot close pythonw
    # gracefully (Tk windows ignore the shutdown message), and a silent
    # setup's suppressed Abort/Retry/Ignore box defaults to ABORT - the
    # whole update rolled back (found live 2026-09-01). Force-terminating
    # our own tray/agent is safe: installs only run when no job is active,
    # and the installer's [Run] entry starts the tray again.
    argv = [installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
           "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    log.info("running installer detached: %s", " ".join(argv))
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(argv, close_fds=True, creationflags=detached | new_group)


def _install_linux(manifest: UpdateManifest, deb_path: str, update_dir: str) -> None:
    """Unprivileged: write the .deb + its checksum and touch `requested`.
    A root path unit (`tbhprint-update.path`, packaging territory) does
    the actual `apt-get install` and restart."""
    os.makedirs(update_dir, exist_ok=True)
    dest_deb = os.path.join(update_dir, os.path.basename(deb_path))
    if os.path.abspath(dest_deb) != os.path.abspath(deb_path):
        with open(deb_path, "rb") as src, open(dest_deb, "wb") as dst:
            dst.write(src.read())
    with open(dest_deb + ".sha256", "w", encoding="utf-8") as fh:
        fh.write(f"{manifest.sha256}  {os.path.basename(dest_deb)}\n")
    open(os.path.join(update_dir, "requested"), "w", encoding="utf-8").close()
    log.info("wrote %s + .sha256 and touched 'requested' for the update path unit", dest_deb)


# -- one full cycle ------------------------------------------------------------

def check_and_install(client: apimod.Client, state_dir: str, *, is_job_active: Callable[[], bool],
                      linux_update_dir: str = DEFAULT_LINUX_UPDATE_DIR,
                      on_installing: Callable[[UpdateManifest], None] | None = None) -> UpdateManifest | None:
    """check -> download -> verify -> install-when-idle. Never raises for a
    network/manifest failure - those are logged and treated as "nothing
    this cycle" so the periodic timer just tries again. Returns the
    manifest whenever a newer version was found (whether or not the
    install actually ran this cycle), None when already up to date."""
    try:
        manifest = check(client, platform_name=apimod.platform_name())
    except (apimod.ApiError, UpdateError) as exc:
        log.warning("update check failed: %s", exc)
        return None
    except Exception:
        log.exception("update check failed unexpectedly")
        return None
    if manifest is None:
        log.debug("no update available (agent is %s)", __version__)
        return None
    log.info("update %s available (currently %s)", manifest.version, __version__)
    os.makedirs(state_dir, exist_ok=True)
    filename = os.path.basename(manifest.url.split("?")[0]) or f"tbhprint-{manifest.version}.bin"
    dest = os.path.join(state_dir, filename)
    try:
        download_update(client, manifest.url, dest, timeout_s=600)
    except Exception as exc:
        log.error("update %s download failed: %s", manifest.version, exc)
        with contextlib.suppress(OSError):
            os.remove(dest)
        return manifest
    if not verify_sha256(dest, manifest.sha256):
        log.error("update %s failed sha256 verification - refusing to install", manifest.version)
        with contextlib.suppress(OSError):
            os.remove(dest)
        return manifest
    log.info("update %s downloaded and verified", manifest.version)
    if on_installing:
        with contextlib.suppress(Exception):
            on_installing(manifest)
    try:
        install_when_idle(manifest, dest, is_job_active=is_job_active, linux_update_dir=linux_update_dir)
    except Exception:
        log.exception("update %s install failed", manifest.version)
    return manifest
