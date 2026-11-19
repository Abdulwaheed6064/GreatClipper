"""Direct Instagram (Meta Graph API) Reels publishing for a professional account.

Two-phase flow, advanced by status checks so the scheduler never blocks:
  1. create():  POST /{ig-user-id}/media  (media_type=REELS, video_url, caption) → container id
  2. status():  GET /{container}?fields=status_code → when FINISHED,
                POST /{ig-user-id}/media_publish (creation_id) → media id
                GET /{media}?fields=permalink,timestamp → live URL + publish time

Paid-partnership labelling: the request parameters for Meta's branded-content
fields must be copied from Meta's current documentation into
``config.yaml → instagram_paid_partnership_params`` and verified with a live
test (capability ``instagram.paid_partnership``). Until then any post that
needs the native label is refused here and blocked by the gates.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from clipper.db import parse_iso
from clipper.publishing.base import (AmbiguousSendError, CreateResult, PermanentError, PostRequest,
                                     StatusResult, TransientError)


class InstagramPublisher:
    name = "instagram"

    def __init__(self, access_token: str | None, ig_user_id: str | None, graph_version: str,
                 http: httpx.Client, paid_partnership_params: dict | None = None):
        self.token = access_token
        self.ig_user_id = ig_user_id
        self.base = f"https://graph.facebook.com/{graph_version}"
        self.http = http
        self.pp_params = paid_partnership_params or {}

    def _req(self, method: str, path: str, *, params: dict, ambiguous: bool = False) -> dict:
        if not self.token:
            raise PermanentError("INSTAGRAM_ACCESS_TOKEN is not configured")
        params = {**params, "access_token": self.token}
        try:
            r = self.http.request(method, f"{self.base}/{path}", data=params if method == "POST" else None,
                                  params=params if method == "GET" else None, timeout=60)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TransientError(f"Instagram unreachable: {exc}")
        except httpx.TransportError as exc:
            if ambiguous:
                raise AmbiguousSendError(f"Instagram request may have been accepted: {exc}")
            raise TransientError(f"Instagram transport error: {exc}")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429 or (body.get("error") or {}).get("code") in (4, 17, 32, 613):
            raise TransientError("Instagram rate limited", 900)
        if r.status_code >= 500:
            if ambiguous:
                raise AmbiguousSendError(f"Instagram HTTP {r.status_code}")
            raise TransientError(f"Instagram HTTP {r.status_code}")
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            if err.get("code") == 190 or r.status_code in (401, 403):
                raise PermanentError(f"Instagram token invalid/expired: {err.get('message')}")
            raise PermanentError(f"Instagram error {r.status_code}: {err.get('message') or r.text[:300]}")
        return body

    def create(self, req: PostRequest) -> CreateResult:
        params = {"media_type": "REELS", "video_url": req.media_url, "caption": req.caption, "share_to_feed": "true"}
        if req.disclosure.get("paid_partnership"):
            if not self.pp_params:
                raise PermanentError("paid-partnership parameters are not configured/verified for Instagram")
            for k, v in self.pp_params.items():
                params[k] = (",".join(req.disclosure.get("sponsor_ids") or []) if v == "{sponsor_ids}" else v)
        body = self._req("POST", f"{req.account_ref}/media", params=params, ambiguous=False)
        # container creation is not a public post; a timeout here is safe to retry
        if not body.get("id"):
            raise TransientError("no container id returned")
        return CreateResult(provider_id=str(body["id"]), state={"phase": "container", "container_id": body["id"],
                                                                 "ig_user_id": req.account_ref,
                                                                 "caption_hint": req.caption})

    def status(self, provider_id: str, state: dict) -> StatusResult:
        phase = state.get("phase", "container")
        ig = state.get("ig_user_id") or self.ig_user_id
        if phase == "container":
            c = self._req("GET", state["container_id"], params={"fields": "status_code,status"})
            code = c.get("status_code")
            if code in ("ERROR", "EXPIRED"):
                return StatusResult("failed", error=f"container {code}: {c.get('status')}")
            if code != "FINISHED":
                return StatusResult("pending")
            try:
                pub = self._req("POST", f"{ig}/media_publish", params={"creation_id": state["container_id"]},
                                ambiguous=True)
            except AmbiguousSendError:
                return StatusResult("pending", state={**state, "phase": "publish_ambiguous"})
            state = {**state, "phase": "published", "media_id": pub["id"]}
            phase = "published"
        if phase == "publish_ambiguous":
            found = self._find_media(ig, state.get("caption_hint"), None)
            if not found:
                return StatusResult("unknown", error="publish call timed out and post not found yet", state=state)
            state = {**state, "phase": "published", "media_id": found["id"]}
        m = self._req("GET", state["media_id"], params={"fields": "permalink,timestamp"})
        link = m.get("permalink")
        if not link:
            return StatusResult("pending", state=state)
        return StatusResult("sent", sent_at=parse_iso(_fix_ts(m.get("timestamp"))), external_link=link, state=state)

    def _find_media(self, ig: str, caption: str | None, since: datetime | None) -> dict | None:
        body = self._req("GET", f"{ig}/media", params={"fields": "id,caption,timestamp,permalink", "limit": 10})
        for m in body.get("data", []):
            ts = parse_iso(_fix_ts(m.get("timestamp")))
            if caption is not None and (m.get("caption") or "").strip() != caption.strip():
                continue
            if since and ts and ts < since - timedelta(minutes=2):
                continue
            return m
        return None

    def find_existing(self, req: PostRequest, since: datetime) -> CreateResult | None:
        m = self._find_media(req.account_ref, req.caption, since)
        if not m:
            return None
        return CreateResult(provider_id=str(m["id"]), state={"phase": "published", "media_id": m["id"],
                                                              "ig_user_id": req.account_ref})


def _fix_ts(ts: str | None) -> str | None:
    # Graph API returns e.g. 2026-11-19T15:04:05+0000
    if ts and len(ts) > 5 and ts[-5] in "+-" and ts[-3] != ":":
        return ts[:-2] + ":" + ts[-2:]
    return ts
