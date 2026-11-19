"""Campaign / client / owned-channel configuration ("opportunities").

Any change to the terms-bearing fields bumps ``terms_version`` and clears the
human approval, so the new terms must be confirmed before they control posting,
and unposted clips are re-validated against them (see rules.gates).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime

from clipper.db import audit, iso, jdump, jload, row, rows, tx

JSON_FIELDS = ("required_tags", "sponsor_ids", "layout", "posting_times", "payment")

TERMS_FIELDS = (
    "revenue_route", "source_url", "campaign_url", "permission_evidence", "terms_text",
    "expires_at", "allowed_from", "allowed_until", "platform", "publisher", "account_ref",
    "required_tags", "disclosure_required", "sponsor_ids", "permitted_audio", "min_duration_s",
    "max_duration_s", "prohibited", "reuse_constraints", "requires_preapproval", "payment",
    "caption_template", "layout",
)

EDITABLE_FIELDS = TERMS_FIELDS + (
    "name", "owner_contact", "feed_url", "posting_times", "editorial_notes", "verified_at",
)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def decode(op: dict | None) -> dict | None:
    if op is None:
        return None
    out = dict(op)
    for f in JSON_FIELDS:
        out[f] = jload(op.get(f), {} if f in ("layout", "payment") else [])
    return out


def get(conn: sqlite3.Connection, opp_id: str) -> dict | None:
    return decode(row(conn, "SELECT * FROM opportunities WHERE id=?", (opp_id,)))


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [decode(r) for r in rows(conn, "SELECT * FROM opportunities ORDER BY status, name")]


def _encode(values: dict) -> dict:
    out = {}
    for k, v in values.items():
        if k not in EDITABLE_FIELDS:
            continue
        out[k] = jdump(v) if k in JSON_FIELDS else v
    return out


def _terms_hash(op: dict) -> str:
    material = jdump({k: op.get(k) for k in TERMS_FIELDS})
    return hashlib.sha256(material.encode()).hexdigest()


def create(conn: sqlite3.Connection, now: datetime, opp_id: str, values: dict, actor: str = "owner") -> dict:
    if not ID_RE.match(opp_id):
        raise ValueError("id must be 2-63 chars: lowercase letters, digits, - or _")
    enc = _encode(values)
    enc.setdefault("name", opp_id)
    enc.setdefault("revenue_route", values.get("revenue_route", "campaign"))
    with tx(conn):
        cols = ["id", "created_at", "updated_at", *enc.keys()]
        conn.execute(
            f"INSERT INTO opportunities({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            (opp_id, iso(now), iso(now), *enc.values()),
        )
        op = get(conn, opp_id)
        conn.execute("UPDATE opportunities SET terms_hash=? WHERE id=?", (_terms_hash(op), opp_id))
        _snapshot(conn, now, opp_id)
        audit(conn, actor, "opportunity.create", {"id": opp_id})
    return get(conn, opp_id)


def update(conn: sqlite3.Connection, now: datetime, opp_id: str, values: dict, actor: str = "owner") -> dict:
    enc = _encode(values)
    with tx(conn):
        before = get(conn, opp_id)
        if before is None:
            raise KeyError(opp_id)
        if enc:
            sets = ", ".join(f"{k}=?" for k in enc)
            conn.execute(f"UPDATE opportunities SET {sets}, updated_at=? WHERE id=?",
                         (*enc.values(), iso(now), opp_id))
        after = get(conn, opp_id)
        new_hash = _terms_hash(after)
        if new_hash != before["terms_hash"]:
            conn.execute(
                "UPDATE opportunities SET terms_version=terms_version+1, terms_hash=?, "
                "approved_terms_version=NULL, approved_at=NULL WHERE id=?",
                (new_hash, opp_id),
            )
            _snapshot(conn, now, opp_id)
            # unposted clips must be revalidated against the new terms
            conn.execute(
                "UPDATE clips SET stage='held', hold_reason='terms changed; revalidate', updated_at=? "
                "WHERE opportunity_id=? AND stage IN ('ready','checked')",
                (iso(now), opp_id),
            )
            audit(conn, actor, "opportunity.terms_changed", {"id": opp_id, "version": after["terms_version"] + 1})
        else:
            audit(conn, actor, "opportunity.update", {"id": opp_id})
    return get(conn, opp_id)


def _snapshot(conn: sqlite3.Connection, now: datetime, opp_id: str) -> None:
    op = row(conn, "SELECT * FROM opportunities WHERE id=?", (opp_id,))
    conn.execute(
        "INSERT OR IGNORE INTO opportunity_terms_history(opportunity_id, terms_version, snapshot, created_at) "
        "VALUES (?, ?, ?, ?)",
        (opp_id, op["terms_version"], jdump(op), iso(now)),
    )


def approve_terms(conn: sqlite3.Connection, now: datetime, opp_id: str, actor: str = "owner") -> dict:
    """Human confirmation that the current terms (version N) are verified."""
    with tx(conn):
        op = get(conn, opp_id)
        problems = terms_problems(op)
        if problems:
            raise ValueError("cannot approve: " + "; ".join(problems))
        conn.execute(
            "UPDATE opportunities SET approved_terms_version=terms_version, approved_at=?, verified_at=?, "
            "updated_at=? WHERE id=?",
            (iso(now), iso(now), iso(now), opp_id),
        )
        # clips held only because terms changed can be re-checked now
        conn.execute(
            "UPDATE clips SET stage='checked', hold_reason=NULL, terms_version=?, updated_at=? "
            "WHERE opportunity_id=? AND stage='held' AND hold_reason='terms changed; revalidate'",
            (op["terms_version"], iso(now), opp_id),
        )
        audit(conn, actor, "opportunity.approve_terms", {"id": opp_id, "version": op["terms_version"]})
    return get(conn, opp_id)


def set_status(conn: sqlite3.Connection, now: datetime, opp_id: str, status: str, actor: str = "owner") -> None:
    with tx(conn):
        conn.execute("UPDATE opportunities SET status=?, updated_at=? WHERE id=?", (status, iso(now), opp_id))
        audit(conn, actor, "opportunity.status", {"id": opp_id, "status": status})


def terms_problems(op: dict) -> list[str]:
    """Missing information that makes approval impossible. Unknown economics are
    allowed (left unknown), but rights and routing are not."""
    p = []
    if not op.get("permission_evidence"):
        p.append("permission evidence is missing")
    if not (op.get("terms_text") or "").strip():
        p.append("terms text/snapshot is missing")
    if op["revenue_route"] == "campaign" and not op.get("campaign_url"):
        p.append("campaign URL is missing")
    if op["publisher"] != "manual" and not op.get("account_ref"):
        p.append("publisher account/channel is missing")
    pay = op.get("payment") or {}
    if not pay.get("model"):
        p.append("payment model is missing (use 'unmonetized' if none)")
    if pay.get("model") not in (None, "unmonetized") and not pay.get("currency"):
        p.append("payment currency is missing")
    if op["min_duration_s"] >= op["max_duration_s"]:
        p.append("min duration must be below max duration")
    return p
