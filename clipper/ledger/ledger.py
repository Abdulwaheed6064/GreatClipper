"""Earnings/cost ledger, idempotent CSV imports and profitability.

Rules from the plan:
* integer minor units, currencies never mixed without an explicit dated FX rate;
* withdrawals move existing money and are never counted as revenue;
* public views are not qualified views; unknown stays unknown (None), not zero;
* imports are idempotent via external ids; corrections are 'adjustment' rows.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from clipper.db import audit, iso, row, rows, tx

EVENT_TYPES = ("estimated_gross", "pending", "approved", "settled", "withdrawal", "fee", "operating_cost",
               "fixed_commitment", "adjustment", "client_fee_invoiced")
REVENUE_TYPES = ("settled",)
LEDGER_COLUMNS = ["external_id", "effective_date", "currency", "amount", "event_type", "opportunity_id",
                  "clip_id", "source_evidence", "note"]
METRIC_COLUMNS = ["external_key", "live_url", "platform_views", "qualified_views", "as_of", "source"]


def to_minor(amount: str | Decimal | float) -> int:
    try:
        d = Decimal(str(amount).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"invalid amount {amount!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_minor(minor: int | None, currency: str = "") -> str:
    if minor is None:
        return "unknown"
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100:,}.{abs(minor) % 100:02d} {currency}".strip()


def add_entry(conn: sqlite3.Connection, now: datetime, *, currency: str, amount: str, event_type: str,
              source_evidence: str, effective_date: str, opportunity_id: str | None = None,
              clip_id: int | None = None, external_id: str | None = None, note: str | None = None,
              actor: str = "owner") -> bool:
    currency = currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a 3-letter code")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
    if not (source_evidence or "").strip():
        raise ValueError("source evidence is required (where this number came from)")
    date.fromisoformat(effective_date)
    minor = to_minor(amount)
    if event_type in ("fee", "operating_cost", "fixed_commitment", "withdrawal") and minor < 0:
        raise ValueError("enter costs/fees/withdrawals as positive amounts")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO ledger(currency, amount_minor, event_type, opportunity_id, clip_id, external_id, "
            "source_evidence, effective_date, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(event_type, external_id) WHERE external_id IS NOT NULL DO NOTHING",
            (currency, minor, event_type, opportunity_id or None, clip_id, external_id or None, source_evidence,
             effective_date, note, iso(now)),
        )
        if cur.rowcount:
            audit(conn, actor, "ledger.add", {"type": event_type, "amount": minor, "currency": currency,
                                               "external_id": external_id})
    return bool(cur.rowcount)


def import_ledger_csv(conn: sqlite3.Connection, now: datetime, text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    missing = {"external_id", "effective_date", "currency", "amount", "event_type", "source_evidence"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
    out = {"inserted": 0, "duplicates": 0, "errors": []}
    for i, r in enumerate(reader, start=2):
        if not (r.get("external_id") or "").strip():
            out["errors"].append(f"line {i}: external_id is required for imports (idempotency)")
            continue
        try:
            ok = add_entry(conn, now, currency=r["currency"], amount=r["amount"], event_type=r["event_type"].strip(),
                           source_evidence=r["source_evidence"], effective_date=r["effective_date"].strip(),
                           opportunity_id=(r.get("opportunity_id") or "").strip() or None,
                           clip_id=int(r["clip_id"]) if (r.get("clip_id") or "").strip() else None,
                           external_id=r["external_id"].strip(), note=r.get("note"), actor="csv_import")
            out["inserted" if ok else "duplicates"] += 1
        except (ValueError, KeyError) as exc:
            out["errors"].append(f"line {i}: {exc}")
    return out


def import_metrics_csv(conn: sqlite3.Connection, now: datetime, text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    missing = {"external_key", "live_url", "as_of", "source"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
    out = {"inserted": 0, "duplicates": 0, "errors": []}

    def num(v):
        v = (v or "").strip()
        return int(v.replace(",", "")) if v else None  # blank = unknown, not zero

    for i, r in enumerate(reader, start=2):
        try:
            pub = row(conn, "SELECT id FROM publications WHERE live_url=?", (r["live_url"].strip(),))
            with tx(conn):
                cur = conn.execute(
                    "INSERT INTO metrics(publication_id, live_url, platform_views, qualified_views, source, as_of, "
                    "external_key, created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(external_key) DO NOTHING",
                    (pub["id"] if pub else None, r["live_url"].strip(), num(r.get("platform_views")),
                     num(r.get("qualified_views")), r["source"].strip(), r["as_of"].strip(),
                     r["external_key"].strip(), iso(now)),
                )
            out["inserted" if cur.rowcount else "duplicates"] += 1
        except (ValueError, KeyError) as exc:
            out["errors"].append(f"line {i}: {exc}")
    return out


def export_ledger_csv(conn: sqlite3.Connection) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", *LEDGER_COLUMNS, "created_at"])
    for r in rows(conn, "SELECT * FROM ledger ORDER BY effective_date, id"):
        w.writerow([r["id"], r["external_id"] or "", r["effective_date"], r["currency"],
                    fmt_minor(r["amount_minor"]).replace(",", ""), r["event_type"], r["opportunity_id"] or "",
                    r["clip_id"] or "", r["source_evidence"], r["note"] or "", r["created_at"]])
    return buf.getvalue()


# -------------------------------------------------------------- estimators --
def estimate_cpm(payment: dict, qualified_views: int | None, remaining_budget_minor: int | None) -> dict:
    """Conservative CPM estimate for ONE clip. Returns {'low','high','notes'} in
    minor units, or low/high None when it must be withheld."""
    notes: list[str] = []
    if payment.get("model") != "cpm":
        return {"low": None, "high": None, "notes": ["not a CPM campaign"]}
    if qualified_views is None:
        return {"low": None, "high": None, "notes": ["qualified views unknown (public views are not qualified views)"]}
    try:
        rate = Decimal(str(payment.get("rate")))
    except (InvalidOperation, TypeError):
        return {"low": None, "high": None, "notes": ["CPM rate unknown"]}
    gross = Decimal(qualified_views) / 1000 * rate
    low = high = gross
    sem = payment.get("min_payable_semantics") or ("unknown" if payment.get("min_payable") or payment.get("min_views") else "none")
    if sem == "per_clip_earnings_minimum":
        if gross < Decimal(str(payment.get("min_payable") or 0)):
            low = high = Decimal(0)
    elif sem == "per_clip_views_minimum":
        if qualified_views < int(payment.get("min_views") or 0):
            low = high = Decimal(0)
    elif sem == "unknown":
        low = Decimal(0)
        notes.append("minimum-payable semantics unknown: low estimate assumes nothing is payable")
    if payment.get("max_per_clip"):
        cap = Decimal(str(payment["max_per_clip"]))
        low, high = min(low, cap), min(high, cap)
    if remaining_budget_minor is None:
        low = Decimal(0)
        notes.append("remaining campaign budget unknown: low estimate is zero")
    else:
        rem = Decimal(remaining_budget_minor) / 100
        low, high = min(low, rem), min(high, rem)
        notes.append("budget observation is shared and not a reservation")
    if payment.get("fee_exempt"):
        fee_lo = fee_hi = Decimal(0)
    elif payment.get("fee_rate") not in (None, ""):
        fee_lo = fee_hi = Decimal(str(payment["fee_rate"]))
    else:
        return {"low": None, "high": None, "notes": notes + ["creator fee unknown: estimate withheld"]}
    low, high = low * (1 - fee_hi), high * (1 - fee_lo)
    return {"low": to_minor(low), "high": to_minor(high), "notes": notes}


# ----------------------------------------------------------- profitability --
def profitability(conn: sqlite3.Connection, hourly_rate_usd: float, period: str | None = None) -> dict:
    """Per-opportunity, per-currency summary. ``period`` = 'YYYY-MM' or None for all time."""
    where, params = "", []
    if period:
        where, params = " WHERE substr(effective_date,1,7)=?", [period]
    sums: dict = defaultdict(lambda: defaultdict(int))
    for r in rows(conn, f"SELECT opportunity_id, currency, event_type, SUM(amount_minor) s FROM ledger{where} "
                        "GROUP BY opportunity_id, currency, event_type", tuple(params)):
        sums[(r["opportunity_id"] or "(shared)", r["currency"])][r["event_type"]] = r["s"]
    proc = {r["opportunity_id"]: r["s"] for r in rows(
        conn, "SELECT opportunity_id, SUM(processing_cost_minor) s FROM sources"
        + (" WHERE substr(discovered_at,1,7)=?" if period else "") + " GROUP BY opportunity_id", tuple(params))}
    tw = " WHERE substr(work_date,1,7)=?" if period else ""
    minutes = {r["opportunity_id"] or "(shared)": r["m"] for r in rows(
        conn, f"SELECT opportunity_id, SUM(minutes) m FROM operator_time{tw} GROUP BY opportunity_id", tuple(params))}
    accepted = {r["opportunity_id"]: r["n"] for r in rows(
        conn, "SELECT opportunity_id, COUNT(*) n FROM submissions WHERE review_state='approved' GROUP BY opportunity_id")}
    out = []
    keys = set(sums) | {(k, "USD") for k in proc if proc[k]} | {(k, "USD") for k in minutes}
    for opp, cur in sorted(keys):
        s = sums.get((opp, cur), {})
        processing = proc.get(opp, 0) if cur == "USD" else 0
        settled = s.get("settled", 0) + s.get("adjustment", 0)
        fees = s.get("fee", 0)
        costs = s.get("operating_cost", 0) + s.get("fixed_commitment", 0) + processing
        cash = settled - fees - costs
        mins = minutes.get(opp, 0) if cur == "USD" else 0
        labor = round(mins / 60 * hourly_rate_usd * 100)
        out.append({
            "opportunity_id": opp, "currency": cur,
            "estimated_gross": s.get("estimated_gross"), "pending": s.get("pending"), "approved": s.get("approved"),
            "settled": settled, "fees": fees, "withdrawn": s.get("withdrawal", 0),
            "client_fees_invoiced": s.get("client_fee_invoiced"),
            "costs": costs, "cash_contribution": cash, "operator_minutes": mins,
            "contribution_after_labor": cash - labor if cur == "USD" else None,
            "per_operator_hour": round(cash / (mins / 60)) if mins and cur == "USD" else None,
            "accepted_clips": accepted.get(opp, 0),
        })
    return {"rows": out, "hourly_rate_usd": hourly_rate_usd, "period": period or "all time"}


def log_time(conn: sqlite3.Connection, now: datetime, minutes: int, activity: str, work_date: str,
             opportunity_id: str | None = None) -> None:
    if minutes <= 0 or minutes > 24 * 60:
        raise ValueError("minutes must be between 1 and 1440")
    date.fromisoformat(work_date)
    with tx(conn):
        conn.execute("INSERT INTO operator_time(opportunity_id, minutes, activity, work_date, created_at) "
                     "VALUES (?,?,?,?,?)", (opportunity_id or None, minutes, activity[:200], work_date, iso(now)))
