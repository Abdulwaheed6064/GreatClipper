"""Durable job queue on SQLite.

* Jobs survive restarts (they are rows, not in-memory timers).
* A job is *reserved* with a lease inside a short transaction; the work then
  runs outside any transaction. Expired leases are recovered.
* Retries use exponential backoff with jitter; permanent errors stop at once.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta

from clipper.db import iso, jdump, jload, row, tx


class PermanentError(Exception):
    """Do not retry: invalid content, credentials, permissions, exhausted funds."""

    error_class = "permanent"


class HoldError(PermanentError):
    """Stop and hold the item for a human; not a crash."""

    error_class = "hold"


class TransientError(Exception):
    """Retry with backoff (network blip, 5xx, rate limit)."""

    error_class = "transient"

    def __init__(self, msg: str, retry_after_s: float | None = None):
        super().__init__(msg)
        self.retry_after_s = retry_after_s


def enqueue(
    conn: sqlite3.Connection,
    *,
    queue: str,
    type: str,
    payload: dict | None = None,
    due_at: datetime,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job. With a dedupe_key, a second enqueue is a no-op and returns None."""
    now = iso(due_at)
    cur = conn.execute(
        "INSERT INTO jobs(queue, type, payload, dedupe_key, due_at, max_attempts, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
        (queue, type, jdump(payload or {}), dedupe_key, iso(due_at), max_attempts, now, now),
    )
    return cur.lastrowid if cur.rowcount else None


def reserve(
    conn: sqlite3.Connection, queue: str, owner: str, now: datetime, lease_s: int
) -> dict | None:
    """Atomically take the oldest due job (or one whose lease expired)."""
    with tx(conn):
        job = row(
            conn,
            "SELECT * FROM jobs WHERE queue=? AND ("
            "  (status='pending' AND due_at<=?) OR (status='running' AND lease_until<?)"
            ") ORDER BY due_at, id LIMIT 1",
            (queue, iso(now), iso(now)),
        )
        if not job:
            return None
        conn.execute(
            "UPDATE jobs SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1, updated_at=? "
            "WHERE id=?",
            (owner, iso(now + timedelta(seconds=lease_s)), iso(now), job["id"]),
        )
    job["attempts"] += 1
    job["payload"] = jload(job["payload"], {})
    return job


def complete(conn: sqlite3.Connection, job_id: int, now: datetime) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE jobs SET status='done', lease_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
            (iso(now), job_id),
        )


def fail(conn: sqlite3.Connection, job: dict, exc: BaseException, now: datetime) -> str:
    """Record a failure. Returns the new status ('pending' for retry, 'failed'/'dead')."""
    err_class = getattr(exc, "error_class", "unexpected")
    msg = f"{type(exc).__name__}: {exc}"[:2000]
    if err_class in ("permanent", "hold"):
        status, due = "failed", now
    elif job["attempts"] >= job["max_attempts"]:
        status, due = "dead", now
    else:
        retry_after = getattr(exc, "retry_after_s", None)
        base = retry_after if retry_after else min(3600, 30 * (2 ** (job["attempts"] - 1)))
        due = now + timedelta(seconds=base * random.uniform(1.0, 1.3))
        status = "pending"
    with tx(conn):
        conn.execute(
            "UPDATE jobs SET status=?, due_at=?, lease_owner=NULL, lease_until=NULL, last_error=?, "
            "error_class=?, updated_at=? WHERE id=?",
            (status, iso(due), msg, err_class, iso(now), job["id"]),
        )
    return status


def cancel_pending(conn: sqlite3.Connection, type: str, payload_like: str | None = None) -> int:
    sql = "UPDATE jobs SET status='cancelled' WHERE status='pending' AND type=?"
    params: list = [type]
    if payload_like:
        sql += " AND payload LIKE ?"
        params.append(payload_like)
    return conn.execute(sql, params).rowcount
