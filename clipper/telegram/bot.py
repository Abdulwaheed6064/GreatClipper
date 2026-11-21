"""Two-way Telegram control bot (owner chat only).

Run: ``python -m clipper bot`` (its own process / compose service).

Flows
-----
* Send a Content Rewards campaign link (or /new) → paste rules, send
  screenshots/PDFs → /done → Claude assessment + proposed settings →
  ✅ Approve / ✏️ Correct / ❌ Skip.
* After approval: send the campaign's video links (Drive / Dropbox / direct).
* Clips needing review arrive as videos with Approve / Reject.
* "Ready to post?" prompts → ✅ Checked — post now → live link + deadline →
  ✅ I submitted it → send the confirmation.

Everything from other chats is ignored. Messages and files from the owner
are still treated as data when passed to Claude.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from clipper import opportunities
from clipper.alerts import alerts_healthy
from clipper.budget import spend
from clipper.context import Ctx
from clipper.db import audit, iso, kv_get, kv_set, row, rows, tx
from clipper.ledger import ledger
from clipper.rules.gates import capability, is_live
from clipper.sources.feed import add_manual
from clipper.sources.links import (LinkRefused, allowlist, approve_hosts, find_urls, hosts_to_allow,
                                   is_campaign_link, normalize_source_url, provider_id_for)
from clipper.sources.safe_http import host_allowed
from clipper.telegram import intake as intake_mod
from clipper.telegram import prompts
from clipper.telegram.api import TelegramAPI, TelegramError

log = logging.getLogger("clipper.bot")

COMMANDS = [
    ("start", "What this bot does + first-time setup"),
    ("setup", "Setup checklist: what is done, what is next"),
    ("new", "Add a campaign (send link, rules, screenshots)"),
    ("done", "Finish the current step"),
    ("cancel", "Cancel the current step"),
    ("videos", "Add source videos to a campaign"),
    ("campaigns", "List campaigns, pause/resume"),
    ("post", "Ask now to post a ready clip"),
    ("status", "Pipeline status"),
    ("golive", "Switch a campaign to real posting"),
    ("pause", "Pause all publishing"),
    ("resume", "Resume publishing"),
    ("help", "How it works"),
]

HELP = """How it works:

1️⃣ Find a campaign on Content Rewards and join it.
2️⃣ Send me its link, then paste the rules (or send screenshots/PDF of the brief). Send /done.
3️⃣ I read it, tell you if it's worth it and show the settings. Tap ✅ Approve.
4️⃣ Send me the campaign's video links (Drive/Dropbox/direct). /done.
5️⃣ I transcribe, pick moments, edit vertical clips with captions and check them. The first clips come here for you to approve.
6️⃣ When a clip is ready I ask "Ready to post?". Check the campaign page, tap ✅.
7️⃣ I post it and send the link with a 30-min countdown. Submit it on Content Rewards, tap ✅ I submitted it, and paste the confirmation.

