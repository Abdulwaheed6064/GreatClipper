"""Source discovery from a creator-controlled JSON feed, plus manual links.

Feed format (see examples/source_feed.json)::

    {"videos": [{"id": "...", "download_url": "https://...", "title": "...", "rights_ref": "..."}]}

A public channel link discovers content but does not grant download rights, so
only feeds/links the owner configured for an approved opportunity are used.
"""
from __future__ import annotations

from datetime import datetime

from clipper import opportunities
from clipper.context import Ctx
from clipper.db import audit, iso, tx
from clipper.jobs.queue import PermanentError, enqueue
from clipper.rules.gates import source_gate
from clipper.sources.links import allowlist
from clipper.sources.safe_http import fetch_json, validate_url


def _insert_source(ctx: Ctx, op: dict, item: dict, mode: str, now: datetime) -> int | None:
    pid = str(item.get("id") or "").strip()
    url = str(item.get("download_url") or "").strip()
    if not pid or not url:
        return None
    hold_reason = None
    try:
        validate_url(url, allowlist(ctx.conn, ctx.settings.source_host_allowlist), resolve=False)
    except Exception as exc:  # recorded as a held source so the owner can see it
        hold_reason = f"download URL refused: {exc}"
    if not item.get("rights_ref"):
        hold_reason = hold_reason or "no rights_ref supplied for this recording"
    cur = ctx.conn.execute(
        "INSERT INTO sources(opportunity_id, provider_id, ingest_mode, download_url, title, rights_ref, "
        "stage, hold_reason, discovered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(opportunity_id, provider_id, processing_version) DO NOTHING",
        (op["id"], pid, mode, url, str(item.get("title") or "")[:300], item.get("rights_ref"),
         "held" if hold_reason else "discovered", hold_reason, iso(now), iso(now)),
    )
    if not cur.rowcount:
        return None
    sid = cur.lastrowid
    if hold_reason:
        return sid
    enqueue(ctx.conn, queue="heavy", type="acquire_source", payload={"source_id": sid},
            due_at=now, dedupe_key=f"acquire:{sid}")
    return sid


def poll_feed(ctx: Ctx, opp_id: str) -> list[int]:
    now = ctx.now()
    op = opportunities.get(ctx.conn, opp_id)
    if not op or not op.get("feed_url"):
        return []
    reasons = source_gate(op, now)
    if reasons:
        return []
    data = fetch_json(ctx.http, op["feed_url"], allowlist(ctx.conn, ctx.settings.source_host_allowlist),
                      resolve=ctx.resolve_dns)
    videos = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(videos, list):
        raise PermanentError("feed JSON must contain a 'videos' list")
    new = []
    for item in videos[:200]:
        if not isinstance(item, dict):
            continue
        with tx(ctx.conn):
            sid = _insert_source(ctx, op, item, "feed", now)
        if sid:
            new.append(sid)
    return new


def add_manual(ctx: Ctx, opp_id: str, provider_id: str, url: str, title: str, rights_ref: str) -> int | None:
    """Owner-entered authorized link. Ingestion is labelled semi-manual."""
    now = ctx.now()
    op = opportunities.get(ctx.conn, opp_id)
    if op is None:
        raise KeyError(opp_id)
    with tx(ctx.conn):
        sid = _insert_source(ctx, op, {"id": provider_id, "download_url": url, "title": title,
                                       "rights_ref": rights_ref}, "manual", now)
        audit(ctx.conn, "owner", "source.add_manual", {"opportunity": opp_id, "provider_id": provider_id, "id": sid})
    return sid
