"""Accounting, budget, backup/restore, web auth/CSRF (section 14)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from clipper.budget import spend
from clipper.db import connect, kv_get, row
from clipper.ledger import ledger
from tests.conftest import make_opportunity

CSV = """external_id,effective_date,currency,amount,event_type,opportunity_id,clip_id,source_evidence,note
cr-1,2026-11-20,USD,12.50,settled,camp,,CR wallet export 2026-11-21,
cr-1-fee,2026-11-20,USD,1.25,fee,camp,,CR wallet export 2026-11-21,10% CPM fee
wd-1,2026-11-22,USD,11.25,withdrawal,,,Whop payout 998,moves money only
cad-1,2026-11-20,CAD,40.00,settled,client-a,,Invoice 7 paid,
"""


def test_import_is_idempotent_and_currencies_stay_separate(ctx):
    make_opportunity(ctx)
    r1 = ledger.import_ledger_csv(ctx.conn, ctx.now(), CSV)
    r2 = ledger.import_ledger_csv(ctx.conn, ctx.now(), CSV)
    assert r1 == {"inserted": 4, "duplicates": 0, "errors": []}
    assert r2["inserted"] == 0 and r2["duplicates"] == 4
    prof = {(r["opportunity_id"], r["currency"]): r for r in ledger.profitability(ctx.conn, 15)["rows"]}
    camp = prof[("camp", "USD")]
    assert camp["settled"] == 1250 and camp["fees"] == 125 and camp["cash_contribution"] == 1125
    assert prof[("(shared)", "USD")]["settled"] == 0           # withdrawal is not revenue
    assert prof[("(shared)", "USD")]["withdrawn"] == 1125
    assert prof[("client-a", "CAD")]["settled"] == 4000        # never added to USD
    assert prof[("client-a", "CAD")]["contribution_after_labor"] is None


def test_unknown_stays_unknown_and_evidence_required(ctx):
    with pytest.raises(ValueError):
        ledger.add_entry(ctx.conn, ctx.now(), currency="USD", amount="5", event_type="settled", source_evidence="",
                         effective_date="2026-11-20")
    text = "external_key,live_url,platform_views,qualified_views,as_of,source\nk1,https://x/1,5000,,2026-11-21,IG insights\n"
    ledger.import_metrics_csv(ctx.conn, ctx.now(), text)
    m = row(ctx.conn, "SELECT * FROM metrics")
    assert m["platform_views"] == 5000 and m["qualified_views"] is None


def test_cpm_estimator_semantics():
    pay = {"model": "cpm", "rate": "2", "fee_rate": "0.10", "min_payable": "10",
           "min_payable_semantics": "per_clip_earnings_minimum", "max_per_clip": "750"}
    assert ledger.estimate_cpm(pay, 4000, 1_000_000)["high"] == 0           # $8 < $10 minimum
    est = ledger.estimate_cpm(pay, 10_000, 1_000_000)
    assert est["low"] == est["high"] == 1800                                 # $20 - 10%
    unknown_min = dict(pay, min_payable_semantics=None)
    assert ledger.estimate_cpm(unknown_min, 10_000, 1_000_000)["low"] == 0   # range when semantics unknown
    assert ledger.estimate_cpm(dict(pay, fee_rate=None), 10_000, 1_000_000)["high"] is None
    assert ledger.estimate_cpm(pay, None, 1_000_000)["high"] is None         # unknown qualified views
    assert ledger.estimate_cpm(pay, 10_000, 500)["high"] == 450              # budget nearly exhausted


def test_budget_reservations_stop_paid_work(ctx):
    st = ctx.settings
    ledger.add_entry(ctx.conn, ctx.now(), currency="USD", amount="35.00", event_type="fixed_commitment",
                     source_evidence="VPS invoice", effective_date="2026-11-01")
    rid = spend.reserve(ctx.conn, ctx.now(), category="claude", amount_minor=300, ceiling_usd=40,
                        model_allowance_usd=10)
    with pytest.raises(spend.BudgetExceeded):
        spend.reserve(ctx.conn, ctx.now(), category="claude", amount_minor=300, ceiling_usd=40,
                      model_allowance_usd=10)
    with pytest.raises(spend.BudgetExceeded):          # >= 90% used: optional work stops
        spend.reserve(ctx.conn, ctx.now(), category="claude", amount_minor=1, ceiling_usd=40,
                      model_allowance_usd=10, optional=True)
    spend.commit(ctx.conn, ctx.now(), rid, 40)
    bs = spend.status(ctx.conn, ctx.now(), 40, 10)
    assert bs.fixed_minor == 3500 and bs.variable_minor == 40 and spend.threshold_alert_level(bs) == "80"
    del st


def test_backup_and_restore(ctx, tmp_path):
    from clipper.backup import make_backup, new_key, restore

    make_opportunity(ctx)
    ctx.env = ctx.env.model_copy(update={"backup_key": new_key()})
    f = make_backup(ctx)
    assert f and f.exists() and b"client-a" not in f.read_bytes()       # encrypted
    target = tmp_path / "restored" / "clipper.db"
    target.parent.mkdir()
    restore(f, ctx.env.backup_key, target)
    conn = connect(target)
    assert row(conn, "SELECT id FROM opportunities")["id"] == "client-a"
    assert kv_get(conn, "paused") is True                                 # review before resuming


def test_no_backup_without_key(ctx):
    from clipper.backup import make_backup

    assert make_backup(ctx) is None
    assert any("BACKUP_KEY" in m for m in ctx.alerts.sent)


# -------------------------------------------------------------------- web --
@pytest.fixture
def client(ctx):
    from fastapi.testclient import TestClient

    from clipper.web.app import create_app
    from clipper.web.auth import hash_password

    ctx.env = ctx.env.model_copy(update={"owner_password_hash": hash_password("correct horse battery")})
    ctx.publishers["_static"] = True
    return TestClient(create_app(ctx))


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_web_requires_login_and_csrf(client, ctx):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = client.get("/login").text
    assert client.post("/login", data={"password": "correct horse battery"}).status_code == 403   # no CSRF
    r = client.post("/login", data={"password": "wrong", "csrf": _csrf(page)}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    r = client.post("/login", data={"password": "correct horse battery", "csrf": _csrf(page)}, follow_redirects=False)
    assert r.headers["location"] == "/"
    for path in ("/", "/opportunities", "/clips", "/session", "/submissions", "/publications", "/ledger",
                 "/capabilities", "/jobs"):
        assert client.get(path).status_code == 200, path
    dash = client.get("/").text
    assert client.post("/pause", data={"paused": "1"}).status_code == 403
    assert client.post("/pause", data={"paused": "1", "csrf": _csrf(dash)}).status_code == 200
    assert kv_get(ctx.conn, "paused") is True


def test_web_create_and_confirm_opportunity(client, ctx):
    page = client.get("/login").text
    client.post("/login", data={"password": "correct horse battery", "csrf": _csrf(page)})
    tok = _csrf(client.get("/opportunities").text)
    r = client.post("/opportunities", data={
        "csrf": tok, "id": "smj-reach", "name": "Social Media Justice", "revenue_route": "campaign",
        "publisher": "instagram", "account_ref": "1784", "campaign_url": "https://contentrewards.com/c/campaigns/x",
        "permission_evidence": "Campaign resource folder", "terms_text": "Brief text", "required_tags": "#ad",
        "disclosure_required": "caption", "min_duration_s": "15", "max_duration_s": "60",
        "layout": '{"template":"blur_pad"}', "pay_model": "cpm", "pay_currency": "usd", "pay_rate": "2",
    })
    assert r.status_code == 200 and "Saved as draft" in r.text
    r = client.post("/opportunities/smj-reach/approve", data={"csrf": tok})
    assert "Terms confirmed" in r.text
    op = row(ctx.conn, "SELECT * FROM opportunities WHERE id='smj-reach'")
    assert op["approved_terms_version"] == op["terms_version"] == 1
    assert '"currency": "USD"' in op["payment"]


def test_public_media_is_opaque(client, ctx):
    assert client.get("/m/short.mp4").status_code == 404
    assert client.get("/m/" + "a" * 32 + ".mp4").status_code == 404
    assert client.get("/clips/1/video", follow_redirects=False).status_code == 303   # private preview needs login


def test_pages_render_with_live_data(client, ctx):
    from tests.test_publishing import campaign, healthy_alerts, make_ready_clip, ready_ack
    from clipper.publishing.service import publish_clip
    from tests.conftest import drain

    page = client.get("/login").text
    client.post("/login", data={"password": "correct horse battery", "csrf": _csrf(page)})
    campaign(ctx)
    healthy_alerts(ctx)
    a = make_ready_clip(ctx, "camp")
    make_ready_clip(ctx, "camp", n=2)
    publish_clip(ctx, a, readiness_id=ready_ack(ctx, a))
    ctx.clock.advance(seconds=20)
    drain(ctx, "light")
    ledger.import_ledger_csv(ctx.conn, ctx.now(), CSV)
    dash = client.get("/").text
    assert "MANUAL STEP REQUIRED" in dash
    subs = client.get("/submissions").text
    assert "dry run — do not submit" in subs and "data-deadline" in subs
    for path in ("/session", "/clips?stage=ready", "/clips?stage=published", "/opportunities/camp", "/publications",
                 "/ledger?period=2026-11", "/jobs", "/capabilities"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    tok = _csrf(subs)
    sid = row(ctx.conn, "SELECT id FROM submissions")["id"]
    r = client.post(f"/submissions/{sid}/submitted", data={"csrf": tok, "receipt": "CR-778"})
    assert "Recorded as submitted" in r.text