Content Rewards has no API, so step 7 stays yours: about a minute per post.
/setup shows what is still missing."""


# ------------------------------------------------------------------- state --
def state(ctx: Ctx) -> dict:
    return kv_get(ctx.conn, "tg_state", {"mode": "idle"}) or {"mode": "idle"}


def set_state(ctx: Ctx, **st) -> None:
    with tx(ctx.conn):
        kv_set(ctx.conn, "tg_state", st or {"mode": "idle"})


class Bot:
    def __init__(self, ctx: Ctx, api: TelegramAPI):
        self.ctx = ctx
        self.api = api

    # ---------------------------------------------------------------- io --
    def say(self, text: str, buttons=None) -> dict | None:
        try:
            return self.api.send_message(text, buttons)
        except TelegramError as exc:
            log.warning("send failed: %s", exc)
            return None

    def owner(self, chat_id) -> bool:
        return str(chat_id) == str(self.ctx.env.telegram_chat_id)

    # ------------------------------------------------------------ dispatch --
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat = (cq.get("message") or {}).get("chat", {}).get("id")
            if not self.owner(chat) or not self.owner(cq.get("from", {}).get("id", chat)):
                return
            self.api.answer_callback(cq["id"])
            self.on_button(cq.get("data", ""), (cq.get("message") or {}).get("message_id"))
            return
        msg = upd.get("message")
        if not msg or not self.owner(msg.get("chat", {}).get("id")):
            if msg:
                log.info("ignored message from chat %s", msg.get("chat", {}).get("id"))
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text.startswith("/"):
            cmd = text.split()[0][1:].split("@")[0].lower()
            arg = text[len(text.split()[0]):].strip()
            fn = getattr(self, f"cmd_{cmd}", None)
            if fn:
                fn(arg)
                return
        mode = state(self.ctx).get("mode", "idle")
        getattr(self, f"in_{mode}", self.in_idle)(msg, text)

    # ------------------------------------------------------------ commands --
    def cmd_start(self, arg: str) -> None:
        ok = self.ctx.alerter.send(self.ctx.conn, self.ctx.now(), "test", "Connected ✅ — alerts reach this chat.")
        if ok:
            with tx(self.ctx.conn):
                self.ctx.conn.execute(
                    "INSERT INTO capabilities(key, status, evidence, verified_at, updated_at) VALUES "
                    "('alerts.telegram','verified','owner chat received /start reply',?,?) ON CONFLICT(key) DO UPDATE "
                    "SET status='verified', evidence=excluded.evidence, verified_at=excluded.verified_at, "
                    "updated_at=excluded.updated_at", (iso(self.ctx.now()), iso(self.ctx.now())))
        self.say("👋 I'm your GreatClipper bot.\n\n" + HELP)
        self.cmd_setup("")

    def cmd_help(self, arg: str) -> None:
        self.say(HELP)

    def cmd_cancel(self, arg: str) -> None:
        set_state(self.ctx, mode="idle")
        self.say("Cancelled. Send a campaign link or /help any time.")

    def cmd_pause(self, arg: str) -> None:
        with tx(self.ctx.conn):
            kv_set(self.ctx.conn, "paused", True)
            audit(self.ctx.conn, "owner:telegram", "system.pause", {"paused": True})
        self.say("⏸ Paused. Nothing new will be published. /resume to continue.")

    def cmd_resume(self, arg: str) -> None:
        with tx(self.ctx.conn):
            kv_set(self.ctx.conn, "paused", False)
            audit(self.ctx.conn, "owner:telegram", "system.pause", {"paused": False})
        self.say("▶️ Resumed.")

    def cmd_setup(self, arg: str) -> None:
        c, en, st = self.ctx.conn, self.ctx.env, self.ctx.settings

        def mark(ok: bool) -> str:
            return "✅" if ok else "⬜"

        healthy = alerts_healthy(c)[0]
        pub_ok = bool(en.instagram_access_token and en.instagram_user_id) or bool(en.buffer_api_key)
        public = en.public_base_url.startswith("https://") and "localhost" not in en.public_base_url
        caps_ok = all(capability(c, k) == "verified" for k in ("instagram.reels_publish", "instagram.permalink")) \
            if (st.campaign_defaults or {}).get("publisher", "instagram") == "instagram" else \
            all(capability(c, k) == "verified" for k in ("buffer.automatic_publish", "buffer.external_link"))
        camps = [o for o in opportunities.list_all(c) if o["revenue_route"] == "campaign" and o["status"] == "active"]
        lines = ["Setup checklist", "",
                 f"{mark(healthy)} Telegram alerts working (send /start)",
                 f"{mark(bool(en.anthropic_api_key))} Claude API key (ANTHROPIC_API_KEY in .env)",
                 f"{mark(pub_ok)} Posting account connected (INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_USER_ID in .env)",
                 f"{mark(public)} Public HTTPS address (CLIPPER_PUBLIC_BASE_URL) so Instagram can fetch clips",
                 f"{mark(bool(en.backup_key))} Backups encrypted (BACKUP_KEY)",
                 f"{mark(bool(camps))} At least one campaign added (send me a campaign link)",
                 f"{mark(caps_ok)} One live test post checked (dashboard → Capabilities)",
                 f"{mark(st.mode == 'live')} Real posting switched on (mode: live in config.yaml, then /golive)"]
        nxt = next((l for l in lines[2:] if l.startswith("⬜")), None)
        lines += ["", f"Next: {nxt[2:]}" if nxt else "All set. I'll ask you when a clip is ready to post."]
        if st.mode != "live":
            lines.append("\nRight now everything runs in DRY RUN: clips are made, posts are simulated.")
        self.say("\n".join(lines))

    def cmd_status(self, arg: str) -> None:
        c, st = self.ctx.conn, self.ctx.settings
        counts = {r["stage"]: r["n"] for r in rows(c, "SELECT stage, COUNT(*) n FROM clips GROUP BY stage")}
        srcs = {r["stage"]: r["n"] for r in rows(c, "SELECT stage, COUNT(*) n FROM sources GROUP BY stage")}
        subs = rows(c, "SELECT * FROM submissions WHERE status IN ('manual_required','overdue')")
        bs = spend.status(c, self.ctx.now(), st.monthly_operating_ceiling_usd, st.monthly_model_allowance_usd)
        lines = [f"Mode: {st.mode}{' · PAUSED' if kv_get(c, 'paused', False) else ''}",
                 f"Videos: {srcs.get('discovered', 0) + srcs.get('acquired', 0) + srcs.get('transcribed', 0)} in progress, "
                 f"{srcs.get('done', 0) + srcs.get('selected', 0)} processed, {srcs.get('held', 0)} on hold",
                 f"Clips: {counts.get('ready', 0)} ready · {counts.get('checked', 0)} awaiting review · "
                 f"{counts.get('held', 0)} held · {counts.get('published', 0)} published",
                 f"Posts today: {prompts.posts_today(self.ctx)} of {st.max_posts_per_day_total}",
                 f"Spend this month: ${bs.used_minor / 100:.2f} of ${bs.ceiling_minor / 100:.2f}"]
        for s in subs:
            lines.append(f"❗ Submission #{s['id']} waiting: {s['live_url']} (deadline {s['deadline_at'][11:16]} UTC)")
        self.say("\n".join(lines))
        for s in subs:
            self.say(f"Submission #{s['id']}", prompts.submission_buttons(s))

    def cmd_new(self, arg: str) -> None:
        urls = [u for u in find_urls(arg) if is_campaign_link(u)]
        it = intake_mod.new_intake(self.ctx, urls[0] if urls else None)
        set_state(self.ctx, mode="intake", intake=it["id"])
        self.say(("Campaign link saved. " if urls else "Send me the campaign link, then ") +
                 "paste the rules / brief text, or send screenshots or a PDF of them. "
                 "Include payout, rules, required tags and where the footage comes from.\n\nSend /done when finished.")

    def cmd_done(self, arg: str) -> None:
        st = state(self.ctx)
        if st.get("mode") == "intake":
            self.finish_intake(st["intake"])
        elif st.get("mode") == "sources":
            set_state(self.ctx, mode="idle")
            n = row(self.ctx.conn, "SELECT COUNT(*) n FROM sources WHERE opportunity_id=?", (st["opp"],))["n"]
            self.say(f"👍 {n} video(s) on file for this campaign. I'll message you when clips need review or are "
                     "ready to post.")
        else:
            self.say("Nothing in progress. Send a campaign link to start.")

    def cmd_videos(self, arg: str) -> None:
        camps = [o for o in opportunities.list_all(self.ctx.conn) if o["status"] in ("active", "paused")]
        if not camps:
            self.say("No campaigns yet. Send me a campaign link first.")
            return
        self.say("Which campaign are the videos for?",
                 [[(o["name"][:40], f"vs:{prompts.make_token(self.ctx, {'opp': o['id']})}")] for o in camps[:10]])

    def cmd_campaigns(self, arg: str) -> None:
        ops = [o for o in opportunities.list_all(self.ctx.conn) if o["status"] != "archived"]
        if not ops:
            self.say("No campaigns yet. Send me a campaign link.")
            return
        live = set(kv_get(self.ctx.conn, "live_opportunities", []) or [])
        for o in ops[:10]:
            ready = row(self.ctx.conn, "SELECT COUNT(*) n FROM clips WHERE opportunity_id=? AND stage='ready'",
                        (o["id"],))["n"]
            tok = prompts.make_token(self.ctx, {"opp": o["id"]})
            toggle = ("⏸ Pause", f"op:{tok}") if o["status"] == "active" else ("▶️ Resume", f"oa:{tok}")
            self.say(f"{o['name']} · {o['status']}{' · LIVE' if o['id'] in live else ''} · {ready} clip(s) ready",
                     [[toggle, ("➕ Add videos", f"vs:{tok}")]])

    def cmd_post(self, arg: str) -> None:
        busy = prompts.campaign_busy(self.ctx)
        if busy:
            self.say(f"Can't yet: {busy}.")
            return
        for o in opportunities.list_all(self.ctx.conn):
            if o["revenue_route"] == "campaign" and o["status"] == "active":
                clip = prompts.best_ready_clip(self.ctx, o)
                if clip:
                    prompts.send_post_prompt(self.ctx, o, clip)
                    return
        self.say("No campaign clip is ready yet. /status shows what is in progress.")

    def cmd_golive(self, arg: str) -> None:
        if self.ctx.settings.mode != "live":
            self.say("Real posting is off for the whole system (config.yaml has mode: dry_run). Finish /setup, set "
                     "mode: live, restart, then run /golive again.")
            return
        ops = [o for o in opportunities.list_all(self.ctx.conn) if o["status"] == "active"]
        live = set(kv_get(self.ctx.conn, "live_opportunities", []) or [])
        btns = [[(f"{'🟢' if o['id'] in live else '⚪️'} {o['name'][:35]}",
                  f"lv:{prompts.make_token(self.ctx, {'opp': o['id']})}")] for o in ops[:10]]
        self.say("Tap a campaign to switch real posting on/off for it:", btns or None)

    # -------------------------------------------------------------- modes --
    def in_idle(self, msg: dict, text: str) -> None:
        urls = find_urls(text)
        camp = [u for u in urls if is_campaign_link(u)]
        if camp:
            it = intake_mod.new_intake(self.ctx, camp[0])
            rest = text
            for u in camp:
                rest = rest.replace(u, "")
            if rest.strip():
                it["texts"].append(rest.strip())
                intake_mod.save(self.ctx, it)
            set_state(self.ctx, mode="intake", intake=it["id"])
            self.say("📥 New campaign. Now paste the rules / brief, or send screenshots or a PDF of them "
                     "(payout, rules, required tags, footage). Send /done when finished.")
            return
        if urls:
            self.say("Is that a video for a campaign? Use /videos to pick the campaign first.")
            return
        self.say("Send me a Content Rewards campaign link to add a campaign, or /help.")

    def in_intake(self, msg: dict, text: str) -> None:
        it = intake_mod.load(self.ctx, state(self.ctx)["intake"])
        added = []
        for u in find_urls(text):
            if is_campaign_link(u) and not it.get("campaign_url"):
                it["campaign_url"] = u
                added.append("campaign link")
        if text:
            it["texts"].append(text)
            added.append("text")
        d = intake_mod.intake_dir(self.ctx, it["id"])
        try:
            if msg.get("photo"):
                if sum(1 for f in it["files"] if f["kind"] == "image") >= intake_mod.MAX_IMAGES:
                    self.say(f"That's the maximum of {intake_mod.MAX_IMAGES} screenshots.")
                else:
                    ph = msg["photo"][-1]
                    p = self.api.download_file(ph["file_id"], d / f"shot{len(it['files']) + 1}.jpg")
                    it["files"].append({"path": str(p), "kind": "image", "mime": "image/jpeg"})
                    added.append("screenshot")
            elif msg.get("document"):
                doc = msg["document"]
                mime = doc.get("mime_type", "")
                name = Path(doc.get("file_name") or "file").name
                if mime == "application/pdf":
                    if sum(1 for f in it["files"] if f["kind"] == "pdf") >= intake_mod.MAX_PDFS:
                        self.say("That's the maximum of 3 PDFs.")
                    else:
                        p = self.api.download_file(doc["file_id"], d / f"doc{len(it['files']) + 1}.pdf")
                        it["files"].append({"path": str(p), "kind": "pdf", "mime": mime})
                        added.append("PDF")
                elif mime in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    p = self.api.download_file(doc["file_id"], d / f"shot{len(it['files']) + 1}.{mime.split('/')[1]}")
                    it["files"].append({"path": str(p), "kind": "image", "mime": mime})
                    added.append("image")
                elif mime.startswith("text/"):
                    p = self.api.download_file(doc["file_id"], d / name)
                    it["texts"].append(p.read_text(errors="replace")[:30_000])
                    added.append("text file")
                else:
                    self.say(f"I can read text, screenshots and PDFs — not {mime or 'that file type'}.")
        except TelegramError as exc:
            self.say(f"Couldn't download that file: {exc}")
        intake_mod.save(self.ctx, it)
        if added:
            self.say(f"Got it ({', '.join(added)}). Send more, or /done when finished.")

    def finish_intake(self, n: int) -> None:
        it = intake_mod.load(self.ctx, n)
        if not it["texts"] and not it["files"] and not it.get("campaign_url"):
            self.say("I don't have anything yet: send the campaign link and its rules first.")
            return
        if self.ctx.editor is None or self.ctx.editor.client is None:
            self.say("I need ANTHROPIC_API_KEY in .env to read campaigns. Add it, restart, then send /done again.")
            return
        self.say("🔎 Reading the campaign… (about a minute)")
        if it.get("campaign_url") and not it.get("page_text"):
            it["page_text"] = intake_mod.fetch_public_page(self.ctx, it["campaign_url"])
            intake_mod.save(self.ctx, it)
        try:
            intake_mod.run_assessment(self.ctx, it)
        except spend.BudgetExceeded as exc:
            self.say(f"Stopped by the spending limit: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - reported to the owner
            log.exception("assessment failed")
            self.say(f"Couldn't read the campaign: {exc}")
            return
        set_state(self.ctx, mode="idle")
        self.send_proposal(intake_mod.load(self.ctx, n))

    def send_proposal(self, it: dict) -> None:
        _v, _w, blockers = intake_mod.draft_to_values(self.ctx, it)
        n = it["id"]
        buttons = []
        if not blockers:
            buttons.append([("✅ Approve & start", f"ia:{n}")])
        buttons.append([("✏️ Correct / answer", f"ie:{n}"), ("❌ Skip", f"ix:{n}")])
        self.say(intake_mod.summary_message(self.ctx, it), buttons)

    def in_correct(self, msg: dict, text: str) -> None:
        n = state(self.ctx)["intake"]
        it = intake_mod.load(self.ctx, n)
        if not text:
            self.say("Type the corrections as text.")
            return
        it["corrections"].append(text)
        urls = [u for u in find_urls(text) if is_campaign_link(u)]
        if urls:
            it["campaign_url"] = urls[0]
        intake_mod.save(self.ctx, it)
        set_state(self.ctx, mode="intake", intake=n)
        self.finish_intake(n)

    def in_sources(self, msg: dict, text: str) -> None:
        opp = state(self.ctx)["opp"]
        urls = find_urls(text)
        if not urls:
            self.say("Send the video links (Google Drive / Dropbox / direct download), one or more per message. "
                     "/done when finished.")
            return
        allowed = allowlist(self.ctx.conn, self.ctx.settings.source_host_allowlist)
        ready, need_hosts, need_urls, sites = [], set(), [], set()
        for u in urls:
            try:
                direct = normalize_source_url(u)
            except LinkRefused as exc:
                self.say(f"⛔ {u}\n{exc}")
                continue
            if host_allowed(urlparse(direct).hostname or "", allowed):
                ready.append((u, direct))
            else:
                need_urls.append((u, direct))
                need_hosts.update(hosts_to_allow(u) + hosts_to_allow(direct))
                sites.add(urlparse(u).hostname or "")
        if ready:
            self.queue_sources(opp, ready)
        if need_urls:
            tok = prompts.make_token(self.ctx, {"opp": opp, "urls": need_urls, "hosts": sorted(need_hosts)})
            self.say(f"First time downloading from {', '.join(sorted(sites))}. Allow downloads from there? "
                     "Only do this for sites the campaign itself gave you.",
                     [[("✅ Allow", f"ha:{tok}"), ("❌ No", f"hx:{tok}")]])

    def queue_sources(self, opp: str, pairs: list) -> None:
        added = 0
        for original, direct in pairs:
            sid = add_manual(self.ctx, opp, provider_id_for(original), direct, "Telegram link",
                             f"campaign-provided footage for {opp} (sent by owner via Telegram)")
            added += 1 if sid else 0
        self.say(f"📥 Queued {added} video(s)" + (" (others were already added)" if added < len(pairs) else "") +
                 ". Send more or /done.")

    def in_receipt(self, msg: dict, text: str) -> None:
        from clipper.submissions.manual import record_submitted

        sid = state(self.ctx)["sub"]
        receipt = text
        if msg.get("photo"):
            try:
                p = self.api.download_file(msg["photo"][-1]["file_id"], self.ctx.env.data_dir / "evidence" /
                                           f"submission{sid}-{self.ctx.now():%Y%m%dT%H%M%S}.jpg")
                receipt = (text + " " if text else "") + f"[screenshot saved: {p.name}]"
            except TelegramError as exc:
                self.say(f"Couldn't save the screenshot ({exc}); type the confirmation instead.")
                return
        try:
            s = record_submitted(self.ctx, sid, receipt)
        except ValueError as exc:
            self.say(str(exc))
            return
        set_state(self.ctx, mode="idle")
        late = " (after the deadline)" if s["status"] == "submitted_late" else ""
        self.say(f"✅ Recorded as submitted{late}. The next post is unlocked.")

    def in_missed(self, msg: dict, text: str) -> None:
        from clipper.submissions.manual import record_missed

        try:
            record_missed(self.ctx, state(self.ctx)["sub"], text)
        except ValueError as exc:
            self.say(str(exc))
            return
        set_state(self.ctx, mode="idle")
        self.say("Noted as missed. The post stays up as it is. The next post is unlocked.")

    def in_budget(self, msg: dict, text: str) -> None:
        tok = state(self.ctx)["token"]
        t = prompts.get_token(self.ctx, tok)
        try:
            minor = ledger.to_minor(text.replace("$", "").strip())
        except ValueError:
            self.say("Send just the number shown as remaining budget, e.g. 12450.50")
            return
        set_state(self.ctx, mode="idle")
        self.publish(t["opp"], t["clip"], minor)

    def in_preapproval(self, msg: dict, text: str) -> None:
        from clipper.jobs.handlers import try_release

        st = state(self.ctx)
        if len(text) < 4:
            self.say("Paste the manager's message or a reference to it.")
            return
        with tx(self.ctx.conn):
            self.ctx.conn.execute("UPDATE clips SET preapproval_state=?, preapproval_evidence=? WHERE id=?",
                                  (st["decision"], text[:1000], st["clip"]))
            audit(self.ctx.conn, "owner:telegram", "clip.preapproval", {"id": st["clip"], "state": st["decision"]})
        set_state(self.ctx, mode="idle")
        reasons = try_release(self.ctx, st["clip"])
        self.say("Recorded. " + ("Clip is ready to post." if not reasons else "Still waiting: " + "; ".join(reasons)))

    # ------------------------------------------------------------ buttons --
    def on_button(self, data: str, message_id: int | None) -> None:
        verb, _, arg = data.partition(":")
        c = self.ctx.conn
        if verb == "noop":
            if message_id:
                self.api.clear_buttons(message_id)
            return
        if verb in ("ia", "ie", "ix"):
            it = intake_mod.load(self.ctx, int(arg))
            if not it:
                return
            if message_id:
                self.api.clear_buttons(message_id)
            if verb == "ix":
                self.say("Skipped. Send another campaign link any time.")
            elif verb == "ie":
                set_state(self.ctx, mode="correct", intake=it["id"])
                self.say("Tell me what to change or answer the questions, in plain words. For example: "
                         "\"max 45 seconds, pays $2 CPM, must include #ad and @brand\".")
            else:
                op, problems = intake_mod.approve(self.ctx, it)
                if problems:
                    self.say("Can't start yet:\n• " + "\n• ".join(problems))
                    return
                set_state(self.ctx, mode="sources", opp=op["id"])
                mode = "DRY RUN (simulated posts)" if not is_live(c, self.ctx.settings.mode, op["id"]) else "LIVE"
                self.say(f"✅ {op['name']} is set up and active ({mode}).\n\nNow send me the campaign's video links "
                         "(Google Drive / Dropbox / direct download — the footage the campaign provides). "
                         "/done when finished.")
            return
        if verb in ("ca", "cr"):
            self.review(int(arg), verb == "ca", message_id)
            return
        if verb in ("pa", "pr"):
            set_state(self.ctx, mode="preapproval", clip=int(arg), decision="approved" if verb == "pa" else "rejected")
            self.say("Paste the campaign manager's reply (or a reference to it) as evidence.")
            return
        if verb in ("sd", "sm"):
            sub = row(c, "SELECT * FROM submissions WHERE id=?", (int(arg),))
            if not sub or sub["status"] not in ("manual_required", "overdue"):
                self.say("That submission is already recorded.")
                return
            if verb == "sd":
                set_state(self.ctx, mode="receipt", sub=sub["id"])
                self.say("Paste the confirmation from Content Rewards (submission ID / message), or send a screenshot.")
            else:
                set_state(self.ctx, mode="missed", sub=sub["id"])
                self.say("What happened? (one line is fine)")
            return
        t = prompts.get_token(self.ctx, arg) if arg.isdigit() else None
        if t is None:
            self.say("That button has expired.")
            return
        if verb == "go":
            if message_id:
                self.api.clear_buttons(message_id)
            self.publish(t["opp"], t["clip"], None)
        elif verb == "gb":
            set_state(self.ctx, mode="budget", token=arg)
            self.say("What remaining budget does the campaign page show now? (just the number)")
        elif verb == "gl":
            prompts.snooze(self.ctx)
            if message_id:
                self.api.clear_buttons(message_id)
            self.say(f"OK, I'll ask again in {self.ctx.settings.telegram_prompt_snooze_minutes // 60} h (or send /post).")
        elif verb == "vs":
            set_state(self.ctx, mode="sources", opp=t["opp"])
            self.say("Send the video links for this campaign. /done when finished.")
        elif verb in ("op", "oa"):
            opportunities.set_status(c, self.ctx.now(), t["opp"], "paused" if verb == "op" else "active",
                                     actor="owner:telegram")
            self.say(("⏸ Paused " if verb == "op" else "▶️ Resumed ") + t["opp"])
        elif verb in ("ha", "hx"):
            if message_id:
                self.api.clear_buttons(message_id)
            if verb == "hx":
                self.say("OK, not downloading from there.")
                return
            approve_hosts(c, t["hosts"])
            with tx(c):
                audit(c, "owner:telegram", "source_hosts.approved", {"hosts": t["hosts"]})
            self.queue_sources(t["opp"], [tuple(x) for x in t["urls"]])
        elif verb == "lv":
            live = set(kv_get(c, "live_opportunities", []) or [])
            op = opportunities.get(c, t["opp"])
            if t["opp"] in live:
                live.discard(t["opp"])
                with tx(c):
                    kv_set(c, "live_opportunities", sorted(live))
                    audit(c, "owner:telegram", "opportunity.live", {"id": t["opp"], "enabled": False})
                self.say(f"⚪️ {op['name']}: back to dry run.")
            else:
                tok = prompts.make_token(self.ctx, {"opp": t["opp"]})
                self.say(f"Switch {op['name']} to REAL posting on {op['publisher']} account {op['account_ref']}? "
                         "Posts will go public.", [[("Yes, go live", f"lc:{tok}"), ("Cancel", "noop:0")]])
        elif verb == "lc":
            if message_id:
                self.api.clear_buttons(message_id)
            with tx(c):
                live = set(kv_get(c, "live_opportunities", []) or [])
                live.add(t["opp"])
                kv_set(c, "live_opportunities", sorted(live))
                audit(c, "owner:telegram", "opportunity.live", {"id": t["opp"], "enabled": True})
            self.say("🟢 Live. Capability checks still apply: anything unverified stays blocked and I'll tell you why.")

    def review(self, cid: int, approve: bool, message_id: int | None) -> None:
        from clipper.jobs.handlers import try_release

        clip = row(self.ctx.conn, "SELECT * FROM clips WHERE id=?", (cid,))
        if not clip or clip["stage"] in ("published", "rejected"):
            self.say(f"Clip #{cid} was already handled.")
            return
        if message_id:
            self.api.clear_buttons(message_id)
        with tx(self.ctx.conn):
            if approve:
                self.ctx.conn.execute("UPDATE clips SET review_state='approved', context_risk=0, updated_at=? WHERE id=?",
                                      (iso(self.ctx.now()), cid))
            else:
                self.ctx.conn.execute("UPDATE clips SET review_state='rejected', stage='rejected', hold_reason="
                                      "'rejected in Telegram', updated_at=? WHERE id=?", (iso(self.ctx.now()), cid))
            audit(self.ctx.conn, "owner:telegram", "clip.approve" if approve else "clip.reject", {"id": cid})
        if not approve:
            self.say(f"❌ Clip #{cid} rejected.")
            return
        reasons = try_release(self.ctx, cid)
        self.say(f"✅ Clip #{cid} approved — " + ("ready to post." if not reasons else "still waiting: " + "; ".join(reasons)))

    def publish(self, opp: str, clip: int, budget_minor: int | None) -> None:
        pub_id, reasons = prompts.go(self.ctx, opp, clip, budget_minor)
        if reasons:
            self.say("Didn't post:\n• " + "\n• ".join(reasons))
            return
        pub = row(self.ctx.conn, "SELECT status, last_error FROM publications WHERE id=?", (pub_id,))
        if pub["status"] == "failed":
            self.say(f"Posting failed and nothing went out: {pub['last_error']}")
        elif pub["status"] == "unknown":
            self.say("⚠️ The platform didn't answer clearly. Check the account; resolve it on the dashboard → Publications.")
        else:
            self.say("📤 Posting… I'll send the live link and the 30-minute countdown as soon as it's up.")


def run_bot(ctx: Ctx) -> None:
    if ctx.tg is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    bot = Bot(ctx, ctx.tg)
    ctx.tg.set_commands(COMMANDS)
    offset = kv_get(ctx.conn, "tg_offset")
    backoff = 5
    log.info("telegram bot started")
    while True:
        try:
            updates = ctx.tg.get_updates(offset, timeout=50)
            backoff = 5
        except TelegramError as exc:
            log.warning("getUpdates failed: %s", exc)
            time.sleep(backoff)
            backoff = min(300, backoff * 2)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            with tx(ctx.conn):
                kv_set(ctx.conn, "tg_offset", offset)
            try:
                bot.handle(u)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                log.exception("update %s failed", u.get("update_id"))
                bot.say(f"Something went wrong handling that: {exc}")


__all__ = ["Bot", "run_bot"]
