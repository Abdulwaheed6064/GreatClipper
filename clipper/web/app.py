"""Owner dashboard (server-rendered) + public opaque media route.

Everything except /m/<token>.mp4, /login and /healthz requires the owner
session. Every POST carries a CSRF token.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from clipper import opportunities
from clipper.alerts import alerts_healthy
from clipper.budget import spend
from clipper.context import Ctx
from clipper.db import audit, connect, iso, jload, kv_get, kv_set, parse_iso, row, rows, tx
from clipper.ledger import ledger
from clipper.rules.gates import NATIVE_DISCLOSURE_CAPABILITY, PUBLISHER_CAPABILITIES, capability, is_live
from clipper.web.auth import check_csrf, csrf_token, require_owner, verify_password

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TOKEN_RE = r"^[A-Za-z0-9_-]{20,64}$"

CAPABILITY_KEYS = [
    ("buffer.automatic_publish", "Buffer posts publish automatically (not a notification/reminder) on the live channel"),
    ("buffer.external_link", "Buffer returns the post's externalLink and sentAt after publishing"),
    ("buffer.native_paid_partnership", "Buffer can set the platform's native paid-partnership label (unverified by default)"),
    ("instagram.reels_publish", "Instagram Graph API publishes a Reel to the professional account"),
    ("instagram.permalink", "Instagram returns permalink + timestamp for the published Reel"),
    ("instagram.paid_partnership", "Instagram paid-partnership label + sponsor set via API and visible on the post"),
    ("alerts.telegram", "Telegram alert received on the owner's phone"),
    ("source.feed", "Creator feed yields a downloadable file"),
    ("backup.restore_tested", "A backup was restored on a test copy and verified"),
]


def _fmt_money(minor, currency=""):
    return ledger.fmt_minor(minor, currency)


def _ago(value):
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return "never"
    s = (datetime.now(dt.tzinfo) - dt).total_seconds()
    if s < 0:
        return f"in {(-s) / 60:.0f} min"
    if s < 120:
        return f"{s:.0f}s ago"
    if s < 7200:
        return f"{s / 60:.0f} min ago"
    if s < 172800:
        return f"{s / 3600:.0f} h ago"
    return f"{s / 86400:.0f} d ago"


TEMPLATES.env.filters["money"] = _fmt_money
TEMPLATES.env.filters["ago"] = _ago
TEMPLATES.env.filters["fromjson"] = lambda v: jload(v, {})


def create_app(base_ctx: Ctx) -> FastAPI:
    app = FastAPI(title="GreatClipper", docs_url=None, redoc_url=None, openapi_url=None)
    secret = base_ctx.env.session_secret or secrets.token_urlsafe(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, https_only=base_ctx.env.cookie_secure,
                       same_site="strict", max_age=12 * 3600)

    def get_ctx():
        from clipper.publishing.registry import build_publishers

        conn = connect(base_ctx.env.db_path)
        ctx = Ctx(settings=base_ctx.settings, env=base_ctx.env, conn=conn, alerter=base_ctx.alerter,
                  http=base_ctx.http, editor=base_ctx.editor, transcriber=None, clock=base_ctx.clock,
                  resolve_dns=base_ctx.resolve_dns)
        ctx.publishers = base_ctx.publishers if base_ctx.publishers.get("_static") else build_publishers(ctx)
        try:
            yield ctx
        finally:
            conn.close()

    def render(request: Request, name: str, ctx: Ctx, **kw) -> HTMLResponse:
        flash = request.session.pop("flash", None)
        return TEMPLATES.TemplateResponse(request, name, {
            "csrf": csrf_token(request), "flash": flash, "settings": ctx.settings, "now": ctx.now(), **kw})

    def done(request: Request, url: str, msg: str | None = None) -> RedirectResponse:
        if msg:
            request.session["flash"] = msg
        return RedirectResponse(url, status_code=303)

    owner = [Depends(require_owner)]
    post = [Depends(require_owner), Depends(check_csrf)]

    # ------------------------------------------------------------ public --
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.get("/m/{token}.mp4")
    def public_media(token: str, ctx: Ctx = Depends(get_ctx)):
        import re

        if not re.match(TOKEN_RE, token):
            raise HTTPException(404)
        clip = row(ctx.conn, "SELECT id FROM clips WHERE public_token=?", (token,))
        path = ctx.env.public_dir / f"{token}.mp4"
        if not clip or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600",
                                                                   "X-Robots-Tag": "noindex"})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, ctx: Ctx = Depends(get_ctx)):
        return render(request, "login.html", ctx, configured=bool(ctx.env.owner_password_hash))

    @app.post("/login", dependencies=[Depends(check_csrf)])
    def login(request: Request, password: str = Form(...), ctx: Ctx = Depends(get_ctx)):
        if verify_password(password, ctx.env.owner_password_hash):
            request.session.clear()
            request.session["owner"] = True
            return RedirectResponse("/", status_code=303)
        with tx(ctx.conn):
            audit(ctx.conn, "anonymous", "login.failed", {"ip": request.client.host if request.client else None})
        return done(request, "/login", "Wrong password.")

    @app.post("/logout", dependencies=post)
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --------------------------------------------------------- dashboard --
    @app.get("/", response_class=HTMLResponse, dependencies=owner)
    def dashboard(request: Request, ctx: Ctx = Depends(get_ctx)):
        c = ctx.conn
        st = ctx.settings
        counts = {r["stage"]: r["n"] for r in rows(c, "SELECT stage, COUNT(*) n FROM clips GROUP BY stage")}
        review = row(c, "SELECT COUNT(*) n FROM clips WHERE stage='checked'")["n"]
        subs = rows(c, "SELECT s.*, o.name FROM submissions s JOIN opportunities o ON o.id=s.opportunity_id "
                       "WHERE s.status IN ('manual_required','overdue') ORDER BY s.deadline_at")
        problems = rows(c, "SELECT * FROM publications WHERE status IN ('unknown','failed') "
                           "AND updated_at>=? ORDER BY id DESC LIMIT 10", (iso(ctx.now() - timedelta(days=7)),))
        inflight = rows(c, "SELECT * FROM publications WHERE status IN ('reserved','sending','accepted') ORDER BY id")
        bs = spend.status(c, ctx.now(), st.monthly_operating_ceiling_usd, st.monthly_model_allowance_usd)
        ops = opportunities.list_all(c)
        live_ops = kv_get(c, "live_opportunities", []) or []
        feeds = {o["id"]: kv_get(c, f"feed_ok:{o['id']}") for o in ops if o.get("feed_url")}
        dead_jobs = rows(c, "SELECT * FROM jobs WHERE status IN ('failed','dead') AND updated_at>=? ORDER BY id DESC "
                            "LIMIT 10", (iso(ctx.now() - timedelta(days=3)),))
        held_sources = rows(c, "SELECT id, title, hold_reason FROM sources WHERE stage='held' ORDER BY id DESC LIMIT 10")
        caps = {k: capability(c, k) for k, _ in CAPABILITY_KEYS}
        return render(request, "dashboard.html", ctx, counts=counts, review=review, subs=subs, problems=problems,
                      inflight=inflight, budget=bs, ops=ops, live_ops=live_ops, paused=kv_get(c, "paused", False),
                      alerts=alerts_healthy(c), feeds=feeds, dead_jobs=dead_jobs, held_sources=held_sources,
                      caps=caps, restored=kv_get(c, "restored_from"),
                      recent_alerts=rows(c, "SELECT * FROM alerts ORDER BY id DESC LIMIT 8"))

    @app.post("/pause", dependencies=post)
    def toggle_pause(request: Request, paused: str = Form(...), ctx: Ctx = Depends(get_ctx)):
        with tx(ctx.conn):
            kv_set(ctx.conn, "paused", paused == "1")
            audit(ctx.conn, "owner", "system.pause", {"paused": paused == "1"})
        return done(request, "/", "System paused." if paused == "1" else "System resumed.")

    @app.post("/alerts/test", dependencies=post)
    def test_alert(request: Request, ctx: Ctx = Depends(get_ctx)):
        ok = ctx.alerter.send(ctx.conn, ctx.now(), "test", "Test alert from the dashboard. If you can read this, "
                                                          "deadline alerts will reach you.")
        if ok:
            _set_cap(ctx, "alerts.telegram", "verified", "test alert delivered")
        return done(request, "/", "Test alert delivered." if ok else "Test alert FAILED — see the alerts list.")

    # ------------------------------------------------------ opportunities --
    @app.get("/opportunities", response_class=HTMLResponse, dependencies=owner)
    def opp_list(request: Request, ctx: Ctx = Depends(get_ctx)):
        return render(request, "opportunities.html", ctx, ops=opportunities.list_all(ctx.conn),
                      live_ops=kv_get(ctx.conn, "live_opportunities", []) or [])

    @app.post("/opportunities", dependencies=post)
    async def opp_create(request: Request, ctx: Ctx = Depends(get_ctx)):
        form = await request.form()
        try:
            op = opportunities.create(ctx.conn, ctx.now(), str(form.get("id", "")).strip(), _opp_values(form))
        except Exception as exc:  # noqa: BLE001 - shown to the owner
            return done(request, "/opportunities", f"Not saved: {exc}")
        return done(request, f"/opportunities/{op['id']}", "Saved as draft. Review, then confirm the terms.")

    @app.get("/opportunities/{opp_id}", response_class=HTMLResponse, dependencies=owner)
    def opp_detail(request: Request, opp_id: str, ctx: Ctx = Depends(get_ctx)):
        op = opportunities.get(ctx.conn, opp_id)
        if not op:
            raise HTTPException(404)
        sources = rows(ctx.conn, "SELECT * FROM sources WHERE opportunity_id=? ORDER BY id DESC LIMIT 50", (opp_id,))
        history = rows(ctx.conn, "SELECT terms_version, created_at FROM opportunity_terms_history WHERE opportunity_id=? "
                                 "ORDER BY terms_version DESC", (opp_id,))
        draft = kv_get(ctx.conn, f"terms_draft:{opp_id}")
        return render(request, "opportunity.html", ctx, op=op, sources=sources, history=history,
                      problems=opportunities.terms_problems(op), draft=draft,
                      live=is_live(ctx.conn, ctx.settings.mode, opp_id),
                      live_listed=opp_id in (kv_get(ctx.conn, "live_opportunities", []) or []))

    @app.post("/opportunities/{opp_id}", dependencies=post)
    async def opp_update(request: Request, opp_id: str, ctx: Ctx = Depends(get_ctx)):
        form = await request.form()
        try:
            before = opportunities.get(ctx.conn, opp_id)
            after = opportunities.update(ctx.conn, ctx.now(), opp_id, _opp_values(form))
        except Exception as exc:  # noqa: BLE001
            return done(request, f"/opportunities/{opp_id}", f"Not saved: {exc}")
        msg = "Saved."
        if after["terms_version"] != before["terms_version"]:
            msg = f"Terms changed → version {after['terms_version']}. Re-confirm them; unposted clips are held for revalidation."
        return done(request, f"/opportunities/{opp_id}", msg)

    @app.post("/opportunities/{opp_id}/approve", dependencies=post)
    def opp_approve(request: Request, opp_id: str, ctx: Ctx = Depends(get_ctx)):
        try:
            opportunities.approve_terms(ctx.conn, ctx.now(), opp_id)
        except ValueError as exc:
            return done(request, f"/opportunities/{opp_id}", str(exc))
        from clipper.jobs.handlers import try_release

        for c in rows(ctx.conn, "SELECT id FROM clips WHERE opportunity_id=? AND stage='checked'", (opp_id,)):
            try_release(ctx, c["id"])
        return done(request, f"/opportunities/{opp_id}", "Terms confirmed for this version.")

    @app.post("/opportunities/{opp_id}/status", dependencies=post)
    def opp_status(request: Request, opp_id: str, status: str = Form(...), ctx: Ctx = Depends(get_ctx)):
        if status not in ("draft", "active", "paused", "archived"):
            raise HTTPException(400)
        opportunities.set_status(ctx.conn, ctx.now(), opp_id, status)
        return done(request, f"/opportunities/{opp_id}", f"Status set to {status}.")

    @app.post("/opportunities/{opp_id}/live", dependencies=post)
    def opp_live(request: Request, opp_id: str, enable: str = Form(...), confirm: str = Form(""),
                 ctx: Ctx = Depends(get_ctx)):
        with tx(ctx.conn):
            live = set(kv_get(ctx.conn, "live_opportunities", []) or [])
            if enable == "1":
                if confirm != opp_id:
                    raise HTTPException(400, "type the opportunity id to confirm live publishing")
                live.add(opp_id)
            else:
                live.discard(opp_id)
            kv_set(ctx.conn, "live_opportunities", sorted(live))
            audit(ctx.conn, "owner", "opportunity.live", {"id": opp_id, "enabled": enable == "1"})
        note = "" if ctx.settings.mode == "live" else " (config.yaml mode is still dry_run, so nothing is posted yet)"
        return done(request, f"/opportunities/{opp_id}", ("Live publishing enabled" if enable == "1"
                                                          else "Live publishing disabled") + note)

    @app.post("/opportunities/{opp_id}/budget", dependencies=post)
    def opp_budget(request: Request, opp_id: str, amount: str = Form(...), currency: str = Form(...),
                   ctx: Ctx = Depends(get_ctx)):
        _record_budget(ctx, opp_id, amount, currency)
        return done(request, f"/opportunities/{opp_id}", "Budget observation recorded.")

    @app.post("/opportunities/{opp_id}/draft-terms", dependencies=post)
    def opp_draft_terms(request: Request, opp_id: str, ctx: Ctx = Depends(get_ctx)):
        op = opportunities.get(ctx.conn, opp_id)
        text = (op.get("terms_text") or "").strip()
        if not text:
            return done(request, f"/opportunities/{opp_id}", "Paste the terms text first.")
        from clipper.editorial.claude import worst_case_cost_minor

        st = ctx.settings
        try:
            rid = spend.reserve(ctx.conn, ctx.now(), category="claude", amount_minor=worst_case_cost_minor(st, text),
                                ceiling_usd=st.monthly_operating_ceiling_usd,
                                model_allowance_usd=st.monthly_model_allowance_usd, optional=True, ref=f"terms:{opp_id}")
        except spend.BudgetExceeded as exc:
            return done(request, f"/opportunities/{opp_id}", str(exc))
        try:
            data, cost = ctx.editor.draft_terms(text)
        except Exception as exc:  # noqa: BLE001
            spend.release(ctx.conn, ctx.now(), rid)
            return done(request, f"/opportunities/{opp_id}", f"Draft failed: {exc}")
        spend.commit(ctx.conn, ctx.now(), rid, cost)
        with tx(ctx.conn):
            kv_set(ctx.conn, f"terms_draft:{opp_id}", data)
        return done(request, f"/opportunities/{opp_id}", "Draft below. It is a proposal: copy values you have "
                                                          "verified into the form.")

    @app.post("/opportunities/{opp_id}/sources", dependencies=post)
    def opp_add_source(request: Request, opp_id: str, provider_id: str = Form(...), url: str = Form(...),
                       title: str = Form(""), rights_ref: str = Form(...), ctx: Ctx = Depends(get_ctx)):
        from clipper.sources.feed import add_manual

        try:
            sid = add_manual(ctx, opp_id, provider_id.strip(), url.strip(), title.strip(), rights_ref.strip())
        except Exception as exc:  # noqa: BLE001
            return done(request, f"/opportunities/{opp_id}", f"Not added: {exc}")
        return done(request, f"/opportunities/{opp_id}",
                    f"Source #{sid} queued (semi-manual ingestion)." if sid else "Already known; not added twice.")

    @app.post("/sources/{sid}/retry", dependencies=post)
    def source_retry(request: Request, sid: int, ctx: Ctx = Depends(get_ctx)):
        from clipper.jobs.queue import enqueue

        src = row(ctx.conn, "SELECT * FROM sources WHERE id=?", (sid,))
        with tx(ctx.conn):
            if not src["file_path"]:
                ctx.conn.execute("UPDATE sources SET stage='discovered', hold_reason=NULL WHERE id=?", (sid,))
                job = "acquire_source"
            elif not src["transcript_path"] or src["language"] is None:
                ctx.conn.execute("UPDATE sources SET stage='acquired', hold_reason=NULL WHERE id=?", (sid,))
                job = "transcribe_source"
            else:
                ctx.conn.execute("UPDATE sources SET stage='transcribed', hold_reason=NULL WHERE id=?", (sid,))
                job = "select_clips"
            audit(ctx.conn, "owner", "source.retry", {"id": sid, "job": job})
        enqueue(ctx.conn, queue="heavy", type=job, payload={"source_id": sid}, due_at=ctx.now(),
                dedupe_key=f"{job}:{sid}:retry{ctx.now().timestamp():.0f}")
        return done(request, f"/opportunities/{src['opportunity_id']}", f"Source #{sid} re-queued ({job}).")

    @app.post("/sources/{sid}/reject", dependencies=post)
    def source_reject(request: Request, sid: int, ctx: Ctx = Depends(get_ctx)):
        src = row(ctx.conn, "SELECT opportunity_id FROM sources WHERE id=?", (sid,))
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE sources SET stage='rejected' WHERE id=?", (sid,))
            audit(ctx.conn, "owner", "source.reject", {"id": sid})
        return done(request, f"/opportunities/{src['opportunity_id']}", f"Source #{sid} rejected.")

    # --------------------------------------------------------------- clips --
    @app.get("/clips", response_class=HTMLResponse, dependencies=owner)
    def clip_list(request: Request, stage: str = "checked", ctx: Ctx = Depends(get_ctx)):
        clips = rows(ctx.conn, "SELECT c.*, o.name opp_name, o.required_tags, o.caption_template FROM clips c "
                               "JOIN opportunities o ON o.id=c.opportunity_id WHERE c.stage=? ORDER BY c.score_total DESC, "
                               "c.id DESC LIMIT 100", (stage,))
        from clipper.publishing.service import compose_caption

        for c in clips:
            op = opportunities.get(ctx.conn, c["opportunity_id"])
            c["final_caption"] = compose_caption(op, c)
        return render(request, "clips.html", ctx, clips=clips, stage=stage)

    @app.get("/clips/{cid}/video", dependencies=owner)
    def clip_video(cid: int, ctx: Ctx = Depends(get_ctx)):
        c = row(ctx.conn, "SELECT render_path FROM clips WHERE id=?", (cid,))
        if not c or not c["render_path"] or not Path(c["render_path"]).exists():
            raise HTTPException(404)
        return FileResponse(c["render_path"], media_type="video/mp4")

    @app.post("/clips/{cid}/review", dependencies=post)
    def clip_review(request: Request, cid: int, decision: str = Form(...), note: str = Form(""),
                    caption: str = Form(None), ctx: Ctx = Depends(get_ctx)):
        from clipper.jobs.handlers import try_release

        clip = row(ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
        if clip["stage"] == "published":
            return done(request, "/clips?stage=published", "Published clips are immutable here.")
        with tx(ctx.conn):
            if caption is not None and caption.strip() and caption.strip() != (clip["caption"] or ""):
                ctx.conn.execute("UPDATE clips SET caption=? WHERE id=?", (caption.strip()[:1500], cid))
            if decision == "approve":
                ctx.conn.execute("UPDATE clips SET review_state='approved', context_risk=0, updated_at=? WHERE id=?",
                                 (iso(ctx.now()), cid))
            elif decision == "reject":
                ctx.conn.execute("UPDATE clips SET review_state='rejected', stage='rejected', hold_reason=?, "
                                 "updated_at=? WHERE id=?", (note[:500] or "rejected by owner", iso(ctx.now()), cid))
            elif decision == "recheck" and clip["stage"] == "held" and clip["render_hash"]:
                ctx.conn.execute("UPDATE clips SET stage='checked', hold_reason=NULL WHERE id=?", (cid,))
            audit(ctx.conn, "owner", f"clip.{decision}", {"id": cid, "note": note})
        msg = f"Clip {cid}: {decision}."
        if decision in ("approve", "recheck"):
            reasons = try_release(ctx, cid)
            msg += " Ready to publish." if not reasons else " Still held: " + "; ".join(reasons)
        return done(request, f"/clips?stage={clip['stage'] if decision != 'reject' else 'checked'}", msg)

    @app.post("/clips/{cid}/preapproval", dependencies=post)
    def clip_preapproval(request: Request, cid: int, state: str = Form(...), evidence: str = Form(""),
                         ctx: Ctx = Depends(get_ctx)):
        from clipper.jobs.handlers import try_release

        if state not in ("approved", "rejected") or len(evidence.strip()) < 4:
            return done(request, "/clips", "Record the campaign manager's decision with evidence.")
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE clips SET preapproval_state=?, preapproval_evidence=? WHERE id=?",
                             (state, evidence[:1000], cid))
            audit(ctx.conn, "owner", "clip.preapproval", {"id": cid, "state": state})
        reasons = try_release(ctx, cid)
        return done(request, "/clips", f"Pre-approval recorded. {'Ready.' if not reasons else 'Held: ' + '; '.join(reasons)}")

    # --------------------------------------------- attended campaign session --
    @app.get("/session", response_class=HTMLResponse, dependencies=owner)
    def session_page(request: Request, ctx: Ctx = Depends(get_ctx)):
        camps = [o for o in opportunities.list_all(ctx.conn) if o["revenue_route"] == "campaign" and o["status"] == "active"]
        ready = {o["id"]: rows(ctx.conn, "SELECT * FROM clips WHERE opportunity_id=? AND stage='ready' "
                                         "ORDER BY score_total DESC", (o["id"],)) for o in camps}
        open_subs = rows(ctx.conn, "SELECT * FROM submissions WHERE status IN ('manual_required','overdue')")
        return render(request, "session.html", ctx, camps=camps, ready=ready, open_subs=open_subs,
                      alerts=alerts_healthy(ctx.conn))

    @app.post("/session/publish", dependencies=post)
    def session_publish(request: Request, opportunity_id: str = Form(...), clip_id: int = Form(...),
                        budget_amount: str = Form(...), budget_currency: str = Form(...),
                        chk_terms: str = Form(None), chk_admission: str = Form(None),
                        chk_disclosure: str = Form(None), chk_available: str = Form(None),
                        ctx: Ctx = Depends(get_ctx)):
        from clipper.publishing.service import publish_clip

        if not all((chk_terms, chk_admission, chk_disclosure, chk_available)):
            return done(request, "/session", "Tick every confirmation; each is a real check in the campaign page.")
        _record_budget(ctx, opportunity_id, budget_amount, budget_currency)
        now = ctx.now()
        with tx(ctx.conn):
            cur = ctx.conn.execute(
                "INSERT INTO operator_ready(opportunity_id, clip_id, checklist, acknowledged_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (opportunity_id, clip_id, json.dumps({"terms": True, "admission": True, "disclosure": True,
                                                      "available": True, "budget": budget_amount}),
                 iso(now), iso(now + timedelta(minutes=ctx.settings.operator_ready_expires_minutes))))
            rid = cur.lastrowid
        pub_id, reasons = publish_clip(ctx, clip_id, readiness_id=rid)
        if reasons:
            return done(request, "/session", "Not published: " + "; ".join(reasons))
        return done(request, "/submissions", f"Publication #{pub_id} sent to the publisher. Stay here: the live URL "
                                             "and deadline appear as soon as the platform confirms.")

    # --------------------------------------------------------- submissions --
    @app.get("/submissions", response_class=HTMLResponse, dependencies=owner)
    def submissions_page(request: Request, ctx: Ctx = Depends(get_ctx)):
        subs = rows(ctx.conn, "SELECT s.*, o.name, p.dry_run FROM submissions s JOIN opportunities o ON "
                              "o.id=s.opportunity_id JOIN publications p ON p.id=s.publication_id "
                              "ORDER BY s.id DESC LIMIT 100")
        inflight = rows(ctx.conn, "SELECT p.*, o.name FROM publications p JOIN opportunities o ON o.id=p.opportunity_id "
                                  "WHERE o.revenue_route='campaign' AND p.status IN ('reserved','sending','accepted','unknown')")
        return render(request, "submissions.html", ctx, subs=subs, inflight=inflight)

    @app.post("/submissions/{sid}/submitted", dependencies=post)
    def sub_done(request: Request, sid: int, receipt: str = Form(...), ctx: Ctx = Depends(get_ctx)):
        from clipper.submissions.manual import record_submitted

        try:
            s = record_submitted(ctx, sid, receipt)
        except ValueError as exc:
            return done(request, "/submissions", str(exc))
        return done(request, "/submissions", f"Recorded as {s['status']}.")

    @app.post("/submissions/{sid}/missed", dependencies=post)
    def sub_missed(request: Request, sid: int, note: str = Form(...), ctx: Ctx = Depends(get_ctx)):
        from clipper.submissions.manual import record_missed

        try:
            record_missed(ctx, sid, note)
        except ValueError as exc:
            return done(request, "/submissions", str(exc))
        return done(request, "/submissions", "Recorded as missed.")

    @app.post("/submissions/{sid}/review", dependencies=post)
    def sub_review(request: Request, sid: int, state: str = Form(...), note: str = Form(""),
                   ctx: Ctx = Depends(get_ctx)):
        from clipper.submissions.manual import record_review

        record_review(ctx, sid, state, note)
        return done(request, "/submissions", "Review state recorded.")

    # -------------------------------------------------------- publications --
    @app.get("/publications", response_class=HTMLResponse, dependencies=owner)
    def pubs_page(request: Request, ctx: Ctx = Depends(get_ctx)):
        pubs = rows(ctx.conn, "SELECT p.*, o.name FROM publications p JOIN opportunities o ON o.id=p.opportunity_id "
                              "ORDER BY p.id DESC LIMIT 100")
        slots = rows(ctx.conn, "SELECT * FROM publish_slots ORDER BY created_at DESC LIMIT 30")
        usage = {}
        for o in opportunities.list_all(ctx.conn):
            if o["publisher"] == "buffer" and o.get("account_ref"):
                b = ctx.publishers.get("buffer")
                usage[o["account_ref"]] = (b.used(o["account_ref"]), ctx.settings.buffer_requests_per_30d)
        return render(request, "publications.html", ctx, pubs=pubs, slots=slots, usage=usage)

    @app.post("/publications/{pid}/resolve", dependencies=post)
    def pub_resolve(request: Request, pid: int, published: str = Form(...), live_url: str = Form(""),
                    sent_at: str = Form(""), ctx: Ctx = Depends(get_ctx)):
        from clipper.publishing.service import resolve_unknown

        try:
            resolve_unknown(ctx, pid, published=published == "1", live_url=live_url.strip() or None,
                            sent_at=parse_iso(sent_at.strip()) if sent_at.strip() else None)
        except ValueError as exc:
            return done(request, "/publications", str(exc))
        return done(request, "/publications", "Resolved.")

    @app.post("/publications/{pid}/recheck", dependencies=post)
    def pub_recheck(request: Request, pid: int, ctx: Ctx = Depends(get_ctx)):
        from clipper.jobs.queue import enqueue

        pub = row(ctx.conn, "SELECT status FROM publications WHERE id=?", (pid,))
        jt = "reconcile_publication" if pub["status"] == "unknown" else "publication_status"
        enqueue(ctx.conn, queue="light", type=jt, payload={"publication_id": pid, "n": 0}, due_at=ctx.now(),
                dedupe_key=f"manual:{jt}:{pid}:{ctx.now().timestamp():.0f}", max_attempts=1)
        return done(request, "/publications", "Check queued (uses publisher API requests).")

    # -------------------------------------------------------------- ledger --
    @app.get("/ledger", response_class=HTMLResponse, dependencies=owner)
    def ledger_page(request: Request, period: str = "", ctx: Ctx = Depends(get_ctx)):
        prof = ledger.profitability(ctx.conn, ctx.settings.operator_hourly_rate_usd, period or None)
        entries = rows(ctx.conn, "SELECT * FROM ledger ORDER BY effective_date DESC, id DESC LIMIT 100")
        metrics = rows(ctx.conn, "SELECT * FROM metrics ORDER BY as_of DESC LIMIT 50")
        st = ctx.settings
        bs = spend.status(ctx.conn, ctx.now(), st.monthly_operating_ceiling_usd, st.monthly_model_allowance_usd)
        return render(request, "ledger.html", ctx, prof=prof, entries=entries, metrics=metrics, budget=bs,
                      ops=opportunities.list_all(ctx.conn), event_types=ledger.EVENT_TYPES, period=period,
                      import_result=request.session.pop("import_result", None))

    @app.post("/ledger/entry", dependencies=post)
    async def ledger_entry(request: Request, ctx: Ctx = Depends(get_ctx)):
        f = await request.form()
        try:
            ledger.add_entry(ctx.conn, ctx.now(), currency=str(f["currency"]), amount=str(f["amount"]),
                             event_type=str(f["event_type"]), source_evidence=str(f.get("source_evidence", "")),
                             effective_date=str(f["effective_date"]), opportunity_id=str(f.get("opportunity_id") or "") or None,
                             external_id=str(f.get("external_id") or "").strip() or None, note=str(f.get("note") or ""))
        except (ValueError, KeyError) as exc:
            return done(request, "/ledger", f"Not recorded: {exc}")
        return done(request, "/ledger", "Recorded.")

    @app.post("/ledger/import", dependencies=post)
    async def ledger_import(request: Request, kind: str = Form(...), file: UploadFile = None,
                            ctx: Ctx = Depends(get_ctx)):
        text = (await file.read()).decode("utf-8-sig") if file else ""
        try:
            fn = ledger.import_ledger_csv if kind == "ledger" else ledger.import_metrics_csv
            res = fn(ctx.conn, ctx.now(), text)
        except ValueError as exc:
            return done(request, "/ledger", f"Import failed: {exc}")
        request.session["import_result"] = {**res, "errors": res["errors"][:15]}
        return done(request, "/ledger", f"Imported {res['inserted']}, duplicates skipped {res['duplicates']}, "
                                        f"errors {len(res['errors'])}.")

    @app.get("/ledger/export.csv", dependencies=owner)
    def ledger_export(ctx: Ctx = Depends(get_ctx)):
        return Response(ledger.export_ledger_csv(ctx.conn), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=ledger.csv"})

    @app.post("/ledger/time", dependencies=post)
    def ledger_time(request: Request, minutes: int = Form(...), activity: str = Form(...), work_date: str = Form(...),
                    opportunity_id: str = Form(""), ctx: Ctx = Depends(get_ctx)):
        try:
            ledger.log_time(ctx.conn, ctx.now(), minutes, activity, work_date, opportunity_id or None)
        except ValueError as exc:
            return done(request, "/ledger", str(exc))
        return done(request, "/ledger", "Time logged.")

    # -------------------------------------------------------- capabilities --
    @app.get("/capabilities", response_class=HTMLResponse, dependencies=owner)
    def caps_page(request: Request, ctx: Ctx = Depends(get_ctx)):
        recs = {r["key"]: r for r in rows(ctx.conn, "SELECT * FROM capabilities")}
        return render(request, "capabilities.html", ctx, keys=CAPABILITY_KEYS, recs=recs,
                      schema=request.session.pop("schema_check", None))

    @app.post("/capabilities", dependencies=post)
    def caps_set(request: Request, key: str = Form(...), status: str = Form(...), evidence: str = Form(""),
                 ctx: Ctx = Depends(get_ctx)):
        if key not in dict(CAPABILITY_KEYS) or status not in ("unverified", "verified", "failed", "not_supported"):
            raise HTTPException(400)
        if status == "verified" and len(evidence.strip()) < 8:
            return done(request, "/capabilities", "Verified needs evidence (e.g. the live post URL and what you saw).")
        _set_cap(ctx, key, status, evidence)
        return done(request, "/capabilities", f"{key}: {status}.")

    @app.post("/capabilities/buffer-schema", dependencies=post)
    def caps_buffer_schema(request: Request, ctx: Ctx = Depends(get_ctx)):
        try:
            missing = ctx.publishers["buffer"].introspect()
            request.session["schema_check"] = {"ok": not missing, "missing": missing}
        except Exception as exc:  # noqa: BLE001
            request.session["schema_check"] = {"ok": False, "error": str(exc)}
        return done(request, "/capabilities")

    # ---------------------------------------------------------------- jobs --
    @app.get("/jobs", response_class=HTMLResponse, dependencies=owner)
    def jobs_page(request: Request, ctx: Ctx = Depends(get_ctx)):
        jobs = rows(ctx.conn, "SELECT * FROM jobs ORDER BY id DESC LIMIT 150")
        audit_rows = rows(ctx.conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 50")
        return render(request, "jobs.html", ctx, jobs=jobs, audit=audit_rows)

    @app.post("/jobs/{jid}/retry", dependencies=post)
    def job_retry(request: Request, jid: int, ctx: Ctx = Depends(get_ctx)):
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE jobs SET status='pending', attempts=0, due_at=?, updated_at=? WHERE id=? "
                             "AND status IN ('failed','dead') AND type NOT IN ('publication_status')",
                             (iso(ctx.now()), iso(ctx.now()), jid))
            audit(ctx.conn, "owner", "job.retry", {"id": jid})
        return done(request, "/jobs", f"Job {jid} re-queued.")

    return app


# ------------------------------------------------------------------ helpers --
def _set_cap(ctx: Ctx, key: str, status: str, evidence: str) -> None:
    with tx(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO capabilities(key, status, evidence, verified_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status=excluded.status, evidence=excluded.evidence, "
            "verified_at=excluded.verified_at, updated_at=excluded.updated_at",
            (key, status, evidence[:2000], iso(ctx.now()) if status == "verified" else None, iso(ctx.now())))
        audit(ctx.conn, "owner", "capability.set", {"key": key, "status": status})


def _record_budget(ctx: Ctx, opp_id: str, amount: str, currency: str) -> None:
    minor = ledger.to_minor(amount)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE opportunities SET budget_remaining_minor=?, budget_currency=?, budget_observed_at=? "
                         "WHERE id=?", (minor, currency.upper()[:3], iso(ctx.now()), opp_id))
        audit(ctx.conn, "owner", "opportunity.budget_observed", {"id": opp_id, "minor": minor})


def _lines(v) -> list[str]:
    return [x.strip() for x in str(v or "").replace("\n", ",").split(",") if x.strip()]


def _num(v, default=None):
    v = str(v or "").strip()
    if not v:
        return default
    return float(v)


def _opp_values(f) -> dict:
    g = lambda k: (str(f.get(k)).strip() if f.get(k) is not None else None)  # noqa: E731
    layout_raw = g("layout") or '{"template":"blur_pad"}'
    try:
        layout = json.loads(layout_raw)
    except ValueError as exc:
        raise ValueError(f"layout must be JSON: {exc}")
    payment = {k: g(f"pay_{k}") or None for k in (
        "model", "currency", "rate", "qualifying_view_definition", "min_payable", "min_payable_semantics",
        "min_views", "max_per_clip", "max_per_account", "max_per_campaign", "fee_rate", "fee_exempt_evidence",
        "payout_schedule", "submission_deadline_minutes", "earning_window", "evidence_source")}
    payment["fee_exempt"] = bool(f.get("pay_fee_exempt"))
    if payment["currency"]:
        payment["currency"] = payment["currency"].upper()
    payment = {k: v for k, v in payment.items() if v not in (None, "")}
    v = {
        "name": g("name"), "revenue_route": g("revenue_route"), "source_url": g("source_url"),
        "campaign_url": g("campaign_url"), "owner_contact": g("owner_contact"),
        "permission_evidence": g("permission_evidence"), "terms_text": g("terms_text"),
        "expires_at": g("expires_at") or None, "allowed_from": g("allowed_from") or None,
        "allowed_until": g("allowed_until") or None, "platform": g("platform"), "publisher": g("publisher"),
        "account_ref": g("account_ref"), "feed_url": g("feed_url") or None,
        "required_tags": _lines(f.get("required_tags")), "disclosure_required": g("disclosure_required"),
        "sponsor_ids": _lines(f.get("sponsor_ids")), "permitted_audio": g("permitted_audio"),
        "min_duration_s": _num(f.get("min_duration_s"), 15), "max_duration_s": _num(f.get("max_duration_s"), 60),
        "prohibited": g("prohibited"), "reuse_constraints": g("reuse_constraints"),
        "requires_preapproval": 1 if f.get("requires_preapproval") else 0, "layout": layout,
        "caption_template": g("caption_template") or "{caption}\n\n{tags}",
        "posting_times": _lines(f.get("posting_times")), "editorial_notes": g("editorial_notes"), "payment": payment,
    }
    return {k: val for k, val in v.items() if val is not None}
