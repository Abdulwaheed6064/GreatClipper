"""Spending reservations against the monthly operating ceiling.

Before every paid request the worst-case cost is *reserved*. After the call the
reservation is committed with the actual cost (or released). Fixed monthly
commitments (VPS etc.) come from ledger rows of type ``fixed_commitment``.

Thresholds (section 10): 80% → alert, 90% → stop optional work, and stop any
new paid work whose reservation would exceed the ceiling. This is an
approximate usage counter, not a billing guarantee — set provider-side spend
limits too.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from clipper.db import iso, row, tx
from clipper.jobs.queue import PermanentError


class BudgetExceeded(PermanentError):
    pass


@dataclass
class BudgetStatus:
    period: str
    ceiling_minor: int
    model_allowance_minor: int
    fixed_minor: int
    variable_minor: int
    reserved_minor: int
    model_minor: int

    @property
    def used_minor(self) -> int:
        return self.fixed_minor + self.variable_minor + self.reserved_minor

    @property
    def fraction(self) -> float:
        return self.used_minor / self.ceiling_minor if self.ceiling_minor else 1.0


def period_of(now: datetime) -> str:
    return now.strftime("%Y-%m")


def status(conn: sqlite3.Connection, now: datetime, ceiling_usd: float, model_allowance_usd: float) -> BudgetStatus:
    p = period_of(now)
    fixed = row(
        conn,
        "SELECT COALESCE(SUM(amount_minor),0) s FROM ledger WHERE event_type='fixed_commitment' "
        "AND currency='USD' AND substr(effective_date,1,7)=?",
        (p,),
    )["s"]
    other_costs = row(
        conn,
        "SELECT COALESCE(SUM(amount_minor),0) s FROM ledger WHERE event_type='operating_cost' "
        "AND currency='USD' AND substr(effective_date,1,7)=?",
        (p,),
    )["s"]
    committed = row(
        conn,
        "SELECT COALESCE(SUM(actual_minor),0) s FROM spend WHERE period=? AND status='committed'",
        (p,),
    )["s"]
    reserved = row(
        conn,
        "SELECT COALESCE(SUM(reserved_minor),0) s FROM spend WHERE period=? AND status='reserved'",
        (p,),
    )["s"]
    model = row(
        conn,
        "SELECT COALESCE(SUM(CASE status WHEN 'committed' THEN actual_minor ELSE reserved_minor END),0) s "
        "FROM spend WHERE period=? AND category='claude' AND status!='released'",
        (p,),
    )["s"]
    return BudgetStatus(
        period=p,
        ceiling_minor=round(ceiling_usd * 100),
        model_allowance_minor=round(model_allowance_usd * 100),
        fixed_minor=fixed,
        variable_minor=committed + other_costs,
        reserved_minor=reserved,
        model_minor=model,
    )


def reserve(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    category: str,
    amount_minor: int,
    ceiling_usd: float,
    model_allowance_usd: float,
    optional: bool = False,
    ref: str | None = None,
) -> int:
    """Reserve worst-case spend or raise BudgetExceeded. Returns the reservation id."""
    amount_minor = max(1, int(amount_minor))
    with tx(conn):
        st = status(conn, now, ceiling_usd, model_allowance_usd)
        if optional and st.fraction >= 0.90:
            raise BudgetExceeded(f"optional work stopped: {st.fraction:.0%} of monthly ceiling used")
        if st.used_minor + amount_minor > st.ceiling_minor:
            raise BudgetExceeded(
                f"reservation of ${amount_minor/100:.2f} would exceed the ${st.ceiling_minor/100:.2f} ceiling "
                f"(used/reserved ${st.used_minor/100:.2f})"
            )
        if category == "claude" and st.model_minor + amount_minor > st.model_allowance_minor:
            raise BudgetExceeded(
                f"model allowance ${st.model_allowance_minor/100:.2f} would be exceeded "
                f"(used/reserved ${st.model_minor/100:.2f})"
            )
        cur = conn.execute(
            "INSERT INTO spend(period, category, optional, reserved_minor, status, ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)",
            (st.period, category, int(optional), amount_minor, ref, iso(now), iso(now)),
        )
        return cur.lastrowid


def commit(conn: sqlite3.Connection, now: datetime, reservation_id: int, actual_minor: int) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE spend SET status='committed', actual_minor=?, updated_at=? WHERE id=?",
            (max(0, int(round(actual_minor))), iso(now), reservation_id),
        )


def release(conn: sqlite3.Connection, now: datetime, reservation_id: int) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE spend SET status='released', updated_at=? WHERE id=? AND status='reserved'",
            (iso(now), reservation_id),
        )


def threshold_alert_level(st: BudgetStatus) -> str | None:
    if st.fraction >= 0.90:
        return "90"
    if st.fraction >= 0.80:
        return "80"
    return None
