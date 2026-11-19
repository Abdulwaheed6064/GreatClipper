"""Turn a transcript into validated, ranked clip candidates.

Validation is deterministic and server-side: bounds against the real duration,
duration limits from the brief, evidence must be in the transcript range,
ranges snapped to word boundaries, overlaps removed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from clipper import opportunities
from clipper.budget import spend
from clipper.context import Ctx
from clipper.db import iso, jdump, row, tx
from clipper.editorial.claude import PROMPT_VERSION, cache_key, worst_case_cost_minor
from clipper.jobs.queue import HoldError
from clipper.transcription.whisper import Transcript

DEFAULT_WEIGHTS = {"clarity": 1.0, "context": 1.0, "payoff": 1.5, "distinctness": 0.75, "campaign_fit": 1.25}
_norm_re = re.compile(r"[^a-z0-9' ]+")


def _norm(text: str) -> list[str]:
    return _norm_re.sub(" ", text.lower()).split()


def build_brief(op: dict) -> str:
    lines = [
        f"Route: {op['revenue_route']}",
        f"Destination platform: {op.get('platform') or 'unspecified'}",
        f"Clip duration: between {op['min_duration_s']:.0f} and {op['max_duration_s']:.0f} seconds",
        f"Audio: {op.get('permitted_audio')}",
    ]
    if op.get("required_tags"):
        lines.append("Caption must contain (the system appends these automatically): " + ", ".join(op["required_tags"]))
    if op.get("prohibited"):
        lines.append(f"Prohibited material: {op['prohibited']}")
    if op.get("editorial_notes"):
        lines.append(f"Editorial notes from the owner: {op['editorial_notes']}")
    return "\n".join(lines)


def windows(t: Transcript, window_min: int, overlap_min: int) -> list[tuple[float, float]]:
    dur = t.duration or (t.segments[-1].end if t.segments else 0)
    size, step = window_min * 60, max(60, (window_min - overlap_min) * 60)
    if dur <= size:
        return [(0.0, dur)]
    out, start = [], 0.0
    while start < dur:
        out.append((start, min(dur, start + size)))
        if start + size >= dur:
            break
        start += step
    return out


def transcript_block(t: Transcript, start: float, end: float) -> str:
    return "\n".join(
        f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in t.segments if s.end > start and s.start < end
    )


def make_validator(t: Transcript, op: dict, lo: float, hi: float):
    words = t.words()
    min_d, max_d = op["min_duration_s"], op["max_duration_s"]

    def snap(start: float, end: float) -> tuple[float, float] | None:
        inside = [w for w in words if w.start >= start - 0.75 and w.end <= end + 0.75]
        if not inside:
            return None
        return max(0.0, inside[0].start - 0.15), min(t.duration, inside[-1].end + 0.25)

    def validate(raw: list) -> tuple[list[dict], list[str]]:
        valid, errors = [], []
        if not isinstance(raw, list):
            return [], ["candidates must be a list"]
        for i, c in enumerate(raw[:20]):
            tag = f"candidate {i + 1}"
            try:
                s, e = float(c["start_s"]), float(c["end_s"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"{tag}: missing/invalid timestamps")
                continue
            if not (0 <= s < e <= t.duration + 0.5) or s < lo - 1 or e > hi + 1:
                errors.append(f"{tag}: range {s:.1f}-{e:.1f}s is outside the transcript window {lo:.0f}-{hi:.0f}s")
                continue
            snapped = snap(s, e)
            if not snapped:
                errors.append(f"{tag}: no speech inside {s:.1f}-{e:.1f}s")
                continue
            s, e = snapped
            if not (min_d - 0.5 <= e - s <= max_d + 0.5):
                errors.append(f"{tag}: duration {e - s:.1f}s not within {min_d:.0f}-{max_d:.0f}s")
                continue
            quote = _norm(str(c.get("evidence_quote", "")))
            text = t.text_between(s, e)
            have = set(_norm(text))
            if len(quote) < 3 or sum(1 for w in quote if w in have) / len(quote) < 0.8:
                errors.append(f"{tag}: evidence_quote is not found in the transcript between {s:.1f}s and {e:.1f}s")
                continue
            scores = c.get("scores") or {}
            try:
                scores = {k: max(1, min(5, int(scores[k]))) for k in DEFAULT_WEIGHTS}
            except (KeyError, TypeError, ValueError):
                errors.append(f"{tag}: scores incomplete")
                continue
            hashtags = [str(h).strip()[:40] for h in (c.get("hashtags") or [])[:8] if str(h).strip()]
            valid.append({
                "start_s": round(s, 2), "end_s": round(e, 2),
                "hook": str(c.get("hook", ""))[:200], "payoff": str(c.get("payoff", ""))[:300],
                "title": str(c.get("title", ""))[:100], "caption": str(c.get("caption", ""))[:1500],
                "hashtags": hashtags, "evidence": str(c.get("evidence_quote", ""))[:500],
                "scores": scores, "context_risk": bool(c.get("context_risk")),
                "notes": str(c.get("notes", ""))[:500], "transcript_text": text[:5000],
            })
        return valid, errors

    return validate


def rank(cands: list[dict], weights: dict | None = None) -> list[dict]:
    w = weights or DEFAULT_WEIGHTS
    for c in cands:
        c["score_total"] = round(sum(c["scores"][k] * w.get(k, 1.0) for k in DEFAULT_WEIGHTS), 3)
    cands = sorted(cands, key=lambda c: -c["score_total"])
    chosen: list[dict] = []
    for c in cands:  # drop overlaps (>40% of the shorter clip)
        clash = False
        for k in chosen:
            ov = min(c["end_s"], k["end_s"]) - max(c["start_s"], k["start_s"])
            if ov > 0.4 * min(c["end_s"] - c["start_s"], k["end_s"] - k["start_s"]):
                clash = True
                break
        if not clash:
            chosen.append(c)
    return chosen


def select_for_source(ctx: Ctx, source_id: int) -> list[int]:
    """Run editorial selection; insert clips; enqueue renders. Returns clip ids."""
    conn, st = ctx.conn, ctx.settings
    src = row(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    op = opportunities.get(conn, src["opportunity_id"])
    t = Transcript.from_json(open(src["transcript_path"]).read())
    brief = build_brief(op)
    all_valid: list[dict] = []
    all_errors: list[str] = []
    total_cost = 0
    model_used = ctx.editor.model
    for lo, hi in windows(t, st.editorial_window_minutes, st.editorial_window_overlap_minutes):
        block = transcript_block(t, lo, hi)
        if not block.strip():
            continue
        key = cache_key(src["file_hash"] or str(source_id), f"{lo:.0f}-{hi:.0f}", str(op["terms_version"]),
                        PROMPT_VERSION, ctx.editor.model, brief)
        cached = row(conn, "SELECT response FROM editorial_cache WHERE cache_key=?", (key,))
        if cached:
            all_valid += json.loads(cached["response"])
            continue
        validator = make_validator(t, op, lo, hi)
        worst = worst_case_cost_minor(st, brief + block) * 2  # one repair allowed
        res_id = spend.reserve(conn, ctx.now(), category="claude", amount_minor=worst,
                               ceiling_usd=st.monthly_operating_ceiling_usd,
                               model_allowance_usd=st.monthly_model_allowance_usd, ref=f"select:{source_id}")
        try:
            valid, cost, model_used, errors = ctx.editor.select(brief, block, st.max_candidates_per_source, validator)
        except Exception:
            spend.release(conn, ctx.now(), res_id)
            raise
        spend.commit(conn, ctx.now(), res_id, cost)
        total_cost += cost
        all_errors += errors
        with tx(conn):
            conn.execute("INSERT OR REPLACE INTO editorial_cache(cache_key, response, cost_minor, created_at) "
                         "VALUES (?, ?, ?, ?)", (key, json.dumps(valid), cost, iso(ctx.now())))
        all_valid += valid

    chosen = rank(all_valid, st.score_weights)[: st.max_candidates_per_source]
    now = ctx.now()
    ids: list[int] = []
    if not chosen:
        reason = "no usable candidates" + (f": {all_errors[0]}" if all_errors else "")
        with tx(conn):
            conn.execute("UPDATE sources SET processing_cost_minor=processing_cost_minor+?, stage='held', "
                         "hold_reason=?, updated_at=? WHERE id=?", (total_cost, reason[:500], iso(now), source_id))
        raise HoldError(reason)
    with tx(conn):
        conn.execute("UPDATE sources SET processing_cost_minor=processing_cost_minor+?, updated_at=? WHERE id=?",
                     (total_cost, iso(now), source_id))
        for c in chosen:
            cur = conn.execute(
                "INSERT INTO clips(source_id, opportunity_id, start_s, end_s, hook, payoff, title, caption, hashtags, "
                "evidence, scores, score_total, context_risk, terms_version, template_version, model_version, "
                "transcript_text, review_state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id, start_s, end_s, template_version) DO NOTHING",
                (source_id, op["id"], c["start_s"], c["end_s"], c["hook"], c["payoff"], c["title"], c["caption"],
                 jdump(c["hashtags"]), c["evidence"], jdump(c["scores"]), c["score_total"], int(c["context_risk"]),
                 op["terms_version"], st.template_version, f"{model_used}/{PROMPT_VERSION}", c["transcript_text"],
                 "pending", iso(now), iso(now)),
            )
            if cur.rowcount:
                ids.append(cur.lastrowid)
        conn.execute("UPDATE sources SET stage='selected', updated_at=? WHERE id=?", (iso(now), source_id))
    from clipper.jobs.queue import enqueue

    for cid in ids:
        enqueue(conn, queue="heavy", type="render_clip", payload={"clip_id": cid}, due_at=now,
                dedupe_key=f"render:{cid}")
    return ids
