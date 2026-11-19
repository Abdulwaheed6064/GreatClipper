# Assumptions and decisions

Built from the *Four-Day MVP Implementation Plan* (18 Nov 2026) and the *Due Diligence* research report. Owner decisions taken before the build:

| Decision | Choice |
|---|---|
| Publishers | One publisher interface with **Buffer** (client/owned default) and **direct Instagram Graph API** (for campaigns that need native paid-partnership labels), per the research report's correction |
| Alerts | **Telegram bot** |
| Deployment | **Docker Compose** (same stack on a Windows PC with Docker Desktop and on a Linux VPS) + Caddy HTTPS |
| Clip hosting | **Served by the app** at opaque, unlisted `/m/<random>.mp4` URLs; private source footage is never public |

## Assumptions made while building

1. **Claude model:** default `claude-opus-5-5` at `effort: medium`, using structured JSON output and server-side refusal fallback. A 30-min transcript is roughly $0.10–0.15 per source at list price, within the $3–10/month allowance at 1–3 clips/day. Switch to `claude-haiku-5-5` in `config.yaml` (and update `claude_pricing`) if you want it cheaper.
2. **Transcript-first editorial only.** The optional candidate-frame (vision) review for silent gameplay action is **not built**. It was first in the plan's cut order. Silent moments can be missed.
3. **One render template** (1080×1920, H.264/AAC, 30 fps, captions in the lower safe area, hook text for 3 s at the top). Three layouts: `blur_pad` (default for landscape podcasts), `center_crop`, and `gameplay_facecam` (fixed crops calibrated once). There is no automatic speaker tracking.
4. **Original audio only**, loudness-normalised to −14 LUFS. No music is added.
5. **Single owner**: one password and no user accounts. A session cookie lasts 12 h. CSRF tokens protect every form.
6. **Calibration**: the first 10 clips per opportunity and template need your approval (`calibration_review_count`). After that, clips that pass every gate are released automatically. Clips the editor flags as context risk always need review.
7. **Daily cap is global** across all opportunities (`max_posts_per_day_total`, default 1). Dry-run posts count toward it so the cadence is realistic.
8. **Campaign budget freshness**: an observed remaining budget older than 30 minutes blocks a campaign publish. You enter it in each attended session.
9. **Submission deadline** defaults to 30 minutes after the actual publish time when the campaign's payment terms don't state one.
10. **Money**: stored as integer cents with a currency. No FX conversion is built, so currencies are reported separately. Shared fixed costs show as "(shared)".
11. **Unknown ≠ zero**: blank imported views and missing payment fields stay unknown. The CPM estimator then shows a range or withholds the estimate.

## Not verified while building (do this on Day 1)

| Item | Why | What to do |
|---|---|---|
| **Buffer API operation/field names** (`createPost`, `schedulingType: automatic`, `mode: shareNow`, `assets.videos`, `post { status sentAt externalLink }`, `posts(...)`) | Buffer's developer docs were not reachable from the build environment. The adapter follows Buffer's public API documentation as it was known. | Run `python -m clipper check-buffer`, then do one live test post. Fix the GraphQL strings in `clipper/publishing/buffer.py` if anything differs. |
| **Buffer video posts to Instagram may need platform metadata** (e.g. Reel type) | Not confirmed | If the test post fails or posts the wrong type, set `buffer_platform_metadata` in `config.yaml` from Buffer's docs |
| **Meta branded-content (paid partnership) parameter names** | Not confirmed against Meta's current docs; deliberately **not hard-coded** | Copy them into `instagram_paid_partnership_params`, then test live. Until that's done, campaigns that require the native label are blocked. |
| **Instagram Graph API version** | Changes over time | Set `INSTAGRAM_GRAPH_VERSION` to the current version |
| **faster-whisper speed/RAM on your VPS** | Tests use a fake transcriber; no model download here | Run `python -m clipper benchmark` on the real host |
| **Docker image build** | The build sandbox could not reach Debian package mirrors. `docker compose config` validates, and the app and tests run on Python 3.13 with FFmpeg 6.1. | Run `docker compose build` on your machine |
| **Claude live call** | No API key in the build environment; the request shape follows the current SDK (`anthropic==2.10.0`) and is checked by tests | Watch the first real selection in the Jobs page and Spend panel |

## Deliberately not built (deferred by the plan)

Live capture, scraping of YouTube/Twitch/Kick, automatic campaign enrollment, any Content Rewards automation (no API exists), automatic invoicing/withdrawals, multi-user SaaS, TikTok direct posting, Redis/Celery/Kubernetes/PostgreSQL, model training, sales outreach, three-platform launch.
