"""Campaign intake from Telegram: collect material → Claude proposal + assessment
→ owner approves → opportunity created, terms confirmed, active.

The owner's tap on "Approve" is the human confirmation the plan requires; the
message shows every value that will control posting.
"""
from __future__ import annotations

import base64
import html
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from clipper import opportunities
from clipper.budget import spend
from clipper.context import Ctx
from clipper.db import audit, iso, kv_get, kv_set, row, tx
from clipper.editorial.claude import worst_case_cost_minor
from clipper.ledger import ledger
from clipper.sources.links import is_campaign_link

MAX_IMAGES = 10
MAX_PDFS = 3
MAX_TEXT_CHARS = 60_000


# ------------------------------------------------------------------ storage --
def new_intake(ctx: Ctx, campaign_url: str | None = None) -> dict:
    with tx(ctx.conn):
        n = (kv_get(ctx.conn, "tg_intake_seq", 0) or 0) + 1
        kv_set(ctx.conn, "tg_intake_seq", n)
        intake = {"id": n, "campaign_url": campaign_url, "texts": [], "files": [], "page_text": "",
                  "created_at": iso(ctx.now()), "draft": None, "corrections": []}
        kv_set(ctx.conn, f"tg_intake:{n}", intake)
    return intake


def load(ctx: Ctx, n: int) -> dict | None:
    return kv_get(ctx.conn, f"tg_intake:{n}")


def save(ctx: Ctx, intake: dict) -> None:
    with tx(ctx.conn):
        kv_set(ctx.conn, f"tg_intake:{intake['id']}", intake)


def intake_dir(ctx: Ctx, n: int) -> Path:
    d = ctx.env.data_dir / "intake" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    return d


# -------------------------------------------------------- public page text --
_script_json = re.compile(r"<script[^>]*type=\"application/(?:ld\+)?json\"[^>]*>(.*?)</script>", re.S | re.I)
_drop = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_tags = re.compile(r"<[^>]+>")


def fetch_public_page(ctx: Ctx, url: str) -> str:
    """Best-effort text of a PUBLIC campaign page (no login, no cookies)."""
    if not is_campaign_link(url):
        return ""
    try:
        r = ctx.http.get(url, follow_redirects=True, timeout=20, headers={"Accept": "text/html"})
    except Exception:  # noqa: BLE001 - optional enrichment only
        return ""
    if r.status_code != 200 or not (r.url.host or "").endswith("contentrewards.com"):
        return ""
    raw = r.text[:2_000_000]
    data = " ".join(m[:20_000] for m in _script_json.findall(raw))
    text = _tags.sub(" ", _drop.sub(" ", raw))
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return (text + ("\n\nEmbedded page data: " + data if data else ""))[:30_000]


# ------------------------------------------------------------ Claude call --
def build_blocks(ctx: Ctx, intake: dict) -> tuple[list[dict], str]:
    """Content blocks for Claude plus a plain-text estimate basis for budgeting."""
    blocks: list[dict] = []
    text = "\n\n".join(intake["texts"])[:MAX_TEXT_CHARS]
    header = f"Campaign link: {intake.get('campaign_url') or 'not given'}"
    blocks.append({"type": "text", "text": f"<campaign_material>\n{header}\n\nPasted by the owner from the "
                                           f"campaign page/brief:\n{text or '(no text pasted)'}\n</campaign_material>"})
    if intake.get("page_text"):
        blocks.append({"type": "text", "text": "<public_campaign_page>\n" + intake["page_text"] +
                                               "\n</public_campaign_page>"})
    basis = header + text + intake.get("page_text", "")
    for f in intake["files"]:
        p = Path(f["path"])
        if not p.exists():
            continue
        data = base64.standard_b64encode(p.read_bytes()).decode()
        if f["kind"] == "image":
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": f["mime"], "data": data}})
            basis += "x" * 5200          # ~1,600 tokens per screenshot
        elif f["kind"] == "pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": data}})
            basis += "x" * max(8000, p.stat().st_size // 6)
    return blocks, basis


def run_assessment(ctx: Ctx, intake: dict) -> dict:
    st = ctx.settings
    blocks, basis = build_blocks(ctx, intake)
    rid = spend.reserve(ctx.conn, ctx.now(), category="claude", amount_minor=worst_case_cost_minor(st, basis),
                        ceiling_usd=st.monthly_operating_ceiling_usd,
                        model_allowance_usd=st.monthly_model_allowance_usd, ref=f"intake:{intake['id']}")
    try:
        data, cost = ctx.editor.assess_campaign(blocks, "\n".join(intake["corrections"]) or None)
    except Exception:
        spend.release(ctx.conn, ctx.now(), rid)
        raise
    spend.commit(ctx.conn, ctx.now(), rid, cost)
    intake["draft"] = data
    intake["draft_cost_minor"] = intake.get("draft_cost_minor", 0) + cost
    save(ctx, intake)
    return data


# ---------------------------------------------------------------- mapping --
def _known(v) -> bool:
    return v not in (None, "", "unknown", "Unknown", 0, "0")


def _dec(v) -> str | None:
    if not _known(v):
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(v).replace(",", ""))
    if not m:
        return None
    try:
        return str(Decimal(m.group(0)))
    except InvalidOperation:
        return None


