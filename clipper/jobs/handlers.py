"""Job handlers.

heavy queue (worker, one at a time): acquire_source → transcribe_source →
    select_clips → render_clip (+checks, +release)
light queue (scheduler, responsive): poll_feeds, publish_slots,
    publication_status, reconcile_publication, submission_reminder,
    housekeeping, backup
"""
from __future__ import annotations

import os
import secrets
import shutil
from datetime import timedelta
from pathlib import Path

from clipper import opportunities
from clipper.context import Ctx
from clipper.db import iso, jdump, jload, kv_get, kv_set, parse_iso, row, rows, tx
from clipper.jobs.queue import HoldError, PermanentError, TransientError, enqueue
from clipper.rendering import ffmpeg
from clipper.rules import checks
from clipper.rules.gates import release_gate, source_gate
from clipper.sources.safe_http import download, file_sha256
from clipper.transcription.whisper import Transcript, transcript_quality_problems


# ------------------------------------------------------------------ helpers --
def hold_source(ctx: Ctx, sid: int, reason: str) -> None:
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE sources SET stage='held', hold_reason=?, updated_at=? WHERE id=?",
                         (reason[:500], iso(ctx.now()), sid))


def hold_clip(ctx: Ctx, cid: int, reason: str) -> None:
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE clips SET stage='held', hold_reason=?, updated_at=? WHERE id=?",
                         (reason[:500], iso(ctx.now()), cid))


def disk_ok(ctx: Ctx) -> tuple[bool, str]:
    du = shutil.disk_usage(ctx.env.data_dir)
    free_gb = du.free / 1024**3
    frac = du.free / du.total
    if free_gb < ctx.settings.min_free_disk_gb or frac < ctx.settings.min_free_disk_fraction:
        return False, f"low disk: {free_gb:.1f} GB free ({frac:.0%})"
    return True, "ok"


def source_minutes_today(ctx: Ctx) -> float:
    from clipper.publishing.service import day_start_utc

    start = day_start_utc(ctx.now(), ctx.settings.timezone)
    r = row(ctx.conn, "SELECT COALESCE(SUM(duration_s),0) s FROM sources WHERE acquired_at>=?", (iso(start),))
    return (r["s"] or 0) / 60


# ------------------------------------------------------------------- heavy --
def acquire_source(ctx: Ctx, p: dict) -> None:
    sid = p["source_id"]
    src = row(ctx.conn, "SELECT * FROM sources WHERE id=?", (sid,))
    if src["stage"] != "discovered":
        return
    op = opportunities.get(ctx.conn, src["opportunity_id"])
    reasons = source_gate(op, ctx.now())
    if not src["rights_ref"]:
        reasons.append("recording has no rights reference")
    if reasons:
        hold_source(ctx, sid, "; ".join(reasons))
        return
    ok, why = disk_ok(ctx)
    if not ok:
        raise TransientError(why, 3600)
    if source_minutes_today(ctx) >= ctx.settings.max_source_minutes_per_day:
        raise TransientError("daily source-minutes budget used; continuing tomorrow", 3 * 3600)

    dest = ctx.env.media_dir / "sources" / f"{sid}.mp4"
    sha, size = download(ctx.http, src["download_url"], dest, ctx.settings.source_host_allowlist,
                         max_bytes=ctx.settings.max_source_bytes, resolve=ctx.resolve_dns)
    pr = ffmpeg.probe(dest)
    if not pr.width or pr.duration <= 0:
        dest.unlink(missing_ok=True)
        raise PermanentError("downloaded file has no decodable video stream")
    reuse = row(ctx.conn, "SELECT transcript_path, language, language_probability FROM sources WHERE file_hash=? "
                          "AND transcript_path IS NOT NULL AND id!=? LIMIT 1", (sha, sid))
    with tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE sources SET stage='acquired', file_path=?, file_hash=?, file_bytes=?, duration_s=?, width=?, "
            "height=?, acquired_at=?, updated_at=? WHERE id=?",
            (str(dest), sha, size, pr.duration, pr.width, pr.height, iso(ctx.now()), iso(ctx.now()), sid))
        if reuse:
            ctx.conn.execute("UPDATE sources SET transcript_path=?, language=?, language_probability=? WHERE id=?",
                             (reuse["transcript_path"], reuse["language"], reuse["language_probability"], sid))
    if pr.duration / 60 > ctx.settings.max_source_minutes_per_day:
        hold_source(ctx, sid, f"source is {pr.duration/60:.0f} min, above the daily {ctx.settings.max_source_minutes_per_day} "
                              "min limit; raise the limit or supply an excerpt")
        return
    probs = ffmpeg.layout_problems(op["layout"], pr)
    if probs:
        hold_source(ctx, sid, "layout changed: " + "; ".join(probs))
        return
    enqueue(ctx.conn, queue="heavy", type="transcribe_source", payload={"source_id": sid}, due_at=ctx.now(),
            dedupe_key=f"transcribe:{sid}")


