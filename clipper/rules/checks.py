"""Deterministic technical checks on a rendered clip + duplicate guards.

Thresholds are starting points that need calibration; similarity checks cannot
guarantee every near-duplicate is caught.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from clipper.db import rows
from clipper.rendering.ffmpeg import H, W, parse_ass_times, probe, run

_black_re = re.compile(r"black_duration:\s*([\d.]+)")
_freeze_re = re.compile(r"freeze_duration:\s*([\d.]+)")
_meanvol_re = re.compile(r"mean_volume:\s*(-?[\d.]+|-inf) dB")


def media_checks(path: Path, expected_duration: float, ass_path: Path | None) -> dict:
    results: dict[str, dict] = {}

    def put(name: str, ok: bool, detail: str = "") -> None:
        results[name] = {"ok": bool(ok), "detail": detail}

    pr = probe(path)
    put("dimensions", (pr.width, pr.height) == (W, H), f"{pr.width}x{pr.height}")
    put("video_codec", pr.vcodec == "h264", str(pr.vcodec))
    put("audio_present", pr.acodec is not None, str(pr.acodec))
    put("duration", abs(pr.duration - expected_duration) <= 0.75, f"{pr.duration:.2f}s vs {expected_duration:.2f}s")

    # full decode + black/frozen/volume analysis in one pass
    p = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
             "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=1.5",
             "-af", "volumedetect", "-f", "null", "-"], timeout=1800)
    errs = [l for l in p.stderr.splitlines() if "error" in l.lower() and "volumedetect" not in l]
    put("decodes", p.returncode == 0 and not errs, errs[0][:200] if errs else "")
    black = sum(float(x) for x in _black_re.findall(p.stderr))
    freeze = sum(float(x) for x in _freeze_re.findall(p.stderr))
    dur = max(pr.duration, 0.1)
    put("black_frames", black <= max(1.0, 0.10 * dur), f"{black:.1f}s black")
    put("frozen_frames", freeze <= max(3.0, 0.20 * dur), f"{freeze:.1f}s frozen")
    m = _meanvol_re.search(p.stderr)
    mean = float("-inf") if not m or m.group(1) == "-inf" else float(m.group(1))
    put("audio_level", mean > -50.0, f"mean {mean} dB")

    if ass_path is not None and ass_path.exists():
        times = parse_ass_times(ass_path)
        bad = [t for t in times if t[0] < -0.01 or t[1] > expected_duration + 0.05 or t[1] <= t[0]]
        put("subtitle_timing", not bad and bool(times), f"{len(times)} events, {len(bad)} out of range")
    return results


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    w = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split()
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def duplicate_checks(conn: sqlite3.Connection, clip: dict, render_hash: str) -> dict:
    results: dict[str, dict] = {}
    same = rows(conn, "SELECT id FROM clips WHERE render_hash=? AND id!=? AND stage!='rejected'",
                (render_hash, clip["id"]))
    results["exact_duplicate"] = {"ok": not same, "detail": f"same file as clip {same[0]['id']}" if same else ""}

    overlaps = rows(
        conn,
        "SELECT id, start_s, end_s FROM clips WHERE source_id=? AND id!=? AND stage NOT IN ('rejected') "
        "AND start_s < ? AND end_s > ?",
        (clip["source_id"], clip["id"], clip["end_s"], clip["start_s"]),
    )
    worst = 0.0
    for o in overlaps:
        ov = min(o["end_s"], clip["end_s"]) - max(o["start_s"], clip["start_s"])
        worst = max(worst, ov / max(0.1, clip["end_s"] - clip["start_s"]))
    results["source_overlap"] = {"ok": worst <= 0.5, "detail": f"max overlap {worst:.0%}"}

    others = rows(
        conn,
        "SELECT id, transcript_text FROM clips WHERE opportunity_id=? AND id!=? "
        "AND stage IN ('ready','published','checked')",
        (clip["opportunity_id"], clip["id"]),
    )
    sim = max((jaccard(clip.get("transcript_text") or "", o["transcript_text"] or "") for o in others), default=0.0)
    results["transcript_similarity"] = {"ok": sim < 0.7, "detail": f"max similarity {sim:.2f}"}
    return results