def draft_to_values(ctx: Ctx, intake: dict) -> tuple[dict, list[str], list[str]]:
    """Returns (opportunity values, warnings, blockers)."""
    st, en = ctx.settings, ctx.env
    d = intake["draft"]
    p = d["proposed"]
    warnings: list[str] = []
    blockers: list[str] = []
    defaults = st.campaign_defaults or {}
    publisher = defaults.get("publisher", "instagram")
    platform = defaults.get("platform", "instagram")
    account = defaults.get("account_ref") or (en.instagram_user_id if publisher == "instagram" else "") or ""
    url = intake.get("campaign_url") or (p["campaign_url"] if is_campaign_link(p.get("campaign_url") or "") else None)
    if not url:
        blockers.append("Send me the campaign link (contentrewards.com/…/campaigns/…).")

    allowed = [x.lower() for x in p.get("platforms_allowed") or [] if x]
    if allowed and not any(platform in a for a in allowed):
        blockers.append(f"The campaign lists {', '.join(allowed)} but your destination is {platform}.")

    min_d, max_d = p.get("min_duration_s") or 0, p.get("max_duration_s") or 0
    if not min_d:
        min_d = 15
        warnings.append("Minimum length not stated: using 15 s.")
    if not max_d:
        max_d = 60
        warnings.append("Maximum length not stated: using 60 s.")
    if min_d >= max_d:
        blockers.append(f"Length rules look wrong ({min_d}–{max_d} s).")

    disclosure = p.get("disclosure_required")
    if disclosure == "unknown":
        blockers.append("Is a disclosure required? Tell me: none, #ad in caption, or Instagram's paid-partnership label.")
    elif disclosure == "native_paid_partnership" and publisher != "instagram":
        warnings.append("Needs the native paid-partnership label; posting will stay blocked on this publisher.")
    elif disclosure == "native_paid_partnership":
        warnings.append("Needs Instagram's paid-partnership label: posting stays blocked until that is tested "
                        "(capability instagram.paid_partnership).")

    model = p.get("payment_model")
    if model == "unknown":
        blockers.append("How does it pay? (CPM per 1,000 views, per post, retainer, pool)")
    currency = (p.get("currency") or "").upper()
    if not _known(currency) or len(currency) != 3:
        currency = "USD" if url and "contentrewards.com" in url else ""
        if currency:
            warnings.append("Currency not stated: assuming USD (Content Rewards pays in USD).")
        elif model not in ("unmonetized", "unknown"):
            blockers.append("Which currency does it pay in?")
    rate = _dec(p.get("rate"))
    if model in ("cpm", "per_post", "retainer") and not rate:
        warnings.append("Rate not stated: earnings estimates will be withheld.")
    fee = _dec(p.get("creator_fee_rate"))
    fee_evidence = "stated in campaign material" if fee else None
    if not fee and model == "cpm" and url and "contentrewards.com" in url:
        fee = st.content_rewards_cpm_fee_rate
        fee_evidence = "Content Rewards creator pricing (10% on CPM), verified 2026-11-18"
        warnings.append(f"Fee not stated: using Content Rewards' standard {Decimal(fee) * 100:.0f}% CPM fee.")
    deadline = p.get("submission_deadline_minutes") or 30
    if not p.get("submission_deadline_minutes"):
        warnings.append("Submission deadline not stated: using Content Rewards' 30 minutes.")

    payment = {
        "model": model if model != "unknown" else None, "currency": currency or None, "rate": rate,
        "qualifying_view_definition": p.get("qualifying_view_definition") if _known(p.get("qualifying_view_definition")) else None,
        "min_payable": _dec(p.get("minimum_payable")),
        "min_payable_semantics": p.get("minimum_semantics") if p.get("minimum_semantics") not in (None, "unknown") else None,
        "min_views": int(p["minimum_views"]) if p.get("minimum_views") else None,
        "max_per_clip": _dec(p.get("max_per_clip")), "fee_rate": fee, "fee_exempt_evidence": None,
        "fee_rate_evidence": fee_evidence, "submission_deadline_minutes": deadline,
        "earning_window": p.get("earning_window") if _known(p.get("earning_window")) else None,
        "evidence_source": f"Telegram intake #{intake['id']} on {intake['created_at'][:10]}",
    }
    payment = {k: v for k, v in payment.items() if v not in (None, "")}

    files = ", ".join(Path(f["path"]).name for f in intake["files"]) or "none"
    terms_text = ("\n\n".join(intake["texts"]) + (f"\n\n[Public page text]\n{intake['page_text']}" if intake.get("page_text") else "")
                  + f"\n\n[Attachments kept in data/intake/{intake['id']}: {files}]")
    notes = "; ".join(x for x in (p.get("editorial_notes"), f"Source footage: {p.get('source_material')}")
                      if _known(x) and x != "Source footage: unknown")
    if not _known(p.get("source_material")):
        warnings.append("Where the footage comes from is unclear: only use files the campaign provides.")
    values = {
        "name": (p.get("name") if _known(p.get("name")) else "Campaign")[:80],
        "revenue_route": "campaign", "campaign_url": url, "publisher": publisher, "platform": platform,
        "account_ref": account or None,
        "permission_evidence": (f"Campaign brief and campaign-provided resources at {url}; footage: "
                                f"{p.get('source_material') or 'campaign-provided'}; confirmed by owner via "
                                f"Telegram intake #{intake['id']}"),
        "terms_text": terms_text.strip(),
        "required_tags": [t for t in p.get("required_tags") or [] if t and t.lower() != "unknown"],
        "disclosure_required": disclosure if disclosure in ("none", "caption", "native_paid_partnership") else "none",
        "min_duration_s": float(min_d), "max_duration_s": float(max_d),
        "permitted_audio": p.get("permitted_audio") if _known(p.get("permitted_audio")) else "original_only",
        "prohibited": p.get("prohibited") if _known(p.get("prohibited")) else None,
        "requires_preapproval": 1 if p.get("requires_preapproval") else 0,
        "layout": defaults.get("layout") or {"template": "blur_pad"},
        "editorial_notes": notes or None, "payment": payment,
    }
    if not account:
        blockers.append("No destination account set: put INSTAGRAM_USER_ID in .env (or campaign_defaults.account_ref "
                        "in config.yaml) and restart.")
    if not values["required_tags"] and disclosure == "caption":
        blockers.append("A caption disclosure is required but no exact text was found. Which tags/mentions must I add?")
    return values, warnings, blockers


