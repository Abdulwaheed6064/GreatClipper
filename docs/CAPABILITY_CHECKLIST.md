# Capability checklist — Day-1 go/no-go

Record each result on **Dashboard → Capabilities** with evidence (live post URL + what you saw). Live publishing that depends on an unverified capability stays blocked automatically. Re-check provider docs on the day: capabilities and limits change.

**If a required capability fails, stop building around it.** Keep the semi-manual route (export the clip, publish by hand, use the submission helper) rather than bypassing a platform rule or removing a disclosure.

## Business gate (outside the app)

- [ ] Content Rewards account created; Whop KYC completed; withdrawal method available in your country.
- [ ] Destination social account connected and verified in Content Rewards.
- [ ] Joined one campaign; full brief saved into the opportunity's *terms text*; hidden post-join rules read.
- [ ] Remaining budget observed and recorded.
- [ ] Authorized source acquisition confirmed (campaign-provided files, creator feed, or written permission). A public channel link is **not** a download right.

## Alerts

- [ ] `alerts.telegram` — Dashboard → *Send test alert* arrives on your phone. (Recorded automatically on success.)

## Source

- [ ] `source.feed` — the creator feed (or a manual link) yields a file; a second poll creates no duplicate.

## Buffer (client / owned route)

- [ ] `python -m clipper check-buffer` (or Capabilities → *Buffer schema check*) reports no missing fields. If anything is missing, update the queries in `clipper/publishing/buffer.py` from Buffer's current API reference before going further.
- [ ] `buffer.automatic_publish` — a known permitted test clip posted through the app **appears on the account by itself** (not as a reminder/notification to post manually).
- [ ] `buffer.external_link` — the app stored the real post URL and publish time (Publications page).
- [ ] `buffer.native_paid_partnership` — leave **unverified** unless you have proven Buffer sets the native label. Campaigns that require it will then be blocked from Buffer automatically.
- [ ] Request budget looks right on the Publications page after the test (each create + status check is counted).

## Instagram Graph API (campaign route with native disclosure)

- [ ] Professional (Business/Creator) account; long-lived token with content-publishing permission in `.env`.
- [ ] `instagram.reels_publish` — test Reel published via the app.
- [ ] `instagram.permalink` — permalink + timestamp stored.
- [ ] `instagram.paid_partnership` — copy the branded-content request parameters from Meta's current docs into `instagram_paid_partnership_params` in `config.yaml`, publish an authorized test, confirm the **Paid partnership** label and sponsor are visible on the post. Only then mark verified.

## Media

- [ ] Benchmark on the target host: 30-min source + 3 clips < 2 h, < 80 % RAM (`python -m clipper benchmark FILE`).
- [ ] First 5–10 outputs reviewed by you (calibration). Captions readable, safe margins OK, layout correct.

## Reliability (before raising cadence above 1/day)

- [ ] `backup.restore_tested` — restore one encrypted backup on a copy (`python -m clipper restore FILE` with services stopped, pointed at a scratch data dir) and confirm data is present.
- [ ] Three consecutive days at 1 post/day without duplicates, missed deadlines or unexplained holds.