def transcribe_source(ctx: Ctx, p: dict) -> None:
    sid = p["source_id"]
    src = row(ctx.conn, "SELECT * FROM sources WHERE id=?", (sid,))
    if src["stage"] != "acquired":
        return
    if not src["transcript_path"]:
        same = row(ctx.conn, "SELECT transcript_path FROM sources WHERE file_hash=? AND transcript_path IS NOT NULL "
                             "AND id!=? LIMIT 1", (src["file_hash"], sid))
        if same:
            src["transcript_path"] = same["transcript_path"]
    if src["transcript_path"] and Path(src["transcript_path"]).exists():
        t = Transcript.from_json(Path(src["transcript_path"]).read_text())
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE sources SET transcript_path=?, language=?, language_probability=? WHERE id=?",
                             (src["transcript_path"], t.language, t.language_probability, sid))
    else:
        t = ctx.transcriber.transcribe(Path(src["file_path"]))
        path = ctx.env.media_dir / "transcripts" / f"{src['file_hash']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(t.to_json())
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE sources SET transcript_path=?, language=?, language_probability=? WHERE id=?",
                             (str(path), t.language, t.language_probability, sid))
    problems = transcript_quality_problems(t, ctx.settings.whisper_min_language_probability)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE sources SET stage='transcribed', updated_at=? WHERE id=?", (iso(ctx.now()), sid))
    if problems:
        hold_source(ctx, sid, "transcript needs review: " + "; ".join(problems))
        return
    enqueue(ctx.conn, queue="heavy", type="select_clips", payload={"source_id": sid}, due_at=ctx.now(),
            dedupe_key=f"select:{sid}")


def select_clips(ctx: Ctx, p: dict) -> None:
    from clipper.editorial.selection import select_for_source

    sid = p["source_id"]
    src = row(ctx.conn, "SELECT * FROM sources WHERE id=?", (sid,))
    if src["stage"] != "transcribed":
        return
    op = opportunities.get(ctx.conn, src["opportunity_id"])
    reasons = source_gate(op, ctx.now())
    if reasons:  # re-check before spending model money
        hold_source(ctx, sid, "; ".join(reasons))
        return
    try:
        select_for_source(ctx, sid)
    except HoldError:
        return  # already recorded on the source


def render_clip(ctx: Ctx, p: dict) -> None:
    cid = p["clip_id"]
    clip = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    if clip["stage"] != "selected":
        return
    src = row(ctx.conn, "SELECT * FROM sources WHERE id=?", (clip["source_id"],))
    op = opportunities.get(ctx.conn, clip["opportunity_id"])
    if not src["file_path"] or not Path(src["file_path"]).exists():
        hold_clip(ctx, cid, "source media no longer available (cleaned up?)")
        return
    t = Transcript.from_json(Path(src["transcript_path"]).read_text())
    work = ctx.env.work_dir / f"clip{cid}"
    work.mkdir(parents=True, exist_ok=True)
    ass = work / "subs.ass"
    events = ffmpeg.caption_events(t, clip["start_s"], clip["end_s"])
    ffmpeg.write_ass(ass, events, clip["hook"], clip["end_s"] - clip["start_s"])
    out = ctx.env.media_dir / "renders" / f"clip{cid}.mp4"
    try:
        ffmpeg.render(Path(src["file_path"]), out, clip["start_s"], clip["end_s"], op["layout"], ass)
    except HoldError as exc:
        hold_clip(ctx, cid, str(exc))
        return
    rhash = file_sha256(out)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE clips SET stage='rendered', render_path=?, subtitle_path=?, updated_at=? WHERE id=?",
                         (str(out), str(ass), iso(ctx.now()), cid))
    run_checks(ctx, cid, rhash)


