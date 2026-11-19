"""One fixed vertical template: 1080x1920, H.264/AAC, 30 fps, burned-in ASS
captions inside safe margins, original audio loudness-normalised.

Layouts (opportunity.layout JSON, calibrated at onboarding):
  {"template": "blur_pad"}            landscape video centred over a blurred fill
  {"template": "center_crop"}         centre crop to 9:16
  {"template": "gameplay_facecam",    fixed facecam (top) + gameplay (bottom) crops
   "expected_width": 1920, "expected_height": 1080,
   "facecam": {"x":0,"y":0,"w":480,"h":270}, "gameplay": {"x":240,"y":0,"w":1440,"h":1080},
   "facecam_height": 640}
A change in source resolution versus expected_width/height causes a hold;
general automatic speaker tracking is intentionally not attempted.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipper.jobs.queue import HoldError, PermanentError
from clipper.transcription.whisper import Transcript

W, H, FPS = 1080, 1920, 30


@dataclass
class Probe:
    duration: float
    width: int | None
    height: int | None
    vcodec: str | None
    acodec: str | None
    fps: float | None


def run(cmd: list[str], timeout: int = 3600, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise PermanentError(f"{cmd[0]} timed out after {timeout}s") from exc


def probe(path: Path) -> Probe:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], 120)
    if p.returncode != 0:
        raise PermanentError(f"ffprobe failed: {p.stderr[-500:]}")
    d = json.loads(p.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        n, _, den = v["avg_frame_rate"].partition("/")
        fps = float(n) / float(den or 1)
    return Probe(
        duration=float(d.get("format", {}).get("duration") or 0),
        width=v and int(v["width"]), height=v and int(v["height"]),
        vcodec=v and v.get("codec_name"), acodec=a and a.get("codec_name"), fps=fps,
    )


def layout_problems(layout: dict, pr: Probe) -> list[str]:
    exp_w, exp_h = layout.get("expected_width"), layout.get("expected_height")
    if exp_w and exp_h and (pr.width, pr.height) != (int(exp_w), int(exp_h)):
        return [f"source is {pr.width}x{pr.height}, layout calibrated for {exp_w}x{exp_h}"]
    if layout.get("template") == "gameplay_facecam" and not (exp_w and exp_h):
        return ["gameplay_facecam layout needs expected_width/height from onboarding calibration"]
    return []


def video_filter(layout: dict) -> str:
    t = layout.get("template", "blur_pad")
    if t == "center_crop":
        return f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0]"
    if t == "gameplay_facecam":
        fc, gp = layout["facecam"], layout["gameplay"]
        fh = int(layout.get("facecam_height", 640))
        gh = H - fh
        return (
            "[0:v]split=2[a][b];"
            f"[a]crop={fc['w']}:{fc['h']}:{fc['x']}:{fc['y']},scale={W}:{fh}:force_original_aspect_ratio=increase,"
            f"crop={W}:{fh}[top];"
            f"[b]crop={gp['w']}:{gp['h']}:{gp['x']}:{gp['y']},scale={W}:{gh}:force_original_aspect_ratio=increase,"
            f"crop={W}:{gh}[bot];"
            "[top][bot]vstack=inputs=2,setsar=1[v0]"
        )
    if t != "blur_pad":
        raise PermanentError(f"unknown layout template {t!r}")
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bgb];"
        f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]"
    )


# ---------------------------------------------------------------- captions --
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,74,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,0,2,90,90,430,1
Style: Hook,DejaVu Sans,62,&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,0,8,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def caption_events(t: Transcript, start: float, end: float, max_words: int = 3) -> list[tuple[float, float, str]]:
    words = [w for w in t.words() if w.start >= start - 0.05 and w.end <= end + 0.05 and w.text.strip()]
    events: list[tuple[float, float, str]] = []
    group: list = []

    def flush():
        if group:
            s = group[0].start - start
            e = group[-1].end - start
            events.append((s, max(e, s + 0.3), " ".join(w.text.strip() for w in group)))
            group.clear()

    for i, w in enumerate(words):
        if group and (len(group) >= max_words or w.start - group[-1].end > 0.6):
            flush()
        group.append(w)
        if w.text.strip()[-1:] in ".?!,":
            flush()
    flush()
    # no overlaps: each caption ends no later than the next one starts
    out = []
    clip_len = end - start
    for i, (s, e, txt) in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else clip_len
        out.append((s, min(e, nxt, clip_len), txt))
    return [ev for ev in out if ev[1] > ev[0]]


def write_ass(path: Path, events: list[tuple[float, float, str]], hook: str | None, clip_len: float) -> None:
    lines = [ASS_HEADER]
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(3.0, clip_len))},Hook,,0,0,0,,{_esc(hook)[:90]}\n")
    for s, e, txt in events:
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Caption,,0,0,0,,{_esc(txt)}\n")
    path.write_text("".join(lines), encoding="utf-8")


def parse_ass_times(path: Path) -> list[tuple[float, float]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            out.append((_parse_ts(parts[1]), _parse_ts(parts[2])))
    return out


def _parse_ts(v: str) -> float:
    h, m, s = v.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


# ------------------------------------------------------------------ render --
def render(src: Path, out: Path, start: float, end: float, layout: dict, ass_path: Path,
           timeout: int = 3600) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    vf = video_filter(layout) + f";[v0]fps={FPS},ass={ass_path.name},format=yuv420p[v]"
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src.resolve()),
        "-filter_complex", vf,
        "-map", "[v]", "-map", "0:a:0?",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart", "-threads", "2",
        str(tmp.resolve()),
    ]
    p = run(cmd, timeout, cwd=ass_path.parent)
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise HoldError(f"ffmpeg render failed: {p.stderr[-800:]}")
    tmp.replace(out)
