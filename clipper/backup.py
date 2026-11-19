"""Encrypted, consistent SQLite backups and restore.

* Uses SQLite's online backup API (WAL-safe), never a raw file copy.
* Encrypted with Fernet (BACKUP_KEY). Without a key, no backup is written and
  the owner is alerted — unencrypted copies must not leave the server.
* Off-host copy: set BACKUP_RCLONE_REMOTE (e.g. "b2:my-bucket/clipper") and
  install rclone; otherwise sync data/backups yourself.
* A restore pauses the system so the owner reviews state before anything new
  is published; outstanding sends are reconciled, not re-sent.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from clipper.context import Ctx
from clipper.db import backup_to, connect, kv_set, tx

KEEP = 14


def make_backup(ctx: Ctx) -> Path | None:
    key = ctx.env.backup_key
    if not key:
        ctx.alerter.send(ctx.conn, ctx.now(), "backup", "Backup skipped: BACKUP_KEY is not set.", severity="warning",
                         dedupe_key="backup:nokey")
        return None
    out_dir = ctx.env.backup_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "clipper.db"
        backup_to(ctx.conn, raw)
        chk = sqlite3.connect(raw)
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        chk.close()
        if not ok:
            ctx.alerter.send(ctx.conn, ctx.now(), "backup", "Backup integrity check FAILED", severity="critical")
            return None
        data = gzip.compress(raw.read_bytes())
    token = Fernet(key.encode()).encrypt(data)
    dest = out_dir / f"clipper-{ctx.now():%Y%m%dT%H%M%SZ}.db.gz.enc"
    dest.write_bytes(token)
    for old in sorted(out_dir.glob("clipper-*.db.gz.enc"))[:-KEEP]:
        old.unlink()
    remote = os.environ.get("BACKUP_RCLONE_REMOTE")
    if remote:
        if shutil.which("rclone") is None:
            ctx.alerter.send(ctx.conn, ctx.now(), "backup", "BACKUP_RCLONE_REMOTE set but rclone is not installed",
                             severity="warning", dedupe_key="backup:rclone")
        else:
            p = subprocess.run(["rclone", "copy", str(dest), remote], capture_output=True, text=True, timeout=1800)
            if p.returncode != 0:
                ctx.alerter.send(ctx.conn, ctx.now(), "backup", f"Off-host backup copy failed: {p.stderr[-300:]}",
                                 severity="warning", dedupe_key="backup:offhost")
    return dest


def restore(backup_file: Path, key: str, db_path: Path) -> None:
    """Restore into db_path. Stop the web/scheduler/worker processes first."""
    data = gzip.decompress(Fernet(key.encode()).decrypt(backup_file.read_bytes()))
    tmp = db_path.with_suffix(".restore")
    tmp.write_bytes(data)
    chk = sqlite3.connect(tmp)
    if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        chk.close()
        tmp.unlink()
        raise RuntimeError("backup failed integrity check")
    chk.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.rename(p.with_name(p.name + ".before-restore"))
    tmp.rename(db_path)
    conn = connect(db_path)
    with tx(conn):
        kv_set(conn, "paused", True)
        kv_set(conn, "restored_from", backup_file.name)
    conn.close()


def new_key() -> str:
    return Fernet.generate_key().decode()
