"""Buffer publishing adapter (GraphQL API at https://api.buffer.com).

VERIFY ON DAY 1: the operation and field names below follow Buffer's public
API documentation (createPost with schedulingType/mode/assets; post status,
sentAt, externalLink). They could not be re-fetched from this build
environment. Run ``python -m clipper check-buffer`` to introspect the live
schema before enabling live mode; it reports any field used here that the
live schema lacks.

Request budget: Free plan allows ~1,000 requests per channel per 30 days. Every
call is logged in ``api_requests``; creates are refused when the remaining
allowance would eat into the reconciliation reserve.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Callable

import httpx

from clipper.db import iso, parse_iso, row
from clipper.publishing.base import (AmbiguousSendError, CreateResult, PermanentError, PostRequest,
                                     StatusResult, TransientError)

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}
"""

GET_POST = """
query GetPost($id: PostId!) {
  post(input: { id: $id }) { id status sentAt externalLink dueAt text }
}
"""

LIST_POSTS = """
query RecentPosts($org: OrganizationId!, $channel: ChannelId!) {
  posts(first: 20, input: { organizationId: $org, filter: { channelIds: [$channel] } }) {
    edges { node { id status text sentAt externalLink createdAt } }
  }
}
"""

# what check-buffer verifies exists in the live schema
SCHEMA_EXPECTATIONS = {
    "Mutation": ["createPost"],
    "Query": ["post", "posts"],
    "Post": ["id", "status", "sentAt", "externalLink", "dueAt", "text"],
    "CreatePostInput": ["text", "channelId", "schedulingType", "mode", "assets"],
}

SENT_STATES = {"sent"}
FAILED_STATES = {"error", "failed"}


