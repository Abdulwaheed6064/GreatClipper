"""Turning links the owner sends into authorized direct downloads.

* Google Drive / Dropbox share links → direct-download URLs.
* Social/streaming page links (YouTube, TikTok, Twitch, Kick, Instagram, X) are
  refused: a public post is not a download right and scraping it is out of scope.
* Download hosts must be allowlisted: config.yaml ``source_host_allowlist`` plus
  hosts the owner approved explicitly from Telegram (stored in kv).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from clipper.db import kv_get, kv_set, tx

PLATFORM_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "twitch.tv", "kick.com", "instagram.com",
                  "x.com", "twitter.com", "facebook.com", "fb.watch")

HOST_FAMILIES = {
    "drive.google.com": ["drive.google.com", "drive.usercontent.google.com", "*.googleusercontent.com"],
    "drive.usercontent.google.com": ["drive.usercontent.google.com", "*.googleusercontent.com"],
    "www.dropbox.com": ["www.dropbox.com", "dl.dropboxusercontent.com", "*.dropboxusercontent.com"],
    "dropbox.com": ["www.dropbox.com", "dl.dropboxusercontent.com", "*.dropboxusercontent.com"],
}

_drive_id = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})")
URL_RE = re.compile(r"https?://[^\s<>\"']+")


class LinkRefused(ValueError):
    pass


def find_urls(text: str) -> list[str]:
    return [u.rstrip(").,;]") for u in URL_RE.findall(text or "")]


def is_campaign_link(url: str) -> bool:
    p = urlparse(url)
    return (p.hostname or "").endswith("contentrewards.com") and "/campaigns/" in p.path


def normalize_source_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if p.scheme != "https":
        raise LinkRefused("only https links can be downloaded")
    if any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS):
        raise LinkRefused(
            f"{host} is a social/streaming page, not a download. A public post is not a right to download "
            "and reuse it. Send the campaign's provided files instead (Drive/Dropbox/direct link).")
    if host == "drive.google.com":
        m = _drive_id.search(p.path)
        fid = m.group(1) if m else (parse_qs(p.query).get("id") or [None])[0]
        if not fid:
            raise LinkRefused("that Google Drive link is a folder or page; open the video file and send its link")
        return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    if host in ("www.dropbox.com", "dropbox.com"):
        q = parse_qs(p.query)
        q["dl"] = ["1"]
        return urlunparse(p._replace(netloc="www.dropbox.com", query=urlencode(q, doseq=True)))
    return url.strip()


def hosts_to_allow(url: str) -> list[str]:
    host = (urlparse(url).hostname or "").lower()
    return HOST_FAMILIES.get(host, [host])


def allowlist(conn: sqlite3.Connection, configured: list[str]) -> list[str]:
    return list(configured) + list(kv_get(conn, "extra_source_hosts", []) or [])


def approve_hosts(conn: sqlite3.Connection, hosts: list[str]) -> None:
    with tx(conn):
        cur = set(kv_get(conn, "extra_source_hosts", []) or [])
        kv_set(conn, "extra_source_hosts", sorted(cur | set(hosts)))


def provider_id_for(url: str) -> str:
    return "tg-" + hashlib.sha256(url.encode()).hexdigest()[:16]
