# Operations guide

## Processes

| Service | Command | Role |
|---|---|---|
| `web` | `python -m clipper web` | Dashboard + public `/m/<token>.mp4` clip URLs |
| `scheduler` | `python -m clipper scheduler` | Exactly one. Feed polling (30 min), publish slots, status checks, deadline reminders, hourly housekeeping, daily backup. Reconciles outstanding publications on start before anything new. |
| `worker` | `python -m clipper worker` | Heavy jobs one at a time: download → transcribe → Claude selection → render → checks |
| `caddy` | — | HTTPS in front of `web` |

State lives in `data/` (SQLite in WAL mode, media, backups, Whisper model cache). Never copy `data/clipper.db` while running; use `python -m clipper backup`.

## Lifecycle you will see

* **Source:** `discovered → acquired → transcribed → selected → done`, or `held` with a reason (rights missing/expired, disallowed host, layout changed, poor audio, no usable candidates, daily minutes cap).
* **Clip:** `selected → rendered → checked → ready → published`, or `held` / `rejected`. `checked` with "awaiting review" means it is in your review queue.
* **Publication:** `reserved → sending → accepted → sent`; `failed` (provably not sent); `unknown` (maybe sent — blocks all new publishing until you resolve it).
* **Submission (campaigns):** `manual_required → submitted` / `submitted_late` / `overdue` / `missed`; separate review state `pending / approved / rejected`.
* **Money:** ledger rows are separate event types — estimated gross, pending, approved, settled, fee, withdrawal (not revenue), operating cost, fixed commitment, adjustment.

## Routine

**Client / owned route (Mode A):** posts go out at the opportunity's posting times (local timezone) from the ready queue — at most `max_posts_per_day_total` across everything. Weekly: review held clips, publication problems, ledger, spend.

**Campaign route (Mode B), each session (≈5–15 min target, measure it):**
1. Open the campaign page on Content Rewards. Check current terms, admission, remaining budget, disclosure rules.
2. Dashboard → **Campaign session**. Tick the four confirmations, type the remaining budget you see, pick a ready clip, **Ready — publish now**. Readiness expires after 5 minutes if publishing has not started.
3. Wait on **Submissions**. When the platform confirms, the live URL + deadline (counted from the real publish time) appear there and in Telegram.
4. Submit the URL on Content Rewards. Paste the confirmation into **I submitted it**. Aim for < 10 minutes.
5. Reminders at 5 and 15 min, escalation at 20, overdue at the deadline. Never delete/re-upload/reassign a post to recover a missed deadline — record it as missed.
6. The next campaign clip unlocks only after the previous submission is recorded.

Log your time on **Ledger → Log operator time** so profit per operator hour is real.

**Weekly review:** acceptance/rejection reasons, settled revenue per source hour, qualified-view yield, spend per accepted clip, deadline misses, time spent. Import official numbers via CSV (examples in `examples/`). After ~20 outcomes with completed earning windows, compare campaigns at similar ages using medians. Raise cadence 1 → 2 → 3/day only when reliability, quality and net contribution support it.

## Spend controls

* Add your VPS bill each month as a `fixed_commitment` ledger row.
* Every Claude call reserves its worst-case cost first. 80 % of the ceiling → alert; 90 % → optional work (e.g. terms drafting) stops; a reservation that would exceed the ceiling or the model allowance stops new paid work.
* Also set a spend limit in the Anthropic console — the app counter is not a billing guarantee.
* Buffer Free: ~1,000 requests/channel/30 days. The app counts every call and stops new posts when only the reserve (310) remains; status checks continue.

## Ambiguous publications (`unknown`)

Happens on a timeout after the request may have reached the provider, after a crash mid-call, or when no live URL appears after six status checks. **All new publishing stops.** Open the real account:
* Post is live → Publications → *Resolve manually* → paste URL (and time if known) → **It is live**. A campaign submission task opens with a conservative deadline.
* Not there → **It was NOT published**. The clip can be published again later.
Never re-post blindly.

## Backups

* Daily encrypted backup (Fernet, `BACKUP_KEY`) into `data/backups`, 14 kept. Set `BACKUP_RCLONE_REMOTE` + install rclone, or sync that folder off the server yourself. **Store `BACKUP_KEY` somewhere other than the server.**
* Restore: stop all services, `docker compose run --rm web restore /data/backups/<file>`, start again. The system comes back **paused**; outstanding sends are reconciled, not re-sent. Check the dashboard, then **Resume**.

## Disk

Ingest pauses below `min_free_disk_gb` / `min_free_disk_fraction`. Raw sources and intermediates are deleted after `raw_retention_days`; final renders of submitted campaign clips are kept for `submission_retention_days`. Terms snapshots, hashes, ledger and receipts stay in the database.

## Credentials

* Expired/revoked Buffer or Instagram credentials fail the affected publication (provably not sent), hold that clip and alert you; other routes keep working. Update `.env` and restart.
* Rotate `SESSION_SECRET` to log out all sessions.

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing processes | Opportunity active? Terms confirmed for the current version? Source host in `source_host_allowlist`? |
| Clips stay in review | `calibration_review_count` not reached yet, or context-risk flag — review them on the Review page |
| Campaign publish refused | The reason list on the flash message: alerts unhealthy, open submission, stale budget, unverified disclosure capability |
| Feed errors | Dashboard alerts; feed must be HTTPS JSON with a `videos` list |
| Jobs failing | Jobs page shows error class: `transient` retries with backoff, `permanent`/`hold` needs you |