class BufferPublisher:
    name = "buffer"

    def __init__(self, api_key: str | None, api_url: str, http: httpx.Client, conn: sqlite3.Connection,
                 clock: Callable[[], datetime], *, requests_per_30d: int, reserve_requests: int,
                 organization_id: str | None = None, platform_metadata: dict | None = None):
        self.api_key = api_key
        self.api_url = api_url
        self.http = http
        self.conn = conn
        self.clock = clock
        self.limit = requests_per_30d
        self.reserve = reserve_requests
        self.organization_id = organization_id
        self.platform_metadata = platform_metadata or {}

    # -- request accounting -------------------------------------------------
    def used(self, channel: str) -> int:
        since = iso(self.clock() - timedelta(days=30))
        return row(self.conn, "SELECT COUNT(*) n FROM api_requests WHERE provider='buffer' AND channel=? "
                              "AND created_at>=?", (channel, since))["n"]

    def remaining(self, channel: str) -> int:
        return self.limit - self.used(channel)

    def _log(self, channel: str, purpose: str, code: int | None) -> None:
        self.conn.execute("INSERT INTO api_requests(provider, channel, purpose, status_code, created_at) "
                          "VALUES ('buffer', ?, ?, ?, ?)", (channel, purpose, code, iso(self.clock())))

    def _post(self, channel: str, purpose: str, query: str, variables: dict, *, ambiguous_on_timeout: bool) -> dict:
        if not self.api_key:
            raise PermanentError("BUFFER_API_KEY is not configured")
        cooldown = parse_iso(self._kv_cooldown())
        if cooldown and cooldown > self.clock():
            raise TransientError("Buffer rate-limit cooldown", (cooldown - self.clock()).total_seconds())
        try:
            r = self.http.post(self.api_url, json={"query": query, "variables": variables},
                               headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30)
        except httpx.ConnectError as exc:
            self._log(channel, purpose, None)
            raise TransientError(f"Buffer unreachable: {exc}")
        except httpx.ConnectTimeout as exc:
            self._log(channel, purpose, None)
            raise TransientError(f"Buffer connect timeout: {exc}")
        except httpx.TransportError as exc:
            self._log(channel, purpose, None)
            if ambiguous_on_timeout:
                raise AmbiguousSendError(f"Buffer request may have been accepted: {exc}")
            raise TransientError(f"Buffer transport error: {exc}")
        self._log(channel, purpose, r.status_code)
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            wait = float(ra) if ra and ra.replace(".", "").isdigit() else 900
            self._set_cooldown(self.clock() + timedelta(seconds=wait))
            raise TransientError("Buffer rate limited", wait)
        if r.status_code in (401, 403):
            raise PermanentError(f"Buffer rejected credentials (HTTP {r.status_code})")
        if r.status_code >= 500:
            if ambiguous_on_timeout:
                raise AmbiguousSendError(f"Buffer HTTP {r.status_code} on create")
            raise TransientError(f"Buffer HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PermanentError(f"Buffer HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in body["errors"])
            codes = {(e.get("extensions") or {}).get("code") for e in body["errors"]}
            if codes & {"UNAUTHENTICATED", "FORBIDDEN"}:
                raise PermanentError(f"Buffer auth error: {msg}")
            raise PermanentError(f"Buffer GraphQL error: {msg}")
        return body.get("data") or {}

    def _kv_cooldown(self) -> str | None:
        r = row(self.conn, "SELECT value FROM kv WHERE key='buffer_cooldown_until'")
        return r["value"].strip('"') if r else None

    def _set_cooldown(self, until: datetime) -> None:
        from clipper.db import kv_set

        kv_set(self.conn, "buffer_cooldown_until", iso(until))

    # -- Publisher API -----------------------------------------------------------
    def create(self, req: PostRequest) -> CreateResult:
        # a create plus its status checks and a margin must fit above the reserve
        if self.remaining(req.account_ref) < 1 + 6 + self.reserve:
            raise PermanentError(
                f"Buffer request budget: only {self.remaining(req.account_ref)} requests left in the 30-day window "
                f"(reserve {self.reserve}); new posts paused")
        if req.disclosure.get("paid_partnership"):
            # Not verified to be settable through Buffer; gates should already block this.
            raise PermanentError("native paid-partnership label is not supported by the Buffer adapter")
        inp = {
            "text": req.caption,
            "channelId": req.account_ref,
            "schedulingType": "automatic",   # never 'notification' (manual reminder)
            "mode": "shareNow",              # the local scheduler owns timing
            "assets": {"videos": [{"url": req.media_url}]},
        }
        if self.platform_metadata:
            inp["metadata"] = self.platform_metadata
        data = self._post(req.account_ref, "create", CREATE_POST, {"input": inp}, ambiguous_on_timeout=True)
        res = data.get("createPost") or {}
        if res.get("message") and not res.get("post"):
            raise PermanentError(f"Buffer refused the post: {res['message']}")
        post = res.get("post") or {}
        if not post.get("id"):
            raise AmbiguousSendError("Buffer response had no post id")
        return CreateResult(provider_id=str(post["id"]), state={"channel": req.account_ref})

    def status(self, provider_id: str, state: dict) -> StatusResult:
        channel = state.get("channel", "unknown")
        data = self._post(channel, "status", GET_POST, {"id": provider_id}, ambiguous_on_timeout=False)
        post = data.get("post")
        if not post:
            return StatusResult("unknown", error="post not found")
        st = (post.get("status") or "").lower()
        sent_at = parse_iso(post.get("sentAt"))
        link = post.get("externalLink")
        if st in SENT_STATES:
            return StatusResult("sent", sent_at=sent_at, external_link=link)
        if st in FAILED_STATES:
            return StatusResult("failed", error=f"Buffer status {st}")
        return StatusResult("pending")

    def find_existing(self, req: PostRequest, since: datetime) -> CreateResult | None:
        if not self.organization_id:
            return None  # cannot list without an organization id → caller holds and alerts
        data = self._post(req.account_ref, "housekeeping", LIST_POSTS,
                          {"org": self.organization_id, "channel": req.account_ref}, ambiguous_on_timeout=False)
        for edge in ((data.get("posts") or {}).get("edges") or []):
            node = edge.get("node") or {}
            created = parse_iso(node.get("createdAt"))
            if node.get("text", "").strip() == req.caption.strip() and (created is None or created >= since - timedelta(minutes=2)):
                return CreateResult(provider_id=str(node["id"]), state={"channel": req.account_ref})
        return None

    # -- schema check ----------------------------------------------------------------
    def introspect(self) -> dict[str, list[str]]:
        """Return {type: [missing fields]} for fields this adapter relies on."""
        q = "query { __schema { types { name fields { name } inputFields { name } } } }"
        data = self._post("setup", "setup", q, {}, ambiguous_on_timeout=False)
        types = {t["name"]: {f["name"] for f in (t.get("fields") or []) + (t.get("inputFields") or [])}
                 for t in data["__schema"]["types"]}
        missing = {}
        for tname, fields in SCHEMA_EXPECTATIONS.items():
            have = types.get(tname)
            if have is None:
                missing[tname] = ["<type missing>"]
            else:
                gone = [f for f in fields if f not in have]
                if gone:
                    missing[tname] = gone
        return missing
