"""Publisher interface. Adapters must not invent API operations; anything not
verified on the live account is gated by a capability record (rules.gates)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from clipper.jobs.queue import PermanentError, TransientError


@dataclass
class PostRequest:
    intent_key: str
    account_ref: str
    caption: str
    media_url: str
    title: str = ""
    disclosure: dict = field(default_factory=dict)   # {"paid_partnership": bool, "sponsor_ids": [...]}


@dataclass
class CreateResult:
    provider_id: str
    state: dict = field(default_factory=dict)     # adapter-private, persisted on the publication


@dataclass
class StatusResult:
    status: Literal["pending", "sent", "failed", "unknown"]
    sent_at: datetime | None = None
    external_link: str | None = None
    error: str | None = None
    state: dict | None = None                   # updated adapter-private state, if any


class AmbiguousSendError(Exception):
    """The request may or may not have reached the provider (timeout after send).
    Never retried blindly: reconcile, else hold and alert."""

    error_class = "ambiguous"


class Publisher(Protocol):
    name: str

    def create(self, req: PostRequest) -> CreateResult: ...

    def status(self, provider_id: str, state: dict) -> StatusResult: ...

    def find_existing(self, req: PostRequest, since: datetime) -> CreateResult | None:
        """Look for a post we may have created already (reconciliation)."""
        ...


__all__ = ["AmbiguousSendError", "CreateResult", "PermanentError", "PostRequest", "Publisher",
           "StatusResult", "TransientError"]
