"""End-to-end: source → transcript → Claude candidates → FFmpeg render → checks
→ review → ready → scheduled publish (dry run)."""
from __future__ import annotations

from clipper.db import kv_set, row, rows, tx
from clipper.jobs.handlers import try_release
from clipper.publishing.service import run_publish_slots
from clipper.sources.feed import add_manual, poll_feed
from tests.conftest import FakeAnthropic, drain, fake_transcript, good_candidates, make_opportunity


def test_feed_discovers_once_and_holds_missing_rights(ctx):
    make_opportunity(ctx, feed_url="https://feed.example.com/feed.json")
    ctx.route.feed = {"videos": [
        {"id": "rec-1", "download_url": "https://media.example.com/rec-1.mp4", "title": "One", "rights_ref": "agr-1"},
        {"id": "rec-2", "download_url": "https://media.example.com/rec-2.mp4", "title": "No rights"},
        {"id": "rec-3", "download_url": "https://evil.example.net/x.mp4", "title": "Bad host", "rights_ref": "agr-1"},
    ]}
    new = poll_feed(ctx, "client-a")
    assert len(new) == 3
    stages = {r["provider_id"]: (r["stage"], r["hold_reason"]) for r in rows(ctx.conn, "SELECT * FROM sources")}
    assert stages["rec-1"][0] == "discovered"
    assert stages["rec-2"][0] == "held" and "rights" in stages["rec-2"][1]
    assert stages["rec-3"][0] == "held" and "allowlist" in stages["rec-3"][1]
    assert poll_feed(ctx, "client-a") == []          # duplicate discovery creates nothing
    assert row(ctx.conn, "SELECT COUNT(*) n FROM jobs WHERE type='acquire_source'")["n"] == 1


def test_expired_rights_stop_processing(ctx):
    make_opportunity(ctx, expires_at="2026-11-01T00:00:00+00:00")
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    drain(ctx)
    src = row(ctx.conn, "SELECT * FROM sources")
    assert src["stage"] == "held" and "expired" in src["hold_reason"]
    assert src["file_path"] is None                    # nothing downloaded


def test_full_pipeline_to_dry_run_publish(ctx):
    make_opportunity(ctx)
    t = fake_transcript(60)
    ctx.editor.client = FakeAnthropic([good_candidates(t)])
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    drain(ctx)

    src = row(ctx.conn, "SELECT * FROM sources")
    assert src["stage"] == "done", src
    clips = rows(ctx.conn, "SELECT * FROM clips ORDER BY id")
    assert len(clips) == 2
    for c in clips:
        assert c["stage"] == "checked", c["hold_reason"]
        assert "calibration" in c["hold_reason"]       # first clip(s) need owner review
    # spend was reserved and committed
    sp = rows(ctx.conn, "SELECT * FROM spend")
    assert sp and all(s["status"] == "committed" for s in sp)
    # editorial request was bounded: structured output + fallbacks on the default model
    call = ctx.editor.client.messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"

    # owner approves the first clip → ready with an opaque public URL
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE clips SET review_state='approved' WHERE id=?", (clips[0]["id"],))
    assert try_release(ctx, clips[0]["id"]) == []
    c0 = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (clips[0]["id"],))
    assert c0["stage"] == "ready" and len(c0["public_token"]) >= 20
    assert (ctx.env.public_dir / f"{c0['public_token']}.mp4").exists()
    # calibration satisfied (count=1) → second clip releases automatically now
    assert try_release(ctx, clips[1]["id"]) == []

    log = run_publish_slots(ctx)
    assert any("published" in l for l in log), log
    drain(ctx, "light")
    assert row(ctx.conn, "SELECT status FROM publications")["status"] == "accepted"  # first check due at +15s
    ctx.clock.advance(seconds=20)
    drain(ctx, "light")
    pub = row(ctx.conn, "SELECT * FROM publications")
    assert pub["dry_run"] == 1 and pub["status"] == "sent" and pub["live_url"].startswith("https://dry-run.invalid/")
    assert pub["media_url"].startswith("https://clips.example.com/m/")
    assert ctx.fake_buffer.calls == []                 # dry run never contacts Buffer
    # same slot does not publish twice; missed slots are not caught up
    assert run_publish_slots(ctx) == []


