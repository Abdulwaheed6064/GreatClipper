from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from clipper.alerts import Alerter
from clipper.config import Env, Settings, ensure_dirs
from clipper.context import Ctx
from clipper.db import connect, migrate
from clipper.editorial.claude import Editor
from clipper.publishing.registry import build_publishers
from clipper.transcription.whisper import Segment, Transcript, Word


# ------------------------------------------------------------------ fakes --
class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class RecordingTransport:
    def __init__(self):
        self.sent: list[str] = []
        self.buttons: list = []
        self.fail = False

    def send(self, text: str, buttons: list | None = None) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        self.buttons.append(buttons)


class FakeTelegramAPI:
    """Stands in for clipper.telegram.api.TelegramAPI."""

    def __init__(self, chat_id: str = "42"):
        self.chat_id = chat_id
        self.messages: list[tuple[str, list | None]] = []
        self.videos: list[tuple[str, str, list | None]] = []
        self.cleared: list[int] = []

    def send_message(self, text, buttons=None, chat_id=None):
        self.messages.append((text, buttons))
        return {"message_id": len(self.messages)}

    def send_video(self, path, caption, buttons=None):
        self.videos.append((str(path), caption, buttons))
        return {"message_id": 1000 + len(self.videos)}

    def answer_callback(self, cid, text=""):
        pass

    def clear_buttons(self, message_id):
        self.cleared.append(message_id)

    def download_file(self, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff fake image " + file_id.encode())
        return dest

    def set_commands(self, commands):
        pass

    @property
    def last(self) -> str:
        return self.messages[-1][0] if self.messages else ""


class FakeTranscriber:
    model_name = "fake"

    def __init__(self, language_probability: float = 0.98):
        self.calls = 0
        self.lp = language_probability

    def transcribe(self, path: Path) -> Transcript:
        self.calls += 1
        return fake_transcript(60.0, self.lp)


def fake_transcript(duration: float, lp: float = 0.98) -> Transcript:
    segs = []
    t = 0.5
    n = 0
    while t + 5 < duration:
        words = []
        for i in range(10):
            n += 1
            txt = f"word{n}" + ("." if i == 9 else "")
            words.append(Word(t, t + 0.4, " " + txt, 0.95))
            t += 0.5
        segs.append(Segment(words[0].start, words[-1].end, " ".join(w.text.strip() for w in words), words))
        t += 0.5
    return Transcript("en", lp, duration, segs)


class FakeMessages:
    def __init__(self, responses: list):
        self.responses = responses
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0) if self.responses else {"candidates": []}
        text = r if isinstance(r, str) else json.dumps(r)
        return SimpleNamespace(
            stop_reason="end_turn", model=kw["model"],
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=3000, output_tokens=800, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
        )


class FakeAnthropic:
    def __init__(self, responses: list):
        self.messages = FakeMessages(responses)
        self.beta = SimpleNamespace(messages=self.messages)


def good_candidates(t: Transcript, starts=(1.0, 22.0)) -> dict:
    out = []
    for s in starts:
        e = s + 18
        quote = t.text_between(s, e).split(" ")[2:6]
        out.append({
            "start_s": s, "end_s": e, "hook": "A clear hook", "payoff": "A complete payoff",
            "title": "Title", "caption": "Caption text", "hashtags": ["podcast"],
            "evidence_quote": " ".join(quote),
            "scores": {"clarity": 4, "context": 4, "payoff": 4, "distinctness": 3, "campaign_fit": 4},
            "context_risk": False, "notes": "",
        })
    return {"candidates": out}