def run_checks(ctx: Ctx, cid: int, rhash: str) -> None:
    clip = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    results = checks.media_checks(Path(clip["render_path"]), clip["end_s"] - clip["start_s"],
                                  Path(clip["subtitle_path"]) if clip["subtitle_path"] else None)
    results.update(checks.duplicate_checks(ctx.conn, clip, rhash))
    failed = [f"{k}: {v['detail']}" for k, v in results.items() if not v["ok"]]
    with tx(ctx.conn):
        if failed:
            ctx.conn.execute("UPDATE clips SET stage='held', hold_reason=?, check_results=?, updated_at=? WHERE id=?",
                             ("failed checks — " + "; ".join(failed)[:450], jdump(results), iso(ctx.now()), cid))
        else:
            ctx.conn.execute("UPDATE clips SET stage='checked', render_hash=?, check_results=?, updated_at=? "
                             "WHERE id=?", (rhash, jdump(results), iso(ctx.now()), cid))
    if not failed:
        try_release(ctx, cid)
    _maybe_finish_source(ctx, clip["source_id"])


def _maybe_finish_source(ctx: Ctx, sid: int) -> None:
    open_ = row(ctx.conn, "SELECT COUNT(*) n FROM clips WHERE source_id=? AND stage IN ('selected','rendered')", (sid,))
    if open_["n"] == 0:
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE sources SET stage='done', updated_at=? WHERE id=? AND stage='selected'",
                             (iso(ctx.now()), sid))


def try_release(ctx: Ctx, cid: int) -> list[str]:
    """checked → ready when every release gate passes. Calibration review keeps
    the clip in 'checked' (review queue); other problems hold it."""
    from clipper.publishing.service import compose_caption

    clip = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    if clip["stage"] not in ("checked",):
        return [f"stage is {clip['stage']}"]
    op = opportunities.get(ctx.conn, clip["opportunity_id"])
    reasons = release_gate(ctx.conn, op, clip, compose_caption(op, clip), ctx.now(),
                           calibration_review_count=ctx.settings.calibration_review_count)
    if not reasons and clip["review_state"] == "pending" and clip["context_risk"]:
        reasons.append("context risk flagged: owner review required")
    soft = [r for r in reasons if r.startswith(("calibration", "campaign requires draft pre-approval",
                                                  "editor flagged", "context risk"))]
    with tx(ctx.conn):
        if not reasons:
            token = clip["public_token"] or secrets.token_urlsafe(24)
            pub = ctx.env.public_dir / f"{token}.mp4"
            if not pub.exists():
                try:
                    os.link(clip["render_path"], pub)
                except OSError:
                    shutil.copy2(clip["render_path"], pub)
            ctx.conn.execute("UPDATE clips SET stage='ready', public_token=?, hold_reason=NULL, updated_at=? WHERE id=?",
                             (token, iso(ctx.now()), cid))
        elif len(soft) == len(reasons):
            ctx.conn.execute("UPDATE clips SET hold_reason=?, updated_at=? WHERE id=?",
                             ("awaiting review: " + "; ".join(reasons)[:450], iso(ctx.now()), cid))
            if op.get("requires_preapproval") and clip["preapproval_state"] == "not_required":
                ctx.conn.execute("UPDATE clips SET preapproval_state='pending' WHERE id=?", (cid,))
        else:
            ctx.conn.execute("UPDATE clips SET stage='held', hold_reason=?, updated_at=? WHERE id=?",
                             ("; ".join(reasons)[:500], iso(ctx.now()), cid))
    return reasons


# ------------------------------------------------------------------- light --
def poll_feeds(ctx: Ctx, p: dict) -> None:
    from clipper.sources.feed import poll_feed

    for op in opportunities.list_all(ctx.conn):
        if op["status"] == "active" and op.get("feed_url"):
            try:
                new = poll_feed(ctx, op["id"])
                with tx(ctx.conn):
                    kv_set(ctx.conn, f"feed_ok:{op['id']}", {"at": iso(ctx.now()), "new": len(new)})
            except (PermanentError, TransientError) as exc:
                ctx.alerter.send(ctx.conn, ctx.now(), "feed_error", f"Source feed for {op['name']} failed: {exc}",
                                 severity="warning", dedupe_key=f"feed:{op['id']}", dedupe_window=timedelta(hours=6))


