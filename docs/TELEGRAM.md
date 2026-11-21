# Running GreatClipper from Telegram

After the one-time setup, you control everything from a Telegram chat with your own bot:

| You do | The bot does |
|---|---|
| Send a Content Rewards campaign link + its rules (text, screenshots or PDF) | Reads them with Claude, says whether the campaign is worth it, shows the exact settings |
| Tap **✅ Approve & start** | Creates the campaign, confirms the terms, switches it on |
| Send the campaign's video links (Drive / Dropbox / direct) | Downloads, transcribes, picks moments, edits captioned vertical clips, checks them |
| Tap **✅ Approve** on the first clip videos | Releases them to the ready queue; after the first 10 per campaign, clips that pass every check are released without asking (flagged ones still ask) |
| When asked "Ready to post?", check the campaign page and tap **✅ Checked — post now** | Posts the clip, sends the live link with a 30-minute countdown |
| Submit the link on Content Rewards, tap **✅ I submitted it**, paste the confirmation or a screenshot | Records it and unlocks the next post |

Content Rewards has no API, so joining campaigns and submitting each link stay with you (about a minute per post). Everything else runs by itself.

## One-time setup (about 20 minutes)

### 1. Create the bot
1. In Telegram, open **@BotFather** → `/newbot` → pick a name and a username ending in `bot`.
2. Copy the token it gives you into `.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`
3. Open your new bot in Telegram and press **Start**.
4. Find your chat id:
   ```bash
   docker compose run --rm web telegram-chat-id
   ```
   Send the bot any message while it waits, then copy the printed `TELEGRAM_CHAT_ID=...` into `.env`.

Only that chat can control the bot; messages from anyone else are ignored.

### 2. Fill in the rest of `.env`
- `ANTHROPIC_API_KEY` — lets the bot read campaigns and pick clips.
- `INSTAGRAM_ACCESS_TOKEN` + `INSTAGRAM_USER_ID` — the Instagram **professional** account clips are posted to.
- `CLIPPER_PUBLIC_BASE_URL` + `CLIPPER_DOMAIN` — your server's HTTPS address (Instagram downloads the clip from there).
- `SESSION_SECRET`, `BACKUP_KEY` (`docker compose run --rm web new-secret`), `OWNER_PASSWORD_HASH` (`docker compose run --rm web hash-password`).

### 3. Start it
```bash
docker compose up -d --build
```
This starts five services: `web` (dashboard), `scheduler`, `worker`, `bot`, `caddy` (HTTPS).

### 4. Say hello
Send `/start` to the bot. It confirms alerts work and shows the **setup checklist** with the next step. Send `/setup` any time to see it again.

### 5. Add your first campaign
1. Join a campaign on Content Rewards (the bot can't do this for you).
2. Send the bot the campaign link.
3. Paste the rules / brief, or send screenshots or the PDF. Include: payout, rules, required tags and mentions, length, and where the footage comes from.
4. Send `/done`. After about a minute you get the assessment and settings.
5. Wrong or missing something? Tap **✏️ Correct / answer** and type it in plain words ("max 45 s, must tag @brand").
6. Tap **✅ Approve & start**, then send the campaign's video links and `/done`.

### 6. First live test (once)
Everything starts in **dry run** (posts are simulated, links look like `dry-run.invalid`). When the first clips look good:
1. Set `mode: live` in `config.yaml`, then `docker compose restart`.
2. `/golive` → tap the campaign → **Yes, go live**.
3. When the bot asks, post one clip. Check it on Instagram: is it there, correct caption, disclosure visible?
4. Record what you saw on the dashboard → **Capabilities** (`instagram.reels_publish`, `instagram.permalink`). Until those are verified, real posting stays blocked, and the bot tells you why.

If a campaign needs Instagram's **Paid partnership** label, that also needs a one-time test (see `docs/CAPABILITY_CHECKLIST.md`). Until then those campaigns can't post automatically.

## Commands

`/start` · `/setup` · `/new` · `/done` · `/cancel` · `/videos` · `/campaigns` · `/post` · `/status` · `/golive` · `/pause` · `/resume` · `/help`

## Good to know
- The bot only asks "Ready to post?" between `telegram_prompt_window` hours (default 09:00–22:00 local). If you tap **Later**, it asks again after 2 hours. `/post` asks right away.
- One campaign post at a time: the next one waits until you've recorded the previous submission.
- Video links from YouTube, TikTok, Instagram, Twitch or Kick are refused. A public post doesn't give you the right to reuse it; use the files the campaign provides.
- Google Drive links must be shared as "Anyone with the link". Send the link to a **file**, not a folder.
- The first time you send a link from a new site, the bot asks you to allow downloads from it.
- Telegram limits bots to 20 MB for files you send *to* the bot, which is why videos go in as links, not uploads.
