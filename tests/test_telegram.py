"""Telegram control bot: intake → approval → sources → review → post prompt → submission."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from clipper import opportunities
from clipper.db import iso, kv_get, row, rows, tx
from clipper.sources.links import LinkRefused, normalize_source_url
from clipper.telegram import prompts
from clipper.telegram.bot import Bot, state
from tests.conftest import FakeAnthropic, drain
from tests.test_publishing import campaign, healthy_alerts, make_ready_clip

CAMP_URL = "https://contentrewards.com/c/campaigns/cc01471a-0dd5-471d-95b7-90955b037974"


def draft(**over):
    proposed = {
        "name": "Social Media Justice Reach", "brand": "AJ's Mindful Podcast", "campaign_url": CAMP_URL,
        "platforms_allowed": ["tiktok", "instagram", "youtube"], "required_tags": ["#ad"],
        "disclosure_required": "caption", "min_duration_s": 15, "max_duration_s": 60,
        "permitted_audio": "original audio only", "prohibited": "No other footage", "requires_preapproval": False,
        "payment_model": "cpm", "currency": "USD", "rate": "$2 per 1,000 views", "qualifying_view_definition": "unknown",
        "minimum_payable": "unknown", "minimum_semantics": "unknown", "minimum_views": 0, "max_per_clip": "750",
        "creator_fee_rate": "unknown", "budget_total": "20800", "budget_remaining": "19,800",
        "submission_deadline_minutes": 30, "earning_window": "unknown",
        "source_material": "Podcast episodes in the campaign resources folder",
        "editorial_notes": "Clips about social-media addiction",
    }
    proposed.update(over)
    return {"proposed": proposed, "evidence": [{"field": "rate", "excerpt": "$2 per 1K views"}],
            "unresolved_questions": [],
            "assessment": {"recommendation": "worth_it", "summary": "Good rate and lots of budget left.",
                           "reasons": ["$2 CPM", "~$19.8K left"], "risks": ["new account reach"]}}


def msg(text="", **extra):
    return {"message": {"chat": {"id": 42}, "text": text, **extra}}


def press(bot, data, mid=7):
    bot.handle({"callback_query": {"id": "cb", "data": data, "from": {"id": 42},
                                   "message": {"chat": {"id": 42}, "message_id": mid}}})


def buttons_of(entry):
    return [d for row_ in (entry[1] or []) for _, d in row_]


@pytest.fixture
def bot(ctx):
    return Bot(ctx, ctx.tg)


def test_ignores_other_chats(bot, ctx):
    bot.handle({"message": {"chat": {"id": 999}, "text": CAMP_URL}})
    bot.handle({"callback_query": {"id": "x", "data": "ia:1", "from": {"id": 999},
                                   "message": {"chat": {"id": 999}, "message_id": 1}}})
    assert ctx.tg.messages == [] and state(ctx)["mode"] == "idle"


def test_campaign_intake_to_sources(bot, ctx):
    ctx.editor.client = FakeAnthropic([draft()])
    bot.handle(msg(f"new one {CAMP_URL}"))
    assert state(ctx)["mode"] == "intake"
    bot.handle(msg("Pays $2 per 1K views. Must include #ad. Use only podcast footage."))
    bot.handle(msg("", photo=[{"file_id": "small"}, {"file_id": "big"}]))
    bot.handle(msg("/done"))
    call = ctx.editor.client.messages.calls[0]
    kinds = [b["type"] for b in call["messages"][0]["content"]]
    assert "image" in kinds and "text" in kinds
    proposal = ctx.tg.messages[-1]
    assert "Worth trying" in proposal[0] and "#ad" in proposal[0]
    assert "~$" in proposal[0]                                       # CPM estimate shown
    assert "ia:1" in buttons_of(proposal)
    press(bot, "ia:1")
    op = opportunities.list_all(ctx.conn)[0]
    assert op["status"] == "active" and op["approved_terms_version"] == op["terms_version"]
    assert op["revenue_route"] == "campaign" and op["campaign_url"] == CAMP_URL
    assert op["publisher"] == "instagram" and op["account_ref"] == "1784000"
    assert op["payment"]["rate"] == "2" and op["payment"]["fee_rate"] == "0.10"   # platform fee, with evidence
    assert op["budget_remaining_minor"] == 1_980_000
    assert state(ctx) == {"mode": "sources", "opp": op["id"]}

    bot.handle(msg("https://www.youtube.com/watch?v=abc"))
    assert "not a download" in ctx.tg.last
    bot.handle(msg("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing"))
    ask = ctx.tg.messages[-1]
    assert "drive.google.com" in ask[0]
    press(bot, buttons_of(ask)[0])
    src = row(ctx.conn, "SELECT * FROM sources")
    assert src["download_url"] == "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
    assert src["stage"] == "discovered"
    assert "*.googleusercontent.com" in kv_get(ctx.conn, "extra_source_hosts")
    bot.handle(msg("/done"))
    assert state(ctx)["mode"] == "idle"


def test_unknown_payment_blocks_until_answered(bot, ctx):
    ctx.editor.client = FakeAnthropic([draft(payment_model="unknown", disclosure_required="unknown"),
                                       draft()])
    bot.handle(msg(CAMP_URL))
    bot.handle(msg("some rules"))
    bot.handle(msg("/done"))
    first = ctx.tg.messages[-1]
    assert "ia:1" not in buttons_of(first) and "How does it pay" in first[0]
    press(bot, "ie:1")
    bot.handle(msg("It pays $2 CPM and needs #ad in the caption"))
    second = ctx.editor.client.messages.calls[1]["messages"][0]["content"]
    assert any("owner_corrections" in b.get("text", "") for b in second)
    assert "ia:1" in buttons_of(ctx.tg.messages[-1])


def test_post_prompt_publish_and_submission(bot, ctx):
    campaign(ctx)
    healthy_alerts(ctx)
    cid = make_ready_clip(ctx, "camp")
    ctx.clock.t = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)      # 12:00 in Toronto
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE opportunities SET budget_observed_at=? WHERE id='camp'", (iso(ctx.now()),))
    assert prompts.campaign_prompts(ctx) == "prompted camp"
    assert prompts.campaign_prompts(ctx) == "prompt pending"           # one at a time
    go = next(d for d in ctx.alerts.buttons[-1][0] if True)[1]
    assert go.startswith("go:")
    press(bot, go)
    pub = row(ctx.conn, "SELECT * FROM publications")
    assert pub and pub["clip_id"] == cid and "Posting" in ctx.tg.last
    ctx.clock.advance(seconds=20)
    drain(ctx, "light")
    sub = row(ctx.conn, "SELECT * FROM submissions")
    btns = [d for r in ctx.alerts.buttons[-1] for _, d in r]
    assert f"sd:{sub['id']}" in btns
    press(bot, f"sd:{sub['id']}")
    assert state(ctx)["mode"] == "receipt"
    bot.handle(msg("", photo=[{"file_id": "receipt"}]))
    s = row(ctx.conn, "SELECT * FROM submissions")
    assert s["status"] == "submitted" and "screenshot saved" in s["receipt"]


def test_budget_changed_path(bot, ctx):
    campaign(ctx)
    healthy_alerts(ctx)
    make_ready_clip(ctx, "camp")
    ctx.clock.t = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)
    prompts.campaign_prompts(ctx)
    gb = [d for r in ctx.alerts.buttons[-1] for _, d in r if d.startswith("gb:")][0]
    press(bot, gb)
    bot.handle(msg("$1,234.50"))
    assert row(ctx.conn, "SELECT budget_remaining_minor FROM opportunities WHERE id='camp'")["budget_remaining_minor"] == 123450
    assert row(ctx.conn, "SELECT COUNT(*) n FROM publications")["n"] == 1


def test_no_prompt_outside_hours_or_when_busy(ctx):
    campaign(ctx)
    healthy_alerts(ctx)
    make_ready_clip(ctx, "camp")
    ctx.clock.t = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)       # 02:00 Toronto
    assert prompts.campaign_prompts(ctx) == "outside window"
    ctx.clock.t = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)
    ctx.settings.max_posts_per_day_total = 0
    assert prompts.campaign_prompts(ctx) == "daily cap reached"


def test_clip_review_in_telegram(bot, ctx, tmp_path):
    campaign(ctx)
    cid = make_ready_clip(ctx, "camp")
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x" * 100)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE clips SET stage='checked', review_state='pending', public_token=NULL, "
                         "render_path=? WHERE id=?", (str(video), cid))
    prompts.send_review(ctx, {"clip_id": cid})
    path, caption, btns = ctx.tg.videos[-1]
    assert "#ad" in caption and [d for r in btns for _, d in r] == [f"ca:{cid}", f"cr:{cid}"]
    press(bot, f"ca:{cid}")
    c = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    assert c["review_state"] == "approved" and c["stage"] == "ready"


def test_link_normalisation():
    assert normalize_source_url("https://www.dropbox.com/s/abc/ep1.mp4?dl=0") == "https://www.dropbox.com/s/abc/ep1.mp4?dl=1"
    with pytest.raises(LinkRefused):
        normalize_source_url("https://drive.google.com/drive/folders/xyz")
    with pytest.raises(LinkRefused):
        normalize_source_url("https://www.tiktok.com/@x/video/1")
    assert normalize_source_url("https://cdn.example.com/a.mp4") == "https://cdn.example.com/a.mp4"


def test_setup_and_status_commands(bot, ctx):
    bot.handle(msg("/start"))
    assert any("Setup checklist" in m for m, _ in ctx.tg.messages)
    assert row(ctx.conn, "SELECT status FROM capabilities WHERE key='alerts.telegram'")["status"] == "verified"
    bot.handle(msg("/status"))
    assert "Mode: dry_run" in ctx.tg.last
    bot.handle(msg("/pause"))
    assert kv_get(ctx.conn, "paused") is True
    assert rows(ctx.conn, "SELECT * FROM audit_log WHERE action='system.pause'")
