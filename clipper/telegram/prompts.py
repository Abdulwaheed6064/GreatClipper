"""Messages the system sends to the owner with action buttons.

* campaign posting prompt: "Ready to post clip X for campaign Y?" (one at a
  time, only inside the configured local hours, re-asked after the snooze)
* clip review: the rendered video with Approve / Reject
* submission task: live link + Submitted / Couldn't submit
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from clipper import opportunities
from clipper.context import Ctx
from clipper.db import iso, kv_get, kv_set, parse_iso, row, tx
from clipper.telegram.api import MAX_UPLOAD_BYTES


# ------------------------------------------------------------------ tokens --
def make_token(ctx: Ctx, payload: dict) -> str:
    """Short handle for callback data (Telegram allows 64 bytes)."""
    with tx(ctx.conn):
        n = (kv_get(ctx.conn, "tg_tok_seq", 0) or 0) + 1
        kv_set(ctx.conn, "tg_tok_seq", n)
        kv_set(ctx.conn, f"tg_tok:{n}", {**payload, "created_at": iso(ctx.now())})
    return str(n)


def get_token(ctx: Ctx, tok: str) -> dict | None:
    return kv_get(ctx.conn, f"tg_tok:{tok}")


# --------------------------------------------------------------- helpers --
def in_window(ctx: Ctx) -> bool:
    try:
        a, b = ctx.settings.telegram_prompt_window.split("-")
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
    except ValueError:
        return True
    local = ctx.now().astimezone(ZoneInfo(ctx.settings.timezone))
    m = local.hour * 60 + local.minute
    lo, hi = ah * 60 + am, bh * 60 + bm
    return lo <= m < hi if lo < hi else (m >= lo or m < hi)


def posts_today(ctx: Ctx) -> int:
    from clipper.publishing.service import day_start_utc

    start = day_start_utc(ctx.now(), ctx.settings.timezone)
    return row(ctx.conn, "SELECT COUNT(*) n FROM publications WHERE created_at>=? AND status NOT IN "
                         "('failed','cancelled')", (iso(start),))["n"]


def campaign_busy(ctx: Ctx) -> str | None:
    s = row(ctx.conn, "SELECT id FROM submissions WHERE status IN ('manual_required','overdue') LIMIT 1")
    if s:
        return f"submission #{s['id']} is waiting for you"
    p = row(ctx.conn, "SELECT p.id FROM publications p JOIN opportunities o ON o.id=p.opportunity_id "
                      "WHERE o.revenue_route='campaign' AND p.status IN ('reserved','sending','accepted','unknown') "
                      "LIMIT 1")
    if p:
        return f"publication #{p['id']} is still in progress"
    return None


def best_ready_clip(ctx: Ctx, op: dict) -> dict | None:
    from clipper.rules.gates import is_live

    acct = op["account_ref"] if is_live(ctx.conn, ctx.settings.mode, op["id"]) else f"dry:{op['account_ref'] or op['id']}"
    return row(ctx.conn, "SELECT c.* FROM clips c WHERE c.opportunity_id=? AND c.stage='ready' AND NOT EXISTS ("
                         " SELECT 1 FROM publications p WHERE p.clip_id=c.id AND p.account_ref=? "
                         " AND p.status NOT IN ('failed','cancelled')) ORDER BY c.score_total DESC, c.id LIMIT 1",
               (op["id"], acct))


# ----------------------------------------------------------- posting prompt --
def send_post_prompt(ctx: Ctx, op: dict, clip: dict) -> bool:
    tok = make_token(ctx, {"kind": "post", "opp": op["id"], "clip": clip["id"]})
    budget = op.get("budget_remaining_minor")
    seen = parse_iso(op.get("budget_observed_at"))
    dry = "" if ctx.settings.mode == "live" else "\n🧪 DRY RUN: nothing is really posted."
    lines = [
        f"🎬 Ready to post for {op['name']}?{dry}",
        f"Clip #{clip['id']}: {clip.get('title') or clip.get('hook') or ''} ({clip['end_s'] - clip['start_s']:.0f}s)",
        "",
        "Before you tap, open the campaign page and check:",
        "1. Rules haven't changed",
        "2. You're still in the campaign",
        "3. Budget is still left",
        "4. You can submit the link within the next 30 min",
    ]
    if budget is not None:
        lines.append(f"\nLast budget seen: ${budget / 100:,.2f} ({(ctx.now() - seen).total_seconds() / 3600:.0f}h ago)"
                     if seen else f"\nLast budget seen: ${budget / 100:,.2f}")
    buttons = []
    if budget is not None:
        buttons.append([("✅ Checked — post now", f"go:{tok}")])
    buttons.append([("💲 Enter budget" if budget is None else "💲 Budget changed", f"gb:{tok}"),
                    ("⏰ Later", f"gl:{tok}")])
    if op.get("campaign_url"):
        buttons.append([("🔗 Open campaign", "url:" + op["campaign_url"])])
    ok = ctx.alerter.send(ctx.conn, ctx.now(), "post_prompt", "\n".join(lines), buttons=buttons)
    if ok:
        with tx(ctx.conn):
            kv_set(ctx.conn, "tg_prompt", {"token": tok, "opp": op["id"], "clip": clip["id"], "sent_at": iso(ctx.now())})
    return ok


def campaign_prompts(ctx: Ctx, p: dict | None = None) -> str:
    """Scheduler job: ask the owner when a campaign clip could go out."""
    if kv_get(ctx.conn, "paused", False):
        return "paused"
    if not in_window(ctx):
        return "outside window"
    pending = kv_get(ctx.conn, "tg_prompt")
    if pending and not pending.get("done"):
        if ctx.now() - parse_iso(pending["sent_at"]) < timedelta(minutes=ctx.settings.telegram_prompt_snooze_minutes):
            return "prompt pending"
    busy = campaign_busy(ctx)
    if busy:
        return busy
    if posts_today(ctx) >= ctx.settings.max_posts_per_day_total:
        return "daily cap reached"
    for op in opportunities.list_all(ctx.conn):
        if op["revenue_route"] != "campaign" or op["status"] != "active":
            continue
        if op.get("approved_terms_version") != op["terms_version"]:
            continue
        clip = best_ready_clip(ctx, op)
        if clip:
            send_post_prompt(ctx, op, clip)
            return f"prompted {op['id']}"
    return "nothing ready"


def go(ctx: Ctx, opp_id: str, clip_id: int, budget_minor: int | None = None) -> tuple[int | None, list[str]]:
    """Owner confirmed readiness from Telegram → record it and publish."""
    from clipper.db import audit
    from clipper.publishing.service import publish_clip

    op = opportunities.get(ctx.conn, opp_id)
    now = ctx.now()
    with tx(ctx.conn):
        amount = budget_minor if budget_minor is not None else op.get("budget_remaining_minor")
        ctx.conn.execute("UPDATE opportunities SET budget_remaining_minor=?, budget_currency=COALESCE(budget_currency,'USD'), "
                         "budget_observed_at=? WHERE id=?", (amount, iso(now), opp_id))
        audit(ctx.conn, "owner:telegram", "opportunity.budget_observed",
              {"id": opp_id, "minor": amount, "how": "entered" if budget_minor is not None else "confirmed unchanged"})
        cur = ctx.conn.execute(
            "INSERT INTO operator_ready(opportunity_id, clip_id, checklist, acknowledged_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (opp_id, clip_id, '{"via":"telegram","terms":true,"admission":true,"disclosure":true,"available":true}',
             iso(now), iso(now + timedelta(minutes=ctx.settings.operator_ready_expires_minutes))))
        rid = cur.lastrowid
        pend = kv_get(ctx.conn, "tg_prompt")
        if pend:
            kv_set(ctx.conn, "tg_prompt", {**pend, "done": True})
    return publish_clip(ctx, clip_id, readiness_id=rid)


def snooze(ctx: Ctx) -> None:
    with tx(ctx.conn):
        pend = kv_get(ctx.conn, "tg_prompt")
        if pend:
            kv_set(ctx.conn, "tg_prompt", {**pend, "done": False, "sent_at": iso(ctx.now())})


# ------------------------------------------------------------- clip review --
def review_buttons(cid: int) -> list:
    return [[("✅ Approve", f"ca:{cid}"), ("❌ Reject", f"cr:{cid}")]]


def send_review(ctx: Ctx, p: dict) -> None:
    """Light job: send a clip that needs review to Telegram as a video."""
    from clipper.publishing.service import compose_caption

    cid = p["clip_id"]
    clip = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
    if not clip or clip["stage"] != "checked" or clip["review_state"] != "pending":
        return
    op = opportunities.get(ctx.conn, clip["opportunity_id"])
    flags = []
    if clip["context_risk"]:
        flags.append("⚠️ might be misleading without context")
    if clip["preapproval_state"] == "pending":
        flags.append("📨 campaign needs to pre-approve this draft")
    caption = (f"🎞 Review clip #{cid} · {op['name']}\n{clip['title'] or ''}\n"
               f"Hook: {clip['hook'] or ''}\nLength {clip['end_s'] - clip['start_s']:.0f}s · score {clip['score_total']}\n"
               + ("\n".join(flags) + "\n" if flags else "") +
               f"\nCaption that will be posted:\n{compose_caption(op, clip)}")
    buttons = review_buttons(cid)
    if clip["preapproval_state"] == "pending":
        buttons.append([("📨 Manager approved", f"pa:{cid}"), ("📨 Manager rejected", f"pr:{cid}")])
    path = Path(clip["render_path"] or "")
    tg = ctx.tg
    if tg is None:
        return
    if path.exists() and path.stat().st_size <= MAX_UPLOAD_BYTES:
        tg.send_video(path, caption, buttons)
    else:
        tg.send_message(caption + "\n(video too large for Telegram; preview it on the dashboard)", buttons)


# -------------------------------------------------------------- submission --
def submission_buttons(sub: dict) -> list:
    b = [[("✅ I submitted it", f"sd:{sub['id']}"), ("❌ Couldn't submit", f"sm:{sub['id']}")]]
    links = []
    if sub.get("campaign_url"):
        links.append(("🔗 Campaign page", "url:" + sub["campaign_url"]))
    if sub.get("live_url") and "dry-run.invalid" not in sub["live_url"]:
        links.append(("▶️ Open post", "url:" + sub["live_url"]))
    if links:
        b.append(links)
    return b
