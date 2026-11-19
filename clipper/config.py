"""Configuration.

Two layers:
* ``config.yaml`` holds non-secret operating settings (section 13 of the plan).
* Environment variables (``.env``) hold secrets and deployment paths.

Nothing in here is editable by model output; the web UI only changes the
runtime switches stored in the database (pause, live mode per route).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ModelPricing(BaseModel):
    """USD per million tokens. Used only for spend *reservations*; the
    provider's bill is the source of truth."""

    input_per_mtok: float = 4.00
    output_per_mtok: float = 20.00


class Settings(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    timezone: str = "America/Toronto"
    max_posts_per_day_total: int = 1
    max_source_minutes_per_day: int = 30
    max_source_bytes: int = 4 * 1024**3
    source_poll_minutes: int = 30
    heavy_worker_concurrency: int = 1
    max_candidates_per_source: int = 6
    monthly_operating_ceiling_usd: float = 40.0
    monthly_model_allowance_usd: float = 10.0
    campaign_submission_mode: Literal["manual"] = "manual"
    campaign_publish_requires_operator_ready: bool = True
    operator_ready_expires_minutes: int = 5
    submission_target_minutes: int = 10
    submission_reminder_minutes: list[int] = Field(default_factory=lambda: [5, 15])
    submission_escalate_minutes: int = 20
    provider_initial_status_checks_max: int = 6
    provider_status_check_offsets_s: list[int] = Field(
        default_factory=lambda: [15, 45, 90, 180, 300, 600]
    )
    calibration_review_count: int = 10
    publish_slot_grace_minutes: int = 30
    operator_hourly_rate_usd: float = 15.0

    # Claude editorial service
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    claude_max_output_tokens: int = 16000
    claude_pricing: ModelPricing = Field(default_factory=ModelPricing)
    editorial_window_minutes: int = 20
    editorial_window_overlap_minutes: int = 2

    # Transcription
    whisper_model: str = "small"
    whisper_compute_type: str = "int8"
    whisper_min_language_probability: float = 0.6

    # Disk policy
    min_free_disk_gb: float = 5.0
    min_free_disk_fraction: float = 0.20
    raw_retention_days: int = 7
    submission_retention_days: int = 120

    # Publisher request budgets (per channel per 30 days)
    buffer_requests_per_30d: int = 1000
    buffer_reserve_requests: int = 310
    # Optional Buffer post metadata, e.g. {"instagram": {"type": "reel", "shouldShareToFeed": true}}.
    # Copy the exact shape from Buffer's current API docs and verify with a live test.
    buffer_platform_metadata: dict = Field(default_factory=dict)
    # Meta branded-content request params, copied from Meta's current docs and verified live.
    # Use the literal value "{sponsor_ids}" where the sponsor id list belongs.
    instagram_paid_partnership_params: dict = Field(default_factory=dict)
    score_weights: dict[str, float] = Field(default_factory=lambda: {
        "clarity": 1.0, "context": 1.0, "payoff": 1.5, "distinctness": 0.75, "campaign_fit": 1.25})

    # Download allowlist: hosts a source feed or its media may come from
    source_host_allowlist: list[str] = Field(default_factory=list)

    # Rendering
    template_version: str = "v1-vertical-1080"


class Env(BaseModel):
    data_dir: Path
    config_path: Path
    public_base_url: str
    anthropic_api_key: str | None
    buffer_api_key: str | None
    buffer_api_url: str
    buffer_organization_id: str | None
    instagram_access_token: str | None
    instagram_user_id: str | None
    instagram_graph_version: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    owner_password_hash: str | None
    session_secret: str
    backup_key: str | None
    cookie_secure: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def public_dir(self) -> Path:
        return self.data_dir / "public"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("CLIPPER_CONFIG", "config.yaml"))
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        return Settings.model_validate(data)
    return Settings()


def load_env() -> Env:
    e = os.environ.get
    return Env(
        data_dir=Path(e("CLIPPER_DATA_DIR", "data")),
        config_path=Path(e("CLIPPER_CONFIG", "config.yaml")),
        public_base_url=e("CLIPPER_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        anthropic_api_key=e("ANTHROPIC_API_KEY") or None,
        buffer_api_key=e("BUFFER_API_KEY") or None,
        buffer_api_url=e("BUFFER_API_URL", "https://api.buffer.com"),
        buffer_organization_id=e("BUFFER_ORGANIZATION_ID") or None,
        instagram_access_token=e("INSTAGRAM_ACCESS_TOKEN") or None,
        instagram_user_id=e("INSTAGRAM_USER_ID") or None,
        instagram_graph_version=e("INSTAGRAM_GRAPH_VERSION", "v23.0"),
        telegram_bot_token=e("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=e("TELEGRAM_CHAT_ID") or None,
        owner_password_hash=e("OWNER_PASSWORD_HASH") or None,
        session_secret=e("SESSION_SECRET", ""),
        backup_key=e("BACKUP_KEY") or None,
        cookie_secure=e("COOKIE_SECURE", "true").lower() == "true",
    )


@lru_cache
def settings() -> Settings:
    return load_settings()


@lru_cache
def env() -> Env:
    return load_env()


def ensure_dirs(en: Env) -> None:
    for d in (en.data_dir, en.media_dir, en.work_dir, en.public_dir, en.backup_dir):
        d.mkdir(parents=True, exist_ok=True)
