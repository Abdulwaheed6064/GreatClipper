"""Builds publisher adapters. In dry_run mode (or for an opportunity not
switched live) the DryRunPublisher is used: it exercises the whole state
machine without contacting any platform."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from clipper.publishing.base import CreateResult, PostRequest, StatusResult
from clipper.publishing.buffer import BufferPublisher
from clipper.publishing.instagram import InstagramPublisher

if TYPE_CHECKING:
    from clipper.context import Ctx


class DryRunPublisher:
    name = "dry_run"

    def __init__(self, clock):
        self.clock = clock

    def create(self, req: PostRequest) -> CreateResult:
        return CreateResult(provider_id=f"dry-{req.intent_key}", state={"created_at": self.clock().isoformat()})

    def status(self, provider_id: str, state: dict) -> StatusResult:
        return StatusResult("sent", sent_at=self.clock(), external_link=f"https://dry-run.invalid/{provider_id}")

    def find_existing(self, req: PostRequest, since: datetime) -> CreateResult | None:
        return None


def build_publishers(ctx: "Ctx") -> dict:
    st, en = ctx.settings, ctx.env
    return {
        "dry_run": DryRunPublisher(ctx.now),
        "buffer": BufferPublisher(
            en.buffer_api_key, en.buffer_api_url, ctx.http, ctx.conn, ctx.now,
            requests_per_30d=st.buffer_requests_per_30d, reserve_requests=st.buffer_reserve_requests,
            organization_id=en.buffer_organization_id, platform_metadata=st.buffer_platform_metadata,
        ),
        "instagram": InstagramPublisher(
            en.instagram_access_token, en.instagram_user_id, en.instagram_graph_version, ctx.http,
            paid_partnership_params=st.instagram_paid_partnership_params,
        ),
    }