def test_reused_media_is_not_transcribed_twice(ctx):
    make_opportunity(ctx)
    t = fake_transcript(60)
    ctx.editor.client = FakeAnthropic([good_candidates(t), good_candidates(t)])
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    add_manual(ctx, "client-a", "rec-2", "https://media.example.com/b.mp4", "B (same bytes)", "agr-1")
    drain(ctx)
    assert ctx.transcriber.calls == 1
    # identical media + same terms → editorial cache hit, model billed once
    assert len(ctx.editor.client.messages.calls) == 1


def test_invalid_model_output_is_repaired_once_then_held(ctx):
    make_opportunity(ctx)
    bad = {"candidates": [{"start_s": 50, "end_s": 400, "hook": "", "payoff": "", "title": "", "caption": "",
                           "hashtags": [], "evidence_quote": "invented words here",
                           "scores": {"clarity": 5, "context": 5, "payoff": 5, "distinctness": 5, "campaign_fit": 5},
                           "context_risk": False, "notes": ""}]}
    ctx.editor.client = FakeAnthropic([bad, "not json"])
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    drain(ctx)
    assert len(ctx.editor.client.messages.calls) == 2  # original + exactly one repair
    src = row(ctx.conn, "SELECT * FROM sources")
    assert src["stage"] == "held" and "no usable candidates" in src["hold_reason"]
    assert row(ctx.conn, "SELECT COUNT(*) n FROM clips")["n"] == 0


def test_low_confidence_transcript_goes_to_review(ctx):
    make_opportunity(ctx)
    ctx.transcriber.lp = 0.3
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    drain(ctx)
    src = row(ctx.conn, "SELECT * FROM sources")
    assert src["stage"] == "held" and "language confidence" in src["hold_reason"]


def test_changed_layout_is_held(ctx):
    make_opportunity(ctx, layout={"template": "gameplay_facecam", "expected_width": 1920, "expected_height": 1080,
                                  "facecam": {"x": 0, "y": 0, "w": 480, "h": 270},
                                  "gameplay": {"x": 240, "y": 0, "w": 1440, "h": 1080}})
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    drain(ctx)
    src = row(ctx.conn, "SELECT * FROM sources")
    assert src["stage"] == "held" and "layout changed" in src["hold_reason"]


def test_terms_change_holds_ready_clips(ctx):
    from clipper import opportunities

    make_opportunity(ctx)
    t = fake_transcript(60)
    ctx.editor.client = FakeAnthropic([good_candidates(t)])
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    drain(ctx)
    cid = row(ctx.conn, "SELECT id FROM clips ORDER BY id LIMIT 1")["id"]
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE clips SET review_state='approved' WHERE id=?", (cid,))
    try_release(ctx, cid)
    opportunities.update(ctx.conn, ctx.now(), "client-a", {"required_tags": ["#ad"]})
    c = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    assert c["stage"] == "held" and "terms changed" in c["hold_reason"]
    op = opportunities.get(ctx.conn, "client-a")
    assert op["approved_terms_version"] is None and op["terms_version"] == 2


def test_daily_source_minutes_cap_defers(ctx):
    ctx.settings.max_source_minutes_per_day = 1
    make_opportunity(ctx)
    ctx.editor.client = FakeAnthropic([good_candidates(fake_transcript(60))] * 2)
    add_manual(ctx, "client-a", "rec-1", "https://media.example.com/a.mp4", "A", "agr-1")
    add_manual(ctx, "client-a", "rec-2", "https://media.example.com/b.mp4", "B", "agr-1")
    drain(ctx)
    job = row(ctx.conn, "SELECT * FROM jobs WHERE type='acquire_source' AND status='pending'")
    assert job and "daily source-minutes" in job["last_error"]