def budget_minor(intake: dict) -> int | None:
    v = _dec((intake.get("draft") or {}).get("proposed", {}).get("budget_remaining"))
    return ledger.to_minor(v) if v else None


# ------------------------------------------------------------------ message --
REC = {"worth_it": "✅ Worth trying", "risky": "⚠️ Risky", "skip": "⛔ I'd skip it"}


def summary_message(ctx: Ctx, intake: dict) -> str:
    d = intake["draft"]
    a = d["assessment"]
    values, warnings, blockers = draft_to_values(ctx, intake)
    pay = values["payment"]
    lines = [f"📋 {values['name']}", f"{REC.get(a['recommendation'], a['recommendation'])} — {a['summary']}", ""]
    lines += [f"• {r}" for r in a["reasons"][:5]]
    if a["risks"]:
        lines += ["", "Risks:"] + [f"• {r}" for r in a["risks"][:5]]
    lines += ["", "What I'll use:",
              f"• Pays: {pay.get('model', '?')} {pay.get('rate', '?')} {pay.get('currency', '')}"
              f"{' per 1,000 qualified views' if pay.get('model') == 'cpm' else ''}"
              f"{', min ' + pay['min_payable'] if pay.get('min_payable') else ''}"
              f"{', max/clip ' + pay['max_per_clip'] if pay.get('max_per_clip') else ''}"
              f"{', fee ' + pay['fee_rate'] if pay.get('fee_rate') else ''}",
              f"• Length: {values['min_duration_s']:.0f}–{values['max_duration_s']:.0f} s",
              f"• Must include: {', '.join(values['required_tags']) or 'nothing'} · disclosure: {values['disclosure_required']}",
              f"• Pre-approval: {'yes' if values['requires_preapproval'] else 'no'} · audio: {values['permitted_audio']}",
              f"• Post to: {values['platform']} via {values['publisher']} ({values['account_ref'] or 'NOT SET'})",
              f"• Submit within {pay.get('submission_deadline_minutes', 30)} min of posting"]
    if values.get("prohibited"):
        lines.append(f"• Not allowed: {values['prohibited'][:300]}")
    if values.get("editorial_notes"):
        lines.append(f"• Clip focus: {values['editorial_notes'][:300]}")
    b = budget_minor(intake)
    if pay.get("model") == "cpm":
        lo = ledger.estimate_cpm(pay, 5_000, b)
        hi = ledger.estimate_cpm(pay, 20_000, b)
        if lo["high"] is not None and hi["high"] is not None:
            lines.append(f"• Per clip after fee: ~${lo['high']/100:.2f} at 5k qualified views, "
                         f"~${hi['high']/100:.2f} at 20k (if approved and budget lasts)")
    if b is not None:
        lines.append(f"• Budget remaining (as shown): ${b/100:,.2f}")
    if warnings:
        lines += ["", "Assumptions:"] + [f"• {w}" for w in warnings]
    if d.get("unresolved_questions"):
        lines += ["", "Open questions:"] + [f"• {q}" for q in d["unresolved_questions"][:6]]
    if blockers:
        lines += ["", "❗ I need these answered before I can start:"] + [f"• {x}" for x in blockers]
    else:
        lines += ["", "Tap Approve to confirm these settings. Nothing is posted until clips are made and you say Ready."]
    return "\n".join(lines)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "campaign"
    return s if len(s) >= 2 else f"c-{s}"


