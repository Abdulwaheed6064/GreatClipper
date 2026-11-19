"""Owner-only alerts through a Telegram bot.

Every attempt is logged. If the most recent delivery failed (or no test has
ever succeeded), ``alerts_healthy`` is False and deadline-sensitive campaign
publishing is blocked. Identical alerts are de-duplicated for an hour to avoid
notification storms. Alerts never go to client contacts.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from clipper.db import iso, row, tx


class AlertTransport(Protocol):
    def send(self, text: str) -> None: ...


class TelegramTransport:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.Client | None = None):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=15)

    def send(self, text: str) -> None:
        r = self.client.post(
            self.url,
            json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True},
        )
        if r.status_code != 200 or not r.json().get("ok"):
            raise RuntimeError(f"telegram delivery failed: HTTP {r.status_code} {r.text[:200]}")


class NullTransport:
    """Used when Telegram is not configured: every send fails loudly."""

    def send(self, text: str) -> None:
        raise RuntimeError("no alert channel configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")


class Alerter:
    def __init__(self, transport: AlertTransport):
        self.transport = transport

    def send(
        self,
        conn: sqlite3.Connection,
        now: datetime,
        kind: str,
        message: str,
        *,
        severity: str = "info",
        dedupe_key: str | None = None,
        dedupe_window: timedelta = timedelta(hours=1),
    ) -> bool:
        if dedupe_key:
            recent = row(
                conn,
                "SELECT id FROM alerts WHERE dedupe_key=? AND delivered=1 AND created_at>=?",
                (dedupe_key, iso(now - dedupe_window)),
            )
            if recent:
                return True
        prefix = {"critical": "🚨 ", "warning": "⚠️ "}.get(severity, "")
        error = None
        try:
            self.transport.send(f"{prefix}[GreatClipper] {message}")
            delivered = 1
        except Exception as exc:  # delivery failure must never crash the caller
            delivered, error = 0, str(exc)[:500]
        with tx(conn):
            conn.execute(
                "INSERT INTO alerts(kind, severity, message, dedupe_key, delivered, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, severity, message, dedupe_key, delivered, error, iso(now)),
            )
        return bool(delivered)


def alerts_healthy(conn: sqlite3.Connection) -> tuple[bool, str]:
    last = row(conn, "SELECT delivered, error, created_at FROM alerts ORDER BY id DESC LIMIT 1")
    tested = row(conn, "SELECT id FROM alerts WHERE kind='test' AND delivered=1 LIMIT 1")
    if not tested:
        return False, "alert channel has never passed a test (Dashboard → Send test alert)"
    if last and not last["delivered"]:
        return False, f"last alert delivery failed at {last['created_at']}: {last['error']}"
    return True, "ok"