def publish_slots(ctx: Ctx, p: dict) -> None:
    from clipper.publishing.service import run_publish_slots

    run_publish_slots(ctx)


def publication_status(ctx: Ctx, p: dict) -> None:
    from clipper.publishing.service import check_status

    check_status(ctx, p["publication_id"], p.get("n", 0))


def reconcile_publication(ctx: Ctx, p: dict) -> None:
    from clipper.publishing.service import reconcile

    reconcile(ctx, p["publication_id"])


def submission_reminder(ctx: Ctx, p: dict) -> None:
    from clipper.submissions.manual import reminder

    reminder(ctx, p["submission_id"], p["kind"])


def housekeeping(ctx: Ctx, p: dict) -> None:
    from clipper.budget import spend

    st = ctx.settings
    bs = spend.status(ctx.conn, ctx.now(), st.monthly_operating_ceiling_usd, st.monthly_model_allowance_usd)
    level = spend.threshold_alert_level(bs)
    if level:
        ctx.alerter.send(ctx.conn, ctx.now(), "budget", f"Spend is at {bs.fraction:.0%} of the monthly ceiling "
                         f"(${bs.used_minor/100:.2f} of ${bs.ceiling_minor/100:.2f}).",
                         severity="warning", dedupe_key=f"budget:{bs.period}:{level}", dedupe_window=timedelta(days=31))
    ok, why = disk_ok(ctx)
    if not ok:
        ctx.alerter.send(ctx.conn, ctx.now(), "disk", f"Ingest paused: {why}", severity="warning",
                         dedupe_key="disk", dedupe_window=timedelta(hours=12))
    cleanup_files(ctx)


def cleanup_files(ctx: Ctx) -> int:
    """Delete expendable raw/intermediate files after the retention window.
    Final renders of submitted campaign clips are kept for the dispute period."""
    st, now = ctx.settings, ctx.now()
    cutoff = iso(now - timedelta(days=st.raw_retention_days))
    removed = 0
    for s in rows(ctx.conn, "SELECT id, file_path FROM sources WHERE file_path IS NOT NULL AND updated_at<? "
                            "AND stage IN ('done','held','rejected')", (cutoff,)):
        if s["file_path"] and Path(s["file_path"]).exists():
            Path(s["file_path"]).unlink()
            removed += 1
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE sources SET file_path=NULL WHERE id=?", (s["id"],))
    for c in rows(ctx.conn, "SELECT id FROM clips WHERE updated_at<?", (cutoff,)):
        shutil.rmtree(ctx.env.work_dir / f"clip{c['id']}", ignore_errors=True)
    keep_cutoff = iso(now - timedelta(days=st.submission_retention_days))
    for c in rows(ctx.conn, "SELECT c.id, c.render_path, c.public_token FROM clips c WHERE c.render_path IS NOT NULL "
                            "AND c.updated_at<? AND (c.stage IN ('rejected','held') OR (c.stage='published' AND NOT EXISTS ("
                            " SELECT 1 FROM publications p JOIN submissions s ON s.publication_id=p.id "
                            " WHERE p.clip_id=c.id AND s.created_at>=?)))", (cutoff, keep_cutoff)):
        for f in (c["render_path"], c["public_token"] and str(ctx.env.public_dir / f"{c['public_token']}.mp4")):
            if f and Path(f).exists():
                Path(f).unlink()
                removed += 1
    return removed


def backup(ctx: Ctx, p: dict) -> None:
    from clipper.backup import make_backup

    make_backup(ctx)


HEAVY = {
    "acquire_source": acquire_source,
    "transcribe_source": transcribe_source,
    "select_clips": select_clips,
    "render_clip": render_clip,
}
LIGHT = {
    "poll_feeds": poll_feeds,
    "publish_slots": publish_slots,
    "publication_status": publication_status,
    "reconcile_publication": reconcile_publication,
    "submission_reminder": submission_reminder,
    "housekeeping": housekeeping,
    "backup": backup,
}
