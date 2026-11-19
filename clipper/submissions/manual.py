"""Manual Content Rewards submission adapter.

Content Rewards documents no API, so this adapter never calls it. It returns
``manual_required`` with the live URL, campaign link and deadline, reminds the
owner at 5 and 15 minutes, escalates at 20, and marks the task overdue at the
true deadline. A submission is only recorded as done with evidence (receipt /
confirmation text) entered by the owner.
"""
from __future__ import annotations

from datetime import timedelta

from clipper import opportunities
from clipper.context import Ctx
from clipper.db import audit, iso, parse_iso, row, tx
from clipper.jobs.queue import enqueue

DEFAULT_DEADLINE_MINUTES = 30


def open_submission(ctx: Ctx, pub_id: int) -> dict:
    conn, st, now = ctx.conn, ctx.settings, ctx.now()
    pub = row(conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    op = opportunities.get(conn, pub["opportunity_id"])
    published_at = parse_iso(pub["sent_at"])
    deadline_min = float((op.get("payment") or {}).get("submission_deadline_minutes") or DEFAULT_DEADLINE_MINUTES)
    deadline = published_at + timedelta(minutes=deadline_min)
    target = published_at + timedelta(minutes=min(st.submission_target_minutes, deadline_min))
    with tx(conn):
        conn.execute(
            "INSERT INTO submissions(opportunity_id, publication_id, live_url, campaign_url, published_at, "
            "published_at_estimated, target_at, deadline_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(publication_id) DO NOTHING",
            (op["id"], pub_id, pub["live_url"], op.get("campaign_url"), iso(published_at),
             pub["sent_at_estimated"], iso(target), iso(deadline), iso(now), iso(now)),
        )
        sub = row(conn, "SELECT * FROM submissions WHERE publication_id=?", (pub_id,))
    for m in st.submission_reminder_minutes:
        enqueue(conn, queue="light", type="submission_reminder", payload={"submission_id": sub["id"], "kind": f"remind{m}"},
                due_at=published_at + timedelta(minutes=m), dedupe_key=f"subrem:{sub['id']}:{m}", max_attempts=2)
    enqueue(conn, queue="light", type="submission_reminder", payload={"submission_id": sub["id"], "kind": "escalate"},
            due_at=published_at + timedelta(minutes=st.submission_escalate_minutes),
            dedupe_key=f"subrem:{sub['id']}:esc", max_attempts=2)
    enqueue(conn, queue="light", type="submission_reminder", payload={"submission_id": sub["id"], "kind": "deadline"},
            due_at=deadline, dedupe_key=f"subrem:{sub['id']}:deadline", max_attempts=3)
    left = (deadline - now).total_seconds() / 60
    est = " (publish time ESTIMATED conservatively)" if pub["sent_at_estimated"] else ""
    dry = "[DRY RUN — do not submit] " if pub["dry_run"] else ""
    ctx.alerter.send(
        conn, now, "submission_required",
        f"{dry}MANUAL STEP REQUIRED: submit {pub['live_url']} on {op.get('campaign_url') or op['name']} "
        f"within {left:.0f} min (deadline {deadline.strftime('%H:%M:%S')} UTC{est}). "
        f"Then record the receipt on the dashboard.",
        severity="critical",
    )
    return sub


def reminder(ctx: Ctx, sub_id: int, kind: str) -> None:
    conn, now = ctx.conn, ctx.now()
    sub = row(conn, "SELECT * FROM submissions WHERE id=?", (sub_id,))
    if not sub or sub["status"] in ("submitted", "submitted_late"):
        return
    deadline = parse_iso(sub["deadline_at"])
    left = (deadline - now).total_seconds() / 60
    if kind == "deadline":
        if now >= deadline and sub["status"] == "manual_required":
            with tx(conn):
                conn.execute("UPDATE submissions SET status='overdue', updated_at=? WHERE id=?", (iso(now), sub_id))
            ctx.alerter.send(conn, now, "submission_overdue",
                             f"Submission #{sub_id} is OVERDUE ({sub['live_url']}). Do not delete or re-upload the "
                             f"post to recover; record what happened on the dashboard.", severity="critical")
        return
    sev = "critical" if kind == "escalate" else "warning"
    label = "ESCALATION" if kind == "escalate" else "Reminder"
    ctx.alerter.send(conn, now, "submission_reminder",
                     f"{label}: submission #{sub_id} still unconfirmed — {left:.0f} min left. "
                     f"URL: {sub['live_url']}  Campaign: {sub['campaign_url']}", severity=sev,
                     dedupe_key=f"subrem:{sub_id}:{kind}")


def record_submitted(ctx: Ctx, sub_id: int, receipt: str, *, actor: str = "owner") -> dict:
    receipt = (receipt or "").strip()
    if len(receipt) < 4:
        raise ValueError("enter the confirmation/receipt evidence (e.g. submission ID or screenshot reference)")
    now = ctx.now()
    with tx(ctx.conn):
        sub = row(ctx.conn, "SELECT * FROM submissions WHERE id=?", (sub_id,))
        if sub["status"] in ("submitted", "submitted_late"):
            raise ValueError("already recorded")
        late = now > parse_iso(sub["deadline_at"])
        ctx.conn.execute(
            "UPDATE submissions SET status=?, submitted_at=?, receipt=?, updated_at=? WHERE id=?",
            ("submitted_late" if late else "submitted", iso(now), receipt[:2000], iso(now), sub_id),
        )
        audit(ctx.conn, actor, "submission.recorded", {"id": sub_id, "late": late})
    return row(ctx.conn, "SELECT * FROM submissions WHERE id=?", (sub_id,))


def record_review(ctx: Ctx, sub_id: int, state: str, note: str, *, actor: str = "owner") -> None:
    if state not in ("pending", "approved", "rejected"):
        raise ValueError("bad review state")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE submissions SET review_state=?, review_note=?, updated_at=? WHERE id=?",
                         (state, note[:1000], iso(ctx.now()), sub_id))
        audit(ctx.conn, actor, "submission.review", {"id": sub_id, "state": state})


def record_missed(ctx: Ctx, sub_id: int, note: str, *, actor: str = "owner") -> None:
    """Owner acknowledges a submission that was not made. Unblocks the next
    campaign session; the post itself is left alone (no delete/re-upload)."""
    if len((note or "").strip()) < 4:
        raise ValueError("explain what happened")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE submissions SET status='missed', review_note=?, updated_at=? WHERE id=? "
                         "AND status IN ('manual_required','overdue')", (note[:1000], iso(ctx.now()), sub_id))
        audit(ctx.conn, actor, "submission.missed", {"id": sub_id})
