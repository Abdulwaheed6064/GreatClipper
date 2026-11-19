"""Process-wide dependencies, passed explicitly so tests can swap any of them."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx

from clipper.alerts import Alerter, NullTransport, TelegramTransport
from clipper.config import Env, Settings, ensure_dirs, load_env, load_settings
from clipper.db import connect, migrate, utcnow


@dataclass
class Ctx:
    settings: Settings
    env: Env
    conn: sqlite3.Connection
    alerter: Alerter
    http: httpx.Client
    publishers: dict[str, Any] = field(default_factory=dict)
    editor: Any = None          # clipper.editorial.claude.Editor
    transcriber: Any = None     # clipper.transcription.whisper.Transcriber
    clock: Callable[[], datetime] = utcnow
    resolve_dns: bool = True

    def now(self) -> datetime:
        return self.clock()


def build_ctx(*, settings: Settings | None = None, env: Env | None = None) -> Ctx:
    from clipper.editorial.claude import Editor
    from clipper.publishing.registry import build_publishers
    from clipper.transcription.whisper import WhisperTranscriber

    settings = settings or load_settings()
    env = env or load_env()
    ensure_dirs(env)
    conn = connect(env.db_path)
    migrate(conn)
    if env.telegram_bot_token and env.telegram_chat_id:
        transport = TelegramTransport(env.telegram_bot_token, env.telegram_chat_id)
    else:
        transport = NullTransport()
    http = httpx.Client(timeout=httpx.Timeout(60, connect=15), headers={"User-Agent": "GreatClipper/1.0"})
    ctx = Ctx(settings=settings, env=env, conn=conn, alerter=Alerter(transport), http=http)
    ctx.publishers = build_publishers(ctx)
    ctx.editor = Editor.from_env(env, settings)
    ctx.transcriber = WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type)
    return ctx
