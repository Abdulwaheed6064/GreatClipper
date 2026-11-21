"""Long-running processes.

* scheduler: exactly one active (lease lock in kv). On start it reconciles
  outstanding publications before anything new is published, then enqueues
  periodic jobs and runs the light queue.
* worker: runs heavy jobs (download/transcribe/model/render) one at a time.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
import traceback
from datetime import timedelta

from clipper.context import Ctx
from clipper.db import iso, kv_get, kv_set, parse_iso, tx
from clipper.jobs import queue
from clipper.jobs.handlers import HEAVY, LIGHT

log = logging.getLogger("clipper")
_stop = False


def _install_signals() -> None:
    def handler(signum, frame):
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def run_one(ctx: Ctx, queue_name: str, owner: str, lease_s: int) -> bool:
    table = HEAVY if queue_name == "heavy" else LIGHT
    job = queue.reserve(ctx.conn, queue_name, owner, ctx.now(), lease_s)
    if not job:
        return False
    fn = table.get(job["type"])
    try:
        if fn is None:
            raise queue.PermanentError(f"unknown job type {job['type']}")
        fn(ctx, job["payload"])
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
        status = queue.fail(ctx.conn, job, exc, ctx.now())
        log.warning("job %s %s failed (%s): %s", job["id"], job["type"], status, exc)
        if getattr(exc, "error_class", None) is None:
            log.debug(traceback.format_exc())
        if status in ("dead",) or (status == "failed" and getattr(exc, "error_class", "") == "permanent"):
            ctx.alerter.send(ctx.conn, ctx.now(), "job_failed",
                             f"Job {job['type']} #{job['id']} stopped: {exc}", severity="warning",
                             dedupe_key=f"job:{job['type']}:{type(exc).__name__}", dedupe_window=timedelta(hours=6))
        _hold_item_for_job(ctx, job, exc)
    else:
        queue.complete(ctx.conn, job["id"], ctx.now())
    return True


def _hold_item_for_job(ctx: Ctx, job: dict, exc: Exception) -> None:
    """Make permanent failures visible on the source/clip instead of only in the job table."""
    if getattr(exc, "error_class", None) not in ("permanent", "hold") and job["attempts"] < job["max_attempts"]:
        return
    p = job["payload"]
    with tx(ctx.conn):
        if "source_id" in p:
            ctx.conn.execute("UPDATE sources SET stage='held', hold_reason=?, updated_at=? WHERE id=? "
                             "AND stage NOT IN ('done','held','rejected')", (str(exc)[:500], iso(ctx.now()), p["source_id"]))
        if "clip_id" in p:
            ctx.conn.execute("UPDATE clips SET stage='held', hold_reason=?, updated_at=? WHERE id=? "
                             "AND stage IN ('selected','rendered','checked')", (str(exc)[:500], iso(ctx.now()), p["clip_id"]))


def acquire_scheduler_lock(ctx: Ctx, owner: str, ttl_s: int = 90) -> bool:
    with tx(ctx.conn):
        cur = kv_get(ctx.conn, "scheduler_lock")
        now = ctx.now()
        if cur and cur["owner"] != owner and parse_iso(cur["until"]) > now:
            return False
        kv_set(ctx.conn, "scheduler_lock", {"owner": owner, "until": iso(now + timedelta(seconds=ttl_s))})
    return True


def _every(ctx: Ctx, key: str, minutes: float) -> bool:
    last = parse_iso(kv_get(ctx.conn, f"last:{key}"))
    if last and ctx.now() - last < timedelta(minutes=minutes):
        return False
    with tx(ctx.conn):
        kv_set(ctx.conn, f"last:{key}", iso(ctx.now()))
    return True


def scheduler_tick(ctx: Ctx) -> None:
    now = ctx.now()
    if _every(ctx, "poll_feeds", ctx.settings.source_poll_minutes):
        queue.enqueue(ctx.conn, queue="light", type="poll_feeds", due_at=now, dedupe_key=f"poll:{now:%Y%m%d%H%M}")
    if _every(ctx, "publish_slots", 1):
        queue.enqueue(ctx.conn, queue="light", type="publish_slots", due_at=now, dedupe_key=f"slots:{now:%Y%m%d%H%M}",
                      max_attempts=1)
    if ctx.tg is not None and _every(ctx, "campaign_prompts", 5):
        queue.enqueue(ctx.conn, queue="light", type="campaign_prompts", due_at=now,
                      dedupe_key=f"prompts:{now:%Y%m%d%H%M}", max_attempts=1)
    if _every(ctx, "housekeeping", 60):
        queue.enqueue(ctx.conn, queue="light", type="housekeeping", due_at=now, dedupe_key=f"hk:{now:%Y%m%d%H}")
    if _every(ctx, "backup", 24 * 60):
        queue.enqueue(ctx.conn, queue="light", type="backup", due_at=now, dedupe_key=f"backup:{now:%Y%m%d}")


def scheduler_loop(ctx: Ctx, poll_s: float = 2.0) -> None:
    from clipper.publishing.service import startup_reconcile

    _install_signals()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    while not acquire_scheduler_lock(ctx, owner):
        log.warning("another scheduler holds the lock; waiting")
        time.sleep(30)
        if _stop:
            return
    log.info("scheduler %s started; reconciling outstanding publications first", owner)
    startup_reconcile(ctx)
    while not _stop:
        if not acquire_scheduler_lock(ctx, owner):
            log.error("lost scheduler lock; exiting")
            return
        scheduler_tick(ctx)
        worked = False
        for _ in range(20):
            if not run_one(ctx, "light", owner, lease_s=300):
                break
            worked = True
        if not worked:
            time.sleep(poll_s)


def worker_loop(ctx: Ctx, poll_s: float = 5.0) -> None:
    _install_signals()
    owner = f"worker:{socket.gethostname()}:{os.getpid()}"
    log.info("heavy worker %s started", owner)
    while not _stop:
        # lease must outlive the longest render/transcription; it is recovered if the worker dies
        if not run_one(ctx, "heavy", owner, lease_s=3 * 3600):
            time.sleep(poll_s)
