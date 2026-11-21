"""Remote URL safety: HTTPS only, host allowlist, no private/local addresses,
every redirect re-validated, size caps enforced while streaming."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from clipper.jobs.queue import HoldError, PermanentError, TransientError

MAX_REDIRECTS = 5
ALLOWED_MEDIA_TYPES = ("video/", "application/octet-stream", "binary/octet-stream", "application/binary",
                       "application/x-download", "application/force-download")


def host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower().strip()
        if not entry:
            continue
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _resolve_public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransientError(f"DNS lookup failed for {host}: {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise PermanentError(f"{host} resolves to non-public address {ip}; refused")


def validate_url(url: str, allowlist: list[str], *, resolve: bool = True) -> str:
    p = urlparse(url)
    if p.scheme != "https":
        raise PermanentError(f"only https URLs are accepted: {url}")
    if not p.hostname:
        raise PermanentError(f"URL has no host: {url}")
    if p.username or p.password:
        raise PermanentError("credentials in URLs are not accepted")
    try:
        ipaddress.ip_address(p.hostname)
        raise PermanentError("literal IP addresses are not accepted; use an allowlisted hostname")
    except ValueError:
        pass
    if not host_allowed(p.hostname, allowlist):
        raise HoldError(f"host {p.hostname} is not in source_host_allowlist")
    if resolve:
        _resolve_public(p.hostname)
    return url


def _follow(client: httpx.Client, method: str, url: str, allowlist: list[str], resolve: bool, **kw):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(current, allowlist, resolve=resolve)
        req = client.build_request(method, current, **kw)
        resp = client.send(req, stream=True, follow_redirects=False)
        if resp.is_redirect:
            loc = resp.headers.get("location")
            resp.close()
            if not loc:
                raise PermanentError("redirect without location")
            current = urljoin(current, loc)
            continue
        return resp
    raise PermanentError("too many redirects")


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code in (429,) or resp.status_code >= 500:
        ra = resp.headers.get("retry-after")
        raise TransientError(f"HTTP {resp.status_code}", float(ra) if ra and ra.isdigit() else None)
    if resp.status_code >= 400:
        raise PermanentError(f"HTTP {resp.status_code} fetching media")


def fetch_json(client: httpx.Client, url: str, allowlist: list[str], *, resolve: bool = True,
               max_bytes: int = 2_000_000) -> object:
    try:
        resp = _follow(client, "GET", url, allowlist, resolve)
    except httpx.TransportError as exc:
        raise TransientError(f"feed fetch failed: {exc}")
    try:
        _raise_for(resp)
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise PermanentError("feed response too large")
    finally:
        resp.close()
    import json

    try:
        return json.loads(body)
    except ValueError as exc:
        raise PermanentError(f"feed is not valid JSON: {exc}")


def download(client: httpx.Client, url: str, dest: Path, allowlist: list[str], *, max_bytes: int,
             resolve: bool = True) -> tuple[str, int]:
    """Stream to ``dest`` (via a .part file). Returns (sha256, bytes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        resp = _follow(client, "GET", url, allowlist, resolve)
    except httpx.TransportError as exc:
        raise TransientError(f"download failed: {exc}")
    h = hashlib.sha256()
    size = 0
    try:
        _raise_for(resp)
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith(ALLOWED_MEDIA_TYPES):
            if ctype == "text/html":
                raise PermanentError("the link returned a web page, not the video file (check it is shared as "
                                     "'anyone with the link', or send a direct download link)")
            raise PermanentError(f"unexpected content-type {ctype!r}")
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HoldError(f"source is {int(declared)} bytes; cap is {max_bytes}")
        try:
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HoldError(f"source exceeded {max_bytes} bytes during download")
                    h.update(chunk)
                    fh.write(chunk)
        except httpx.TransportError as exc:
            part.unlink(missing_ok=True)
            raise TransientError(f"download interrupted: {exc}")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    finally:
        resp.close()
    part.replace(dest)
    return h.hexdigest(), size


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