@dataclass
class FakeBuffer:
    """GraphQL stand-in for api.buffer.com."""
    mode: str = "ok"          # ok | timeout_on_create | unauthorized
    posts: dict = field(default_factory=dict)
    sent_at: str | None = None
    calls: list = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        q = body["query"]
        self.calls.append(q.split("(")[0].strip())
        if self.mode == "unauthorized":
            return httpx.Response(401, json={"errors": [{"message": "bad token"}]})
        if "createPost" in q:
            if self.mode == "timeout_on_create":
                raise httpx.ReadTimeout("timed out", request=request)
            pid = f"p{len(self.posts) + 1}"
            self.posts[pid] = body["variables"]["input"]
            return httpx.Response(200, json={"data": {"createPost": {"post": {"id": pid, "status": "sending"}}}})
        if "GetPost" in q:
            pid = body["variables"]["id"]
            return httpx.Response(200, json={"data": {"post": {
                "id": pid, "status": "sent", "sentAt": self.sent_at, "externalLink": f"https://instagram.com/reel/{pid}",
                "dueAt": None, "text": self.posts.get(pid, {}).get("text")}}})
        if "RecentPosts" in q:
            return httpx.Response(200, json={"data": {"posts": {"edges": []}}})
        return httpx.Response(400, json={"errors": [{"message": "unknown op"}]})


# ---------------------------------------------------------------- fixtures --
@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=60",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out),
    ], check=True)
    return out


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc))


@pytest.fixture
def ctx(tmp_path, clock, sample_video):
    settings = Settings(
        mode="dry_run", min_free_disk_gb=0, min_free_disk_fraction=0, calibration_review_count=1,
        source_host_allowlist=["media.example.com", "feed.example.com"], max_posts_per_day_total=3,
    )
    env = Env(
        data_dir=tmp_path / "data", config_path=tmp_path / "config.yaml", public_base_url="https://clips.example.com",
        anthropic_api_key=None, buffer_api_key="buf-key", buffer_api_url="https://api.buffer.com",
        buffer_organization_id="org1", instagram_access_token=None, instagram_user_id="1784000",
        instagram_graph_version="v23.0", telegram_bot_token=None, telegram_chat_id="42",
        owner_password_hash=None, session_secret="test-secret", backup_key=None, cookie_secure=False,
    )
    ensure_dirs(env)
    conn = connect(env.db_path)
    migrate(conn)
    transport = RecordingTransport()
    fake_buffer = FakeBuffer()
    video_bytes = sample_video.read_bytes()

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.buffer.com":
            return fake_buffer.handler(request)
        if request.url.host == "media.example.com":
            return httpx.Response(200, content=video_bytes, headers={"content-type": "video/mp4"})
        if request.url.host == "feed.example.com":
            return httpx.Response(200, json=route.feed)
        return httpx.Response(404)

    route.feed = {"videos": []}
    http = httpx.Client(transport=httpx.MockTransport(route))
    c = Ctx(settings=settings, env=env, conn=conn, alerter=Alerter(transport), http=http, clock=clock,
            resolve_dns=False)
    c.publishers = build_publishers(c)
    c.tg = FakeTelegramAPI()
    c.editor = Editor(FakeAnthropic([]), settings)
    c.transcriber = FakeTranscriber()
    c.alerts = transport          # test handles
    c.fake_buffer = fake_buffer
    c.route = route
    yield c
    conn.close()


def make_opportunity(ctx, opp_id="client-a", **over):
    from clipper import opportunities

    vals = dict(
        name=opp_id, revenue_route="client", permission_evidence="agreement-001", terms_text="Client agreement text",
        publisher="buffer", account_ref="chan1", platform="instagram", min_duration_s=10, max_duration_s=40,
        payment={"model": "client_fee", "currency": "USD", "rate": "50"}, posting_times=["10:00"],
    )
    vals.update(over)
    opportunities.create(ctx.conn, ctx.now(), opp_id, vals)
    opportunities.approve_terms(ctx.conn, ctx.now(), opp_id)
    opportunities.set_status(ctx.conn, ctx.now(), opp_id, "active")
    return opportunities.get(ctx.conn, opp_id)


def drain(ctx, queue="heavy", limit=50):
    from clipper.jobs.runner import run_one

    n = 0
    while n < limit and run_one(ctx, queue, "test", 600):
        n += 1
    return n
