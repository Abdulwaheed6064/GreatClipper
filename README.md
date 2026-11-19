# GreatClipper

A small, single-owner Python application that turns **authorized** recordings into captioned vertical clips, publishes them through one supported integration, and records costs and earnings. It implements the *Automated Clipping Business — Four-Day MVP Implementation Plan* (18 Nov 2026).

> **Status:** built and tested against mocked external services. **Not production-ready** until the Day-1 live checks in [docs/CAPABILITY_CHECKLIST.md](docs/CAPABILITY_CHECKLIST.md) pass on your real accounts. It is not "fully automated": Content Rewards campaigns require an attended session and a manual link submission, by design.

## What it does

```
authorized feed / manual link ─► download (allowlist, size cap, hash)
   ─► faster-whisper CPU transcript (word timing, VAD)   ─► held if poor audio / wrong language
   ─► Claude: up to 6 candidates (structured JSON, 1 repair max) ─► server-side validation, ranking
   ─► FFmpeg 1080×1920 template + burned-in captions      ─► technical + duplicate checks
   ─► review (first N clips) / release gates               ─► READY queue (opaque public URL)
        ├─ client / owned route:  local schedule ─► Buffer or Instagram ─► live URL
        └─ campaign route:        attended session ─► publish one ─► live URL + deadline
                                   ─► YOU submit on Content Rewards ─► record receipt
   ─► ledger (manual / CSV), profitability, budget ceiling, Telegram alerts
```

| Module | Purpose |
|---|---|
| `clipper/sources` | creator JSON feed + manual links, SSRF-safe downloads |
| `clipper/transcription` | faster-whisper (CPU, int8) |
| `clipper/editorial` | bounded Claude calls, validation, ranking, cache |
| `clipper/rendering` | FFmpeg layouts (`blur_pad`, `center_crop`, `gameplay_facecam`), ASS captions |
| `clipper/rules` | permission/terms/release/publish gates, media + duplicate checks |
| `clipper/publishing` | Buffer adapter, Instagram Graph adapter, dry-run adapter, publish state machine |
| `clipper/submissions` | manual Content Rewards submission tasks + deadline reminders |
| `clipper/ledger` | ledger, idempotent CSV import, CPM estimator, profitability |
| `clipper/budget` | spend reservations against the monthly ceiling |
| `clipper/jobs` | durable SQLite job queue, scheduler + heavy worker |
| `clipper/web` | authenticated dashboard (server-rendered, CSRF) |

## Quick start (your PC, dry run)

Requirements: Docker Desktop (Windows/macOS) or Docker Engine (Linux).

```bash
git clone <this repo> greatclipper && cd greatclipper
cp config.example.yaml config.yaml
cp .env.example .env
docker compose build
docker compose run --rm web new-secret        # paste SESSION_SECRET and BACKUP_KEY into .env
docker compose run --rm web hash-password     # paste OWNER_PASSWORD_HASH (keep the single quotes)
```

For local testing set in `.env`: `CLIPPER_DOMAIN=localhost`, `CLIPPER_PUBLIC_BASE_URL=https://localhost`, then:

```bash
docker compose up -d
```

Open https://localhost (accept the local certificate). Everything starts in **dry run**: the whole pipeline runs, but publishing is simulated (`https://dry-run.invalid/...` URLs). Real publishing needs a public HTTPS domain, because Buffer/Meta must fetch the clip file from it.

Without Docker (Linux/macOS, Python 3.11+, ffmpeg installed):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export $(grep -v '^#' .env | xargs)   # or use your own env loader
python -m clipper migrate
python -m clipper web & python -m clipper scheduler & python -m clipper worker
```

## Going live (VPS)

1. A small x86 VPS (start ~2 vCPU / 4 GB), a domain pointing at it, ports 80/443 open.
2. Fill `.env` (domain, keys, Telegram). Run `docker compose up -d --build`.
3. Run the capacity test on the real host: `docker compose run --rm worker benchmark /data/sample-30min.mp4`. Pass target: 30-min source + 3 clips in < 2 h and < 80 % RAM. If it fails, use `whisper_model: base` or lower the daily source minutes before upgrading the server.
4. Dashboard → **Send test alert** (must arrive on your phone).
5. Create the opportunity, paste the full terms, fill rights and payment fields, **confirm terms**, set it **active**.
6. Work through [docs/CAPABILITY_CHECKLIST.md](docs/CAPABILITY_CHECKLIST.md): one authorized live test post per destination, record the evidence.
7. Set `mode: live` in `config.yaml`, restart, and press **Enable live publishing** on that opportunity (you must type its id).

Operating details (daily routine, campaign sessions, backups, restore, troubleshooting): [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

36 tests cover the section-14 acceptance list with mocked Buffer/Claude/Telegram and a real FFmpeg render: feed de-duplication, rights/terms gating, malformed model output, changed layout, budget stop, ambiguous-send reconciliation (no duplicate), restart recovery, deadline from actual publish time, reminders/overdue, readiness expiry, credential failure isolation, idempotent imports, currency separation, backup/restore, auth/CSRF.

## Design decisions & assumptions

See [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) — including what could **not** be verified while building (Buffer schema, Meta branded-content parameter names, real faster-whisper speed on your host).
