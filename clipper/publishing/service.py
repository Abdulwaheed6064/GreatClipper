"""Publication state machine.

reserved → sending → accepted → sent        (live URL + publish time stored)
                   ↘ failed                 (provably not sent: validation/connect errors)
                   ↘ unknown                (maybe sent: reconcile, else hold + alert)

* The intent row is committed *before* the external call.
* Ambiguous sends are never retried blindly; ``reconcile`` looks the post up.
* Success requires a real external link; a provider job id is not a post URL.
* On restart, outstanding sends are reconciled before anything new is published.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from clipper import opportunities
from clipper.alerts import alerts_healthy
from clipper.context import Ctx
from clipper.db import iso, jdump, jload, parse_iso, row, rows, tx
from clipper.jobs.queue import PermanentError, TransientError, enqueue
from clipper.publishing.base import AmbiguousSendError, PostRequest, StatusResult
from clipper.rules.gates import is_live, publish_gate


def compose_caption(op: dict, clip: dict) -> str:
    tags: list[str] = []
    for t in op.get("required_tags") or []:
        if t and t not in tags:
            tags.append(t)
    for h in jload(clip.get("hashtags"), []) if isinstance(clip.get("hashtags"), str) else (clip.get("hashtags") or []):
        h = h if h.startswith("#") else "#" + h.replace(" ", "")
        if h.lower() not in (x.lower() for x in tags):
            tags.append(h)
    text = (op.get("caption_template") or "{caption}\n\n{tags}").replace("\\n", "\n")
    return text.replace("{caption}", clip.get("caption") or "").replace("{tags}", " ".join(tags)).strip()


def day_start_utc(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))


def media_url(ctx: Ctx, clip: dict) -> str:
    return f"{ctx.env.public_base_url}/m/{clip['public_token']}.mp4"


def _disclosure(op: dict) -> dict:
    return {"paid_partnership": op["disclosure_required"] == "native_paid_partnership",
            "sponsor_ids": op.get("sponsor_ids") or [], "required": op["disclosure_required"]}


def publish_clip(ctx: Ctx, clip_id: int, *, readiness_id: int | None = None) -> tuple[int | None, list[str]]:
    """Gate, reserve intent, call the publisher. Returns (publication_id, blocking_reasons)."""
    conn, st, now = ctx.conn, ctx.settings, ctx.now()
    clip = row(conn, "SELECT * FROM clips WHERE id=?", (clip_id,))
    if not clip:
        return None, ["clip not found"]
    op = opportunities.get(conn, clip["opportunity_id"])
    caption = compose_caption(op, clip)
    readiness = row(conn, "SELECT * FROM operator_ready WHERE id=?", (readiness_id,)) if readiness_id else None
    live = is_live(conn, st.mode, op["id"])
    reasons = publish_gate(
        conn, op, clip, caption, now, mode=st.mode, calibration_review_count=st.calibration_review_count,
        max_posts_per_day=st.max_posts_per_day_total, day_start_utc=day_start_utc(now, st.timezone),
        readiness=readiness if op["revenue_route"] == "campaign" else None, alerts_ok=alerts_healthy(conn),
    )
    if op["revenue_route"] != "campaign" or not st.campaign_publish_requires_operator_ready:
        reasons = [r for r in reasons if "readiness" not in r]
    if reasons:
        return None, reasons

    publisher_name = op["publisher"] if live else "dry_run"
    account = op["account_ref"] if live else f"dry:{op['account_ref'] or op['id']}"
    publisher = ctx.publishers[publisher_name]
    req = PostRequest(intent_key="", account_ref=op["account_ref"] or "", caption=caption,
                      media_url=media_url(ctx, clip), title=clip.get("title") or "", disclosure=_disclosure(op))
    with tx(conn):
        attempt = row(conn, "SELECT COUNT(*) n FROM publications WHERE clip_id=? AND account_ref=?",
                      (clip_id, account))["n"] + 1
        req.intent_key = f"{clip_id}:{account}:{attempt}"
        cur = conn.execute(
            "INSERT INTO publications(clip_id, opportunity_id, publisher, account_ref, intent_key, dry_run, status, "
            "caption, media_url, disclosure, created_at, updated_at) VALUES (?,?,?,?,?,?, 'reserved', ?,?,?,?,?)",
            (clip_id, op["id"], publisher_name, account, req.intent_key, int(not live), caption, req.media_url,
             jdump(req.disclosure), iso(now), iso(now)),
        )
        pub_id = cur.lastrowid
        if readiness:
            conn.execute("UPDATE operator_ready SET used_at=?, publication_id=? WHERE id=?",
                         (iso(now), pub_id, readiness["id"]))
    with tx(conn):
        conn.execute("UPDATE publications SET status='sending', request_started_at=?, updated_at=? WHERE id=?",
                     (iso(ctx.now()), iso(ctx.now()), pub_id))
    try:
        res = publisher.create(req)
    except AmbiguousSendError as exc:
        _mark_unknown(ctx, pub_id, str(exc))
        enqueue(conn, queue="light", type="reconcile_publication", payload={"publication_id": pub_id},
                due_at=ctx.now() + timedelta(minutes=2), dedupe_key=f"reconcile:{pub_id}:1")
        return pub_id, []
    except (PermanentError, TransientError) as exc:
        # provably not sent (validation, auth, connect failure): safe to mark failed
        with tx(conn):
            conn.execute("UPDATE publications SET status='failed', last_error=?, updated_at=? WHERE id=?",
                         (str(exc)[:1000], iso(ctx.now()), pub_id))
            if isinstance(exc, PermanentError):
                conn.execute("UPDATE clips SET stage='held', hold_reason=?, updated_at=? WHERE id=?",
                             (f"publish failed: {exc}"[:500], iso(ctx.now()), clip_id))
        ctx.alerter.send(conn, ctx.now(), "publish_failed",
                         f"Publish of clip {clip_id} to {publisher_name} failed (not sent): {exc}",
                         severity="warning", dedupe_key=f"pubfail:{clip_id}")
        return pub_id, []
    with tx(conn):
        conn.execute("UPDATE publications SET status='accepted', provider_id=?, provider_state=?, updated_at=? "
                     "WHERE id=?", (res.provider_id, jdump(res.state), iso(ctx.now()), pub_id))
    schedule_status_check(ctx, pub_id, 0)
    return pub_id, []


def schedule_status_check(ctx: Ctx, pub_id: int, n: int) -> bool:
    st = ctx.settings
    offsets = st.provider_status_check_offsets_s[: st.provider_initial_status_checks_max]
    if n >= len(offsets):
        return False
    pub = row(ctx.conn, "SELECT request_started_at FROM publications WHERE id=?", (pub_id,))
    base = parse_iso(pub["request_started_at"]) or ctx.now()
    due = max(base + timedelta(seconds=offsets[n]), ctx.now())
    enqueue(ctx.conn, queue="light", type="publication_status", payload={"publication_id": pub_id, "n": n},
            due_at=due, dedupe_key=f"pubstatus:{pub_id}:{n}", max_attempts=1)
    return True


def _mark_unknown(ctx: Ctx, pub_id: int, why: str) -> None:
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE publications SET status='unknown', last_error=?, updated_at=? WHERE id=?",
                         (why[:1000], iso(ctx.now()), pub_id))
    ctx.alerter.send(ctx.conn, ctx.now(), "publish_unknown",
                     f"Publication #{pub_id}: outcome unknown ({why}). New publishing is blocked until it is "
                     f"reconciled. Check the account and resolve it on the dashboard.",
                     severity="critical", dedupe_key=f"unknown:{pub_id}")


def check_status(ctx: Ctx, pub_id: int, n: int) -> str:
    conn = ctx.conn
    pub = row(conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    if not pub or pub["status"] not in ("accepted",):
        return pub["status"] if pub else "missing"
    publisher = ctx.publishers[pub["publisher"]]
    state = jload(pub["provider_state"], {})
    with tx(conn):
        conn.execute("UPDATE publications SET status_checks=status_checks+1, updated_at=? WHERE id=?",
                     (iso(ctx.now()), pub_id))
    try:
        res: StatusResult = publisher.status(pub["provider_id"], state)
    except TransientError as exc:
        res = StatusResult("pending", error=str(exc))
    except PermanentError as exc:
        res = StatusResult("unknown", error=str(exc))
    if res.state is not None:
        with tx(conn):
            conn.execute("UPDATE publications SET provider_state=? WHERE id=?", (jdump(res.state), pub_id))
    if res.status == "sent" and res.external_link:
        finalize_sent(ctx, pub_id, res.external_link, res.sent_at)
        return "sent"
    if res.status == "failed":
        with tx(conn):
            conn.execute("UPDATE publications SET status='failed', last_error=?, updated_at=? WHERE id=?",
                         (res.error, iso(ctx.now()), pub_id))
            conn.execute("UPDATE clips SET stage='held', hold_reason=?, updated_at=? WHERE id=?",
                         (f"provider reported failure: {res.error}"[:500], iso(ctx.now()), pub["clip_id"]))
        ctx.alerter.send(conn, ctx.now(), "publish_failed", f"Publication #{pub_id} failed at provider: {res.error}",
                         severity="warning")
        return "failed"
    if res.status == "unknown" or not schedule_status_check(ctx, pub_id, n + 1):
        _mark_unknown(ctx, pub_id, res.error or "no live URL after the initial status checks")
        return "unknown"
    return "pending"


def finalize_sent(ctx: Ctx, pub_id: int, link: str, sent_at: datetime | None, *, actor: str = "system") -> None:
    conn = ctx.conn
    pub = row(conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    op = opportunities.get(conn, pub["opportunity_id"])
    estimated = sent_at is None
    if estimated:
        # earliest plausible publish time: when the request started (conservative for deadlines)
        sent_at = parse_iso(pub["request_started_at"]) or parse_iso(pub["created_at"])
    with tx(conn):
        conn.execute("UPDATE publications SET status='sent', sent_at=?, sent_at_estimated=?, live_url=?, "
                     "last_error=NULL, updated_at=? WHERE id=?",
                     (iso(sent_at), int(estimated), link, iso(ctx.now()), pub_id))
        if not pub["dry_run"]:
            conn.execute("UPDATE clips SET stage='published', updated_at=? WHERE id=?", (iso(ctx.now()), pub["clip_id"]))
    if op["revenue_route"] == "campaign":
        from clipper.submissions.manual import open_submission

        open_submission(ctx, pub_id)
    else:
        ctx.alerter.send(conn, ctx.now(), "published",
                         f"{'[DRY RUN] ' if pub['dry_run'] else ''}Published clip {pub['clip_id']} for {op['name']}: {link}")


def reconcile(ctx: Ctx, pub_id: int) -> str:
    conn = ctx.conn
    pub = row(conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    if pub["status"] not in ("unknown", "sending"):
        return pub["status"]
    publisher = ctx.publishers[pub["publisher"]]
    op = opportunities.get(conn, pub["opportunity_id"])
    req = PostRequest(pub["intent_key"], op["account_ref"] or "", pub["caption"], pub["media_url"],
                      disclosure=jload(pub["disclosure"], {}))
    since = parse_iso(pub["request_started_at"]) or parse_iso(pub["created_at"])
    try:
        found = publisher.find_existing(req, since)
    except (TransientError, PermanentError) as exc:
        found = None
        note = str(exc)
    else:
        note = "not found at provider"
    if found:
        with tx(conn):
            conn.execute("UPDATE publications SET status='accepted', provider_id=?, provider_state=?, status_checks=0, "
                         "updated_at=? WHERE id=?", (found.provider_id, jdump(found.state), iso(ctx.now()), pub_id))
        enqueue(conn, queue="light", type="publication_status", payload={"publication_id": pub_id, "n": 0},
                due_at=ctx.now(), dedupe_key=f"pubstatus:{pub_id}:r{ctx.now().timestamp():.0f}", max_attempts=1)
        return "accepted"
    _mark_unknown(ctx, pub_id, f"reconciliation: {note}; owner must check the account")
    return "unknown"


def resolve_unknown(ctx: Ctx, pub_id: int, *, published: bool, live_url: str | None = None,
                    sent_at: datetime | None = None, actor: str = "owner") -> None:
    """Owner resolution after checking the real account."""
    conn = ctx.conn
    pub = row(conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    if pub["status"] not in ("unknown", "accepted", "sending"):
        raise ValueError(f"publication is {pub['status']}")
    from clipper.db import audit

    if published:
        if not live_url or not live_url.startswith("https://"):
            raise ValueError("a live https URL is required to mark as published")
        finalize_sent(ctx, pub_id, live_url, sent_at, actor=actor)
    else:
        with tx(conn):
            conn.execute("UPDATE publications SET status='failed', last_error='owner confirmed not published', "
                         "updated_at=? WHERE id=?", (iso(ctx.now()), pub_id))
    with tx(conn):
        audit(conn, actor, "publication.resolve", {"id": pub_id, "published": published, "url": live_url})


def startup_reconcile(ctx: Ctx) -> None:
    """Run before the scheduler issues any new publish."""
    conn = ctx.conn
    for p in rows(conn, "SELECT id, status FROM publications WHERE status IN ('reserved','sending','accepted')"):
        if p["status"] == "reserved":
            with tx(conn):
                conn.execute("UPDATE publications SET status='failed', last_error='interrupted before sending', "
                             "updated_at=? WHERE id=?", (iso(ctx.now()), p["id"]))
        elif p["status"] == "sending":
            _mark_unknown(ctx, p["id"], "process stopped during the publish call")
            enqueue(conn, queue="light", type="reconcile_publication", payload={"publication_id": p["id"]},
                    due_at=ctx.now(), dedupe_key=f"reconcile:{p['id']}:startup")
        else:
            pending = row(conn, "SELECT id FROM jobs WHERE type='publication_status' AND status IN ('pending','running') "
                                "AND payload LIKE ?", (f'%"publication_id": {p["id"]}%',))
            if not pending:
                enqueue(conn, queue="light", type="publication_status", payload={"publication_id": p["id"], "n": 0},
                        due_at=ctx.now(), dedupe_key=f"pubstatus:{p['id']}:startup{ctx.now().timestamp():.0f}",
                        max_attempts=1)


def run_publish_slots(ctx: Ctx) -> list[str]:
    """Mode A: unattended client/owned publishing at configured local times.
    Missed slots are skipped, never caught up in a burst."""
    conn, st, now = ctx.conn, ctx.settings, ctx.now()
    tz = ZoneInfo(st.timezone)
    local = now.astimezone(tz)
    log = []
    for op in opportunities.list_all(conn):
        if op["status"] != "active" or op["revenue_route"] == "campaign":
            continue
        for hhmm in op.get("posting_times") or []:
            try:
                h, m = (int(x) for x in str(hhmm).split(":"))
            except ValueError:
                continue
            slot_local = local.replace(hour=h, minute=m, second=0, microsecond=0)
            key = f"{op['id']}:{slot_local.date().isoformat()}:{h:02d}:{m:02d}"
            if slot_local > local or row(conn, "SELECT 1 FROM publish_slots WHERE slot_key=?", (key,)):
                continue
            if local - slot_local > timedelta(minutes=st.publish_slot_grace_minutes):
                outcome, pub_id = "skipped:missed", None
            else:
                live = is_live(conn, st.mode, op["id"])
                acct = op["account_ref"] if live else f"dry:{op['account_ref'] or op['id']}"
                clip = row(
                    conn,
                    "SELECT c.* FROM clips c WHERE c.opportunity_id=? AND c.stage='ready' AND NOT EXISTS ("
                    "  SELECT 1 FROM publications p WHERE p.clip_id=c.id AND p.account_ref=? "
                    "  AND p.status NOT IN ('failed','cancelled')) "
                    "ORDER BY c.score_total DESC, c.id LIMIT 1",
                    (op["id"], acct),
                )
                if not clip:
                    outcome, pub_id = "skipped:no ready clip", None
                else:
                    pub_id, reasons = publish_clip(ctx, clip["id"])
                    outcome = "published" if pub_id else "skipped:" + "; ".join(reasons)[:400]
            with tx(conn):
                conn.execute("INSERT OR IGNORE INTO publish_slots(slot_key, opportunity_id, publication_id, outcome, "
                             "created_at) VALUES (?,?,?,?,?)", (key, op["id"], pub_id, outcome, iso(now)))
            log.append(f"{key}: {outcome}")
    return log