def approve(ctx: Ctx, intake: dict) -> tuple[dict | None, list[str]]:
    values, _warnings, blockers = draft_to_values(ctx, intake)
    if blockers:
        return None, blockers
    base = _slug(values["name"])
    opp_id, i = base, 2
    while row(ctx.conn, "SELECT 1 FROM opportunities WHERE id=?", (opp_id,)):
        opp_id, i = f"{base}-{i}", i + 1
    opportunities.create(ctx.conn, ctx.now(), opp_id, values, actor="owner:telegram")
    try:
        opportunities.approve_terms(ctx.conn, ctx.now(), opp_id, actor="owner:telegram")
    except ValueError as exc:
        return opportunities.get(ctx.conn, opp_id), [str(exc)]
    opportunities.set_status(ctx.conn, ctx.now(), opp_id, "active", actor="owner:telegram")
    b = budget_minor(intake)
    with tx(ctx.conn):
        if b is not None:
            ctx.conn.execute("UPDATE opportunities SET budget_remaining_minor=?, budget_currency=?, budget_observed_at=? "
                             "WHERE id=?", (b, values["payment"].get("currency", "USD"), iso(ctx.now()), opp_id))
        audit(ctx.conn, "owner:telegram", "intake.approved", {"intake": intake["id"], "opportunity": opp_id})
        intake["opportunity_id"] = opp_id
        kv_set(ctx.conn, f"tg_intake:{intake['id']}", intake)
    return opportunities.get(ctx.conn, opp_id), []


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")
