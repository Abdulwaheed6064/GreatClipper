"""Permission, terms and release gates.

Every function returns a list of human-readable blocking reasons; an empty
list means "pass". Gates never raise for policy failures, so the dashboard can
show exactly why something is held.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from clipper.db import kv_get, parse_iso, row

# capability keys the owner records after the Day-1 live tests
PUBLISHER_CAPABILITIES = {
    "buffer": ["buffer.automatic_publish", "buffer.external_link"],
    "instagram": ["instagram.reels_publish", "instagram.permalink"],
    "manual": [],
}
NATIVE_DISCLOSURE_CAPABILITY = {
    "buffer": "buffer.native_paid_partnership",
    "instagram": "instagram.paid_partnership",
}


def capability(conn: sqlite3.Connection, key: str) -> str:
    r = row(conn, "SELECT status FROM capabilities WHERE key=?", (key,))
    return r["status"] if r else "unverified"


def source_gate(op: dict, now: datetime) -> list[str]:
    """May we spend download/transcription/model money on this opportunity?"""
    r = []
    if op["status"] != "active":
        r.append(f"opportunity is {op['status']}")
    if op.get("approved_terms_version") != op["terms_version"]:
        r.append("current terms are not confirmed by the owner")
    if not op.get("permission_evidence"):
        r.append("no permission evidence recorded")
    exp = parse_iso(op.get("expires_at"))
    if exp and exp <= now:
        r.append(f"terms/rights expired at {op['expires_at']}")
    until = parse_iso(op.get("allowed_until"))
    if until and until <= now:
        r.append("publication window has closed")
    return r


def caption_problems(op: dict, caption: str) -> list[str]:
    r = []
    low = caption.lower()
    for tag in op.get("required_tags") or []:
        if tag and tag.lower() not in low:
            r.append(f"required text {tag!r} missing from caption")
    return r


def clip_content_gate(op: dict, clip: dict) -> list[str]:
    r = []
    dur = clip["end_s"] - clip["start_s"]
    if dur < op["min_duration_s"] - 0.5 or dur > op["max_duration_s"] + 0.5:
        r.append(f"duration {dur:.1f}s outside {op['min_duration_s']}-{op['max_duration_s']}s")
    if clip.get("terms_version") != op["terms_version"]:
        r.append("clip was prepared under older terms")
    if clip.get("context_risk"):
        r.append("editor flagged possible missing context")
    return r


def calibration_needed(conn: sqlite3.Connection, op: dict, template_version: str, needed: int) -> bool:
    """The first N outputs per opportunity+template need human review."""
    n = row(
        conn,
        "SELECT COUNT(*) n FROM clips WHERE opportunity_id=? AND template_version=? AND review_state='approved'",
        (op["id"], template_version),
    )["n"]
    return n < needed


def release_gate(conn: sqlite3.Connection, op: dict, clip: dict, caption: str, now: datetime,
                 *, calibration_review_count: int) -> list[str]:
    """May a technically checked clip enter the ready queue?"""
    r = source_gate(op, now)
    r += clip_content_gate(op, clip)
    r += caption_problems(op, caption)
    if clip["review_state"] == "rejected":
        r.append("rejected by reviewer")
    elif clip["review_state"] != "approved" and calibration_needed(
        conn, op, clip["template_version"], calibration_review_count
    ):
        r.append(f"calibration: owner review required (first {calibration_review_count} clips)")
    if op.get("requires_preapproval") and clip.get("preapproval_state") != "approved":
        r.append("campaign requires draft pre-approval for this exact render")
    return r


def is_live(conn: sqlite3.Connection, mode: str, op_id: str) -> bool:
    return mode == "live" and op_id in (kv_get(conn, "live_opportunities", []) or [])


def publish_gate(conn: sqlite3.Connection, op: dict, clip: dict, caption: str, now: datetime, *,
                 mode: str, calibration_review_count: int, max_posts_per_day: int,
                 day_start_utc: datetime, readiness: dict | None, alerts_ok: tuple[bool, str]) -> list[str]:
    """Final check immediately before a publish call."""
    r = []
    if kv_get(conn, "paused", False):
        r.append("system is paused")
    if clip["stage"] != "ready":
        r.append(f"clip stage is {clip['stage']}")
    r += release_gate(conn, op, clip, caption, now, calibration_review_count=calibration_review_count)

    posted = row(
        conn,
        "SELECT COUNT(*) n FROM publications WHERE created_at>=? AND status NOT IN ('failed','cancelled')",
        (day_start_utc.isoformat(timespec="seconds"),),
    )["n"]
    if posted >= max_posts_per_day:
        r.append(f"daily cap of {max_posts_per_day} post(s) reached")

    unresolved = row(conn, "SELECT COUNT(*) n FROM publications WHERE status='unknown'")["n"]
    if unresolved:
        r.append(f"{unresolved} publication(s) in unknown state must be reconciled first")

    live = is_live(conn, mode, op["id"])
    if live:
        if op["publisher"] == "manual":
            r.append("publisher is manual: publish by hand")
        for cap in PUBLISHER_CAPABILITIES.get(op["publisher"], []):
            if capability(conn, cap) != "verified":
                r.append(f"capability {cap} not verified on the live account")
        if op["disclosure_required"] == "native_paid_partnership":
            cap = NATIVE_DISCLOSURE_CAPABILITY.get(op["publisher"])
            if not cap or capability(conn, cap) != "verified":
                r.append("native paid-partnership label required but not verified for this publisher; "
                         "publish manually or choose another destination")

    if op["revenue_route"] == "campaign":
        if not alerts_ok[0]:
            r.append(f"alert channel unhealthy: {alerts_ok[1]}")
        open_sub = row(conn, "SELECT id FROM submissions WHERE status IN ('manual_required','overdue') LIMIT 1")
        if open_sub:
            r.append(f"submission #{open_sub['id']} is not confirmed yet")
        pending_pub = row(
            conn,
            "SELECT p.id FROM publications p JOIN opportunities o ON o.id=p.opportunity_id "
            "WHERE o.revenue_route='campaign' AND p.status IN ('reserved','sending','accepted') LIMIT 1",
        )
        if pending_pub:
            r.append(f"campaign publication #{pending_pub['id']} is still in flight")
        if not readiness:
            r.append("no operator readiness acknowledgement (attended session required)")
        else:
            if readiness["clip_id"] != clip["id"] or readiness["opportunity_id"] != op["id"]:
                r.append("readiness acknowledgement is for a different clip")
            if readiness.get("used_at"):
                r.append("readiness acknowledgement already used")
            if parse_iso(readiness["expires_at"]) <= now:
                r.append("readiness acknowledgement expired; renew it")
        obs = parse_iso(op.get("budget_observed_at"))
        if not obs or now - obs > timedelta(minutes=30):
            r.append("campaign budget observation is stale; re-check it in this session")
    return r
