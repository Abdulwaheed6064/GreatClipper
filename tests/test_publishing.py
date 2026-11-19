"""Publishing state machine, campaign gates, deadlines, recovery (section 14)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from clipper.db import iso, jdump, kv_set, row, rows, tx
from clipper.jobs.queue import enqueue, reserve
from clipper.publishing.service import publish_clip, reconcile, resolve_unknown, startup_reconcile
from clipper.submissions.manual import record_submitted
from tests.conftest import drain, make_opportunity


def make_ready_clip(ctx, opp_id, n=1, **over):
    now = iso(ctx.now())
    with tx(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO sources(opportunity_id, provider_id, rights_ref, stage, discovered_at, updated_at) "
            "VALUES (?, ?, 'agr', 'done', ?, ?)", (opp_id, f"src-{n}-{over.get('tag', '')}", now, now))
        sid = cur.lastrowid
        op = row(ctx.conn, "SELECT terms_version FROM opportunities WHERE id=?", (opp_id,))
        token = f"tok{'x' * 20}{sid}{n}"
        (ctx.env.public_dir / f"{token}.mp4").write_bytes(b"fake")
        cur = ctx.conn.execute(
            "INSERT INTO clips(source_id, opportunity_id, start_s, end_s, title, caption, hashtags, terms_version, "
            "template_version, stage, review_state, public_token, render_path, render_hash, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, opp_id, 0, 20, "T", over.get("caption", "Caption"), jdump([]), op["terms_version"],
             ctx.settings.template_version, "ready", "approved", token, "/dev/null", f"h{sid}{n}", now, now))
        return cur.lastrowid


def go_live(ctx, opp_id, *caps):
    ctx.settings.mode = "live"
    with tx(ctx.conn):
        kv_set(ctx.conn, "live_opportunities", [opp_id])
        for c in caps:
            ctx.conn.execute("INSERT OR REPLACE INTO capabilities(key, status, evidence, updated_at) VALUES "
                             "(?, 'verified', 'test', ?)", (c, iso(ctx.now())))


def healthy_alerts(ctx):
    ctx.alerter.send(ctx.conn, ctx.now(), "test", "test")


BUFFER_CAPS = ("buffer.automatic_publish", "buffer.external_link")


# ------------------------------------------------------------ client route --
def test_live_buffer_publish_stores_provider_id_and_url(ctx):
    make_opportunity(ctx)
    cid = make_ready_clip(ctx, "client-a")
    go_live(ctx, "client-a", *BUFFER_CAPS)
    ctx.fake_buffer.sent_at = "2026-11-19T15:00:05Z"
    pub_id, reasons = publish_clip(ctx, cid)
    assert reasons == [] and pub_id
    inp = ctx.fake_buffer.posts["p1"]
    assert inp["schedulingType"] == "automatic" and inp["mode"] == "shareNow"
    assert inp["assets"]["videos"][0]["url"].startswith("https://clips.example.com/m/")
    ctx.clock.advance(seconds=20)
    drain(ctx, "light")
    pub = row(ctx.conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    assert pub["status"] == "sent" and pub["provider_id"] == "p1"
    assert pub["live_url"] == "https://instagram.com/reel/p1" and pub["sent_at_estimated"] == 0
    assert row(ctx.conn, "SELECT stage FROM clips WHERE id=?", (cid,))["stage"] == "published"
    # the same clip can never be published twice to the account
    _, reasons = publish_clip(ctx, cid)
    assert any("stage is published" in r for r in reasons)


def test_unverified_capability_blocks_live(ctx):
    make_opportunity(ctx)
    cid = make_ready_clip(ctx, "client-a")
    go_live(ctx, "client-a")  # no capabilities verified
    pub_id, reasons = publish_clip(ctx, cid)
    assert pub_id is None and any("buffer.automatic_publish" in r for r in reasons)
    assert ctx.fake_buffer.calls == []


def test_native_disclosure_unsupported_blocks_release(ctx):
    make_opportunity(ctx, disclosure_required="native_paid_partnership")
    cid = make_ready_clip(ctx, "client-a")
    go_live(ctx, "client-a", *BUFFER_CAPS)
    _, reasons = publish_clip(ctx, cid)
    assert any("native paid-partnership" in r for r in reasons)


def test_ambiguous_send_reconciles_and_never_duplicates(ctx):
    make_opportunity(ctx)
    cid = make_ready_clip(ctx, "client-a")
    cid2 = make_ready_clip(ctx, "client-a", n=2)
    go_live(ctx, "client-a", *BUFFER_CAPS)
    ctx.fake_buffer.mode = "timeout_on_create"
    pub_id, reasons = publish_clip(ctx, cid)
    assert reasons == []
    assert row(ctx.conn, "SELECT status FROM publications WHERE id=?", (pub_id,))["status"] == "unknown"
    assert any("outcome unknown" in m for m in ctx.alerts.sent)
    ctx.fake_buffer.mode = "ok"
    # nothing new may publish while a send is unresolved
    _, reasons = publish_clip(ctx, cid2)
    assert any("unknown state" in r for r in reasons)
    # reconciliation does not find it → stays unknown, no blind retry
    assert reconcile(ctx, pub_id) == "unknown"
    creates = [c for c in ctx.fake_buffer.calls if "CreatePost" in c]
    assert len(creates) == 1
    # owner checks the account and confirms it is live
    resolve_unknown(ctx, pub_id, published=True, live_url="https://instagram.com/reel/abc")
    assert row(ctx.conn, "SELECT status FROM publications WHERE id=?", (pub_id,))["status"] == "sent"
    _, reasons = publish_clip(ctx, cid2)
    assert reasons == []


def test_expired_credentials_pause_only_affected_route(ctx):
    make_opportunity(ctx)
    make_opportunity(ctx, "owned-b", revenue_route="owned", publisher="manual", account_ref=None)
    cid = make_ready_clip(ctx, "client-a")
    cid_b = make_ready_clip(ctx, "owned-b", n=3)
    go_live(ctx, "client-a", *BUFFER_CAPS)
    ctx.fake_buffer.mode = "unauthorized"
    pub_id, _ = publish_clip(ctx, cid)
    assert row(ctx.conn, "SELECT status FROM publications WHERE id=?", (pub_id,))["status"] == "failed"
    clip = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    assert clip["stage"] == "held" and "credentials" in clip["hold_reason"]
    assert any("failed (not sent)" in m for m in ctx.alerts.sent)
    # other route (dry run / owned) still works
    pid_b, reasons = publish_clip(ctx, cid_b)
    assert pid_b and reasons == []


def test_buffer_request_budget_protects_reserve(ctx):
    make_opportunity(ctx)
    cid = make_ready_clip(ctx, "client-a")
    go_live(ctx, "client-a", *BUFFER_CAPS)
    with tx(ctx.conn):
        for _ in range(ctx.settings.buffer_requests_per_30d - ctx.settings.buffer_reserve_requests - 3):
            ctx.conn.execute("INSERT INTO api_requests(provider, channel, purpose, created_at) VALUES "
                             "('buffer','chan1','status',?)", (iso(ctx.now()),))
    pub_id, _ = publish_clip(ctx, cid)
    pub = row(ctx.conn, "SELECT * FROM publications WHERE id=?", (pub_id,))
    assert pub["status"] == "failed" and "request budget" in pub["last_error"]
    assert ctx.fake_buffer.calls == []


def test_daily_cap(ctx):
    ctx.settings.max_posts_per_day_total = 1
    make_opportunity(ctx)
    a, b = make_ready_clip(ctx, "client-a"), make_ready_clip(ctx, "client-a", n=2)
    assert publish_clip(ctx, a)[1] == []
    assert any("daily cap" in r for r in publish_clip(ctx, b)[1])


def test_pause_blocks_everything(ctx):
    make_opportunity(ctx)
    cid = make_ready_clip(ctx, "client-a")
    with tx(ctx.conn):
        kv_set(ctx.conn, "paused", True)
    assert any("paused" in r for r in publish_clip(ctx, cid)[1])


# --------------------------------------------------------- campaign route --
def campaign(ctx, **over):
    vals = dict(revenue_route="campaign", campaign_url="https://contentrewards.com/c/campaigns/x",
                required_tags=["@Coinbase", "#ad"],
                payment={"model": "cpm", "currency": "USD", "rate": "1.50", "fee_rate": "0.10",
                         "submission_deadline_minutes": 30})
    vals.update(over)
    op = make_opportunity(ctx, "camp", **vals)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE opportunities SET budget_remaining_minor=300000, budget_currency='USD', "
                         "budget_observed_at=? WHERE id='camp'", (iso(ctx.now()),))
    return op


def ready_ack(ctx, cid, minutes=5):
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO operator_ready(opportunity_id, clip_id, checklist, acknowledged_at, "
                               "expires_at) VALUES ('camp', ?, '{}', ?, ?)",
                               (cid, iso(ctx.now()), iso(ctx.now() + timedelta(minutes=minutes))))
        return cur.lastrowid


def test_campaign_requires_fresh_readiness_and_healthy_alerts(ctx):
    campaign(ctx)
    cid = make_ready_clip(ctx, "camp")
    _, reasons = publish_clip(ctx, cid)
    assert any("readiness" in r for r in reasons)
    assert any("alert channel" in r for r in reasons)      # never tested
    healthy_alerts(ctx)
    rid = ready_ack(ctx, cid)
    ctx.clock.advance(minutes=6)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE opportunities SET budget_observed_at=? WHERE id='camp'", (iso(ctx.now()),))
    _, reasons = publish_clip(ctx, cid, readiness_id=rid)
    assert any("expired" in r for r in reasons)
    rid = ready_ack(ctx, cid)
    pub_id, reasons = publish_clip(ctx, cid, readiness_id=rid)
    assert reasons == [] and pub_id
    # readiness is single-use
    assert row(ctx.conn, "SELECT used_at FROM operator_ready WHERE id=?", (rid,))["used_at"]


def test_campaign_stale_budget_and_unknown_terms_block(ctx):
    from clipper import opportunities

    campaign(ctx)
    healthy_alerts(ctx)
    cid = make_ready_clip(ctx, "camp")
    ctx.clock.advance(minutes=45)
    rid = ready_ack(ctx, cid)
    _, reasons = publish_clip(ctx, cid, readiness_id=rid)
    assert any("budget observation is stale" in r for r in reasons)
    opportunities.update(ctx.conn, ctx.now(), "camp", {"terms_text": "changed terms"})
    _, reasons = publish_clip(ctx, cid, readiness_id=rid)
    assert any("not confirmed" in r for r in reasons)


def test_missing_required_mention_blocks(ctx):
    campaign(ctx, caption_template="{caption}")      # template drops the required tags
    healthy_alerts(ctx)
    cid = make_ready_clip(ctx, "camp")
    _, reasons = publish_clip(ctx, cid, readiness_id=ready_ack(ctx, cid))
    assert any("@Coinbase" in r for r in reasons)


def test_deadline_from_actual_publish_time_and_reminders(ctx):
    campaign(ctx)
    healthy_alerts(ctx)
    a = make_ready_clip(ctx, "camp")
    b = make_ready_clip(ctx, "camp", n=2)
    go_live(ctx, "camp", *BUFFER_CAPS)
    ctx.fake_buffer.sent_at = "2026-11-19T15:00:03Z"
    pub_id, reasons = publish_clip(ctx, a, readiness_id=ready_ack(ctx, a))
    assert reasons == []
    # publisher answers late: the app notices at +3 min, deadline still counts from the real sentAt
    ctx.clock.advance(minutes=3)
    drain(ctx, "light")
    sub = row(ctx.conn, "SELECT * FROM submissions")
    assert sub["published_at"] == "2026-11-19T15:00:03+00:00"
    assert sub["deadline_at"] == "2026-11-19T15:30:03+00:00"
    assert sub["status"] == "manual_required"
    assert any("MANUAL STEP REQUIRED" in m for m in ctx.alerts.sent)
    # the next campaign clip is blocked until the submission is confirmed
    _, reasons = publish_clip(ctx, b, readiness_id=ready_ack(ctx, b))
    assert any("not confirmed yet" in r for r in reasons)
    # reminders at 5 and 15, escalation at 20, overdue at the true deadline
    for minutes in (3, 10, 5, 11):
        ctx.clock.advance(minutes=minutes)
        drain(ctx, "light")
    texts = " | ".join(ctx.alerts.sent)
    assert "Reminder" in texts and "ESCALATION" in texts and "OVERDUE" in texts
    assert row(ctx.conn, "SELECT status FROM submissions")["status"] == "overdue"
    # a submission is never recorded without evidence
    with pytest.raises(ValueError):
        record_submitted(ctx, sub["id"], "")
    s = record_submitted(ctx, sub["id"], "CR receipt #12345")
    assert s["status"] == "submitted_late"


def test_missing_sent_at_uses_earliest_plausible_time(ctx):
    campaign(ctx)
    healthy_alerts(ctx)
    a = make_ready_clip(ctx, "camp")
    go_live(ctx, "camp", *BUFFER_CAPS)
    ctx.fake_buffer.sent_at = None
    pub_id, _ = publish_clip(ctx, a, readiness_id=ready_ack(ctx, a))
    ctx.clock.advance(minutes=4)
    drain(ctx, "light")
    sub = row(ctx.conn, "SELECT * FROM submissions")
    assert sub["published_at_estimated"] == 1
    assert sub["published_at"] == "2026-11-19T15:00:00+00:00"   # request start, not when the app noticed


def test_dead_alert_channel_blocks_campaign_publication(ctx):
    campaign(ctx)
    healthy_alerts(ctx)
    ctx.alerts.fail = True
    ctx.alerter.send(ctx.conn, ctx.now(), "x", "this one fails")
    cid = make_ready_clip(ctx, "camp")
    _, reasons = publish_clip(ctx, cid, readiness_id=ready_ack(ctx, cid))
    assert any("alert channel unhealthy" in r for r in reasons)


def test_one_post_cannot_be_submitted_to_two_campaigns(ctx):
    import sqlite3

    campaign(ctx)
    healthy_alerts(ctx)
    a = make_ready_clip(ctx, "camp")
    pub_id, _ = publish_clip(ctx, a, readiness_id=ready_ack(ctx, a))
    ctx.clock.advance(seconds=20)
    drain(ctx, "light")
    sub = row(ctx.conn, "SELECT * FROM submissions")
    with pytest.raises(sqlite3.IntegrityError):
        ctx.conn.execute("INSERT INTO submissions(opportunity_id, publication_id, live_url, published_at, target_at, "
                         "deadline_at, created_at, updated_at) VALUES ('camp', ?, ?, 'x','x','x','x','x')",
                         (pub_id + 100, sub["live_url"]))


# ---------------------------------------------------------------- restart --
def test_restart_recovers_without_republishing(ctx):
    make_opportunity(ctx)
    a, b, c = (make_ready_clip(ctx, "client-a", n=i) for i in (1, 2, 3))
    now = iso(ctx.now())
    with tx(ctx.conn):
        for cid, status in ((a, "reserved"), (b, "sending"), (c, "accepted")):
            ctx.conn.execute(
                "INSERT INTO publications(clip_id, opportunity_id, publisher, account_ref, intent_key, dry_run, status, "
                "provider_id, provider_state, caption, media_url, request_started_at, created_at, updated_at) "
                "VALUES (?, 'client-a', 'dry_run', 'dry:chan1', ?, 1, ?, ?, '{}', 'c', 'u', ?, ?, ?)",
                (cid, f"k{cid}", status, f"dry-k{cid}" if status == "accepted" else None, now, now, now))
    startup_reconcile(ctx)
    st = {r["clip_id"]: r["status"] for r in rows(ctx.conn, "SELECT clip_id, status FROM publications")}
    assert st[a] == "failed"          # never sent → safe
    assert st[b] == "unknown"         # maybe sent → reconcile, never re-send
    drain(ctx, "light")
    st = {r["clip_id"]: r["status"] for r in rows(ctx.conn, "SELECT clip_id, status FROM publications")}
    assert st[c] == "sent"


def test_expired_job_lease_is_recovered(ctx):
    enqueue(ctx.conn, queue="heavy", type="render_clip", payload={"clip_id": 1}, due_at=ctx.now(), dedupe_key="r1")
    job = reserve(ctx.conn, "heavy", "dead-worker", ctx.now(), lease_s=60)
    assert job and reserve(ctx.conn, "heavy", "w2", ctx.now(), 60) is None
    ctx.clock.advance(seconds=61)
    again = reserve(ctx.conn, "heavy", "w2", ctx.now(), 60)
    assert again and again["id"] == job["id"] and again["attempts"] == 2
