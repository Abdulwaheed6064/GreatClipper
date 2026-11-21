"""Command line: ``python -m clipper <command>``."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import resource
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create/upgrade the database")
    w = sub.add_parser("web", help="run the dashboard")
    w.add_argument("--host", default="0.0.0.0")
    w.add_argument("--port", type=int, default=8000)
    sub.add_parser("scheduler", help="run the scheduler (light jobs, publishing, deadlines)")
    sub.add_parser("worker", help="run the heavy worker (download/transcribe/select/render)")
    sub.add_parser("bot", help="run the two-way Telegram control bot")
    sub.add_parser("telegram-chat-id", help="show the chat id of whoever messaged the bot (setup helper)")
    sub.add_parser("hash-password", help="print an OWNER_PASSWORD_HASH value")
    sub.add_parser("new-secret", help="print random SESSION_SECRET and BACKUP_KEY values")
    sub.add_parser("backup", help="write an encrypted backup now")
    r = sub.add_parser("restore", help="restore an encrypted backup (stop all services first)")
    r.add_argument("file")
    sub.add_parser("check-buffer", help="introspect the live Buffer schema against the adapter")
    b = sub.add_parser("benchmark", help="capacity test: transcribe + render clips from a local file")
    b.add_argument("file")
    b.add_argument("--clips", type=int, default=3)
    b.add_argument("--clip-seconds", type=float, default=40)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "hash-password":
        from clipper.web.auth import hash_password

        pw = getpass.getpass("Owner password: ")
        if len(pw) < 12:
            print("Use at least 12 characters.", file=sys.stderr)
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("Passwords differ.", file=sys.stderr)
            return 1
        # $ must be escaped as $$ inside docker-compose .env interpolation
        h = hash_password(pw)
        print(f"OWNER_PASSWORD_HASH='{h}'")
        return 0
    if args.cmd == "new-secret":
        import secrets

        from clipper.backup import new_key

        print(f"SESSION_SECRET={secrets.token_urlsafe(48)}")
        print(f"BACKUP_KEY={new_key()}")
        return 0
    if args.cmd == "telegram-chat-id":
        import os

        from clipper.telegram.api import TelegramAPI

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).", file=sys.stderr)
            return 1
        print("Now send any message to your bot in Telegram (waiting up to 60 s)…")
        api = TelegramAPI(token, "0")
        for upd in api.get_updates(None, timeout=60) or []:
            chat = (upd.get("message") or {}).get("chat") or {}
            if chat:
                print(f"TELEGRAM_CHAT_ID={chat['id']}   ({chat.get('first_name') or chat.get('title') or ''})")
                return 0
        print("No message received. Send /start to the bot and run this again.")
        return 1
    if args.cmd == "restore":
        from clipper.backup import restore
        from clipper.config import load_env

        en = load_env()
        if not en.backup_key:
            print("BACKUP_KEY is required", file=sys.stderr)
            return 1
        restore(Path(args.file), en.backup_key, en.db_path)
        print("Restored. The system is paused; review the dashboard before resuming.")
        return 0

    from clipper.context import build_ctx

    ctx = build_ctx()
    if args.cmd == "migrate":
        print("database ready at", ctx.env.db_path)
    elif args.cmd == "web":
        import uvicorn

        from clipper.web.app import create_app

        if not ctx.env.session_secret:
            print("SESSION_SECRET is not set (python -m clipper new-secret)", file=sys.stderr)
            return 1
        uvicorn.run(create_app(ctx), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    elif args.cmd == "scheduler":
        from clipper.jobs.runner import scheduler_loop

        scheduler_loop(ctx)
    elif args.cmd == "worker":
        from clipper.jobs.runner import worker_loop

        worker_loop(ctx)
    elif args.cmd == "bot":
        from clipper.telegram.bot import run_bot

        run_bot(ctx)
    elif args.cmd == "backup":
        from clipper.backup import make_backup

        print(make_backup(ctx) or "backup not written (see alerts)")
    elif args.cmd == "check-buffer":
        missing = ctx.publishers["buffer"].introspect()
        print("OK: all fields used by the adapter exist" if not missing else f"MISSING: {json.dumps(missing)}")
        return 0 if not missing else 2
    elif args.cmd == "benchmark":
        return benchmark(ctx, Path(args.file), args.clips, args.clip_seconds)
    return 0


def benchmark(ctx, path: Path, n: int, seconds: float) -> int:
    """Section 10 capacity test. Pilot acceptance: a 30-min source + 3 clips in
    under 2 hours and below 80% RAM on the target host."""
    from clipper.rendering import ffmpeg
    from clipper.rules.checks import media_checks

    t0 = time.monotonic()
    pr = ffmpeg.probe(path)
    print(f"source: {pr.width}x{pr.height} {pr.duration/60:.1f} min")
    t = ctx.transcriber.transcribe(path)
    t1 = time.monotonic()
    print(f"transcription ({ctx.transcriber.model_name}): {t1 - t0:.0f}s for {pr.duration:.0f}s of media "
          f"(x{pr.duration / max(1, t1 - t0):.2f} realtime), language={t.language} p={t.language_probability:.2f}")
    work = ctx.env.work_dir / "benchmark"
    work.mkdir(parents=True, exist_ok=True)
    step = max(seconds, (pr.duration - seconds) / max(1, n))
    for i in range(n):
        start = min(i * step, max(0, pr.duration - seconds))
        ass = work / f"b{i}.ass"
        ffmpeg.write_ass(ass, ffmpeg.caption_events(t, start, start + seconds), "Benchmark clip", seconds)
        out = work / f"b{i}.mp4"
        r0 = time.monotonic()
        ffmpeg.render(path, out, start, start + seconds, {"template": "blur_pad"}, ass)
        res = media_checks(out, seconds, ass)
        bad = [k for k, v in res.items() if not v["ok"]]
        print(f"clip {i + 1}: render {time.monotonic() - r0:.0f}s, checks {'OK' if not bad else 'FAILED ' + str(bad)}")
    total = time.monotonic() - t0
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 + \
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    try:
        mem_total = int(next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]) / 1024
        ram = f"{peak_mb:.0f} MB peak ({peak_mb / mem_total:.0%} of {mem_total:.0f} MB)"
    except (OSError, StopIteration):
        ram = f"{peak_mb:.0f} MB peak"
    print(f"TOTAL {total/60:.1f} min · {ram}")
    print("Pilot target: 30-min source + 3 clips < 120 min and < 80% RAM.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
