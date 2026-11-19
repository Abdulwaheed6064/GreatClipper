-- Initial schema. All timestamps are UTC ISO-8601 strings.
-- Money is stored as integer minor units (cents) with an explicit currency.

CREATE TABLE opportunities (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    revenue_route           TEXT NOT NULL CHECK (revenue_route IN ('client','campaign','owned')),
    status                  TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','paused','archived')),
    source_url              TEXT,
    campaign_url            TEXT,
    owner_contact           TEXT,
    permission_evidence     TEXT,
    terms_text              TEXT,
    terms_version           INTEGER NOT NULL DEFAULT 1,
    terms_hash              TEXT,
    approved_terms_version  INTEGER,          -- NULL = a human has not confirmed the current terms
    approved_at             TEXT,
    verified_at             TEXT,
    expires_at              TEXT,
    allowed_from            TEXT,
    allowed_until           TEXT,
    platform                TEXT,             -- instagram / tiktok / youtube / ...
    publisher               TEXT NOT NULL DEFAULT 'manual' CHECK (publisher IN ('buffer','instagram','manual')),
    account_ref             TEXT,             -- Buffer channel id or Instagram user id
    feed_url                TEXT,             -- creator-controlled JSON feed (optional)
    required_tags           TEXT NOT NULL DEFAULT '[]',   -- JSON list of literal strings that must appear in caption
    disclosure_required     TEXT NOT NULL DEFAULT 'none' CHECK (disclosure_required IN ('none','caption','native_paid_partnership')),
    sponsor_ids             TEXT NOT NULL DEFAULT '[]',
    permitted_audio         TEXT NOT NULL DEFAULT 'original_only',
    min_duration_s          REAL NOT NULL DEFAULT 15,
    max_duration_s          REAL NOT NULL DEFAULT 60,
    prohibited              TEXT,
    reuse_constraints       TEXT,
    requires_preapproval    INTEGER NOT NULL DEFAULT 0,
    layout                  TEXT NOT NULL DEFAULT '{"template":"blur_pad"}',  -- JSON, calibrated at onboarding
    caption_template        TEXT NOT NULL DEFAULT '{caption}\n\n{tags}',
    posting_times           TEXT NOT NULL DEFAULT '[]',   -- JSON list of "HH:MM" local times (client/owned route)
    editorial_notes         TEXT,
    payment                 TEXT NOT NULL DEFAULT '{}',   -- JSON, see clipper/ledger/terms.py
    budget_remaining_minor  INTEGER,
    budget_currency         TEXT,
    budget_observed_at      TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE opportunity_terms_history (
    id              INTEGER PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(id),
    terms_version   INTEGER NOT NULL,
    snapshot        TEXT NOT NULL,     -- full JSON snapshot of the opportunity row
    created_at      TEXT NOT NULL,
    UNIQUE (opportunity_id, terms_version)
);

CREATE TABLE sources (
    id                  INTEGER PRIMARY KEY,
    opportunity_id      TEXT NOT NULL REFERENCES opportunities(id),
    provider_id         TEXT NOT NULL,
    processing_version  TEXT NOT NULL DEFAULT 'p1',
    ingest_mode         TEXT NOT NULL DEFAULT 'feed' CHECK (ingest_mode IN ('feed','manual')),
    download_url        TEXT,
    title               TEXT,
    rights_ref          TEXT,
    stage               TEXT NOT NULL DEFAULT 'discovered'
                        CHECK (stage IN ('discovered','acquired','transcribed','selected','done','held','rejected')),
    hold_reason         TEXT,
    file_path           TEXT,
    file_hash           TEXT,
    file_bytes          INTEGER,
    duration_s          REAL,
    width               INTEGER,
    height              INTEGER,
    transcript_path     TEXT,
    language            TEXT,
    language_probability REAL,
    processing_cost_minor INTEGER NOT NULL DEFAULT 0,   -- USD cents
    acquired_at         TEXT,
    discovered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (opportunity_id, provider_id, processing_version)
);
CREATE INDEX sources_hash ON sources(file_hash);

CREATE TABLE editorial_cache (
    cache_key   TEXT PRIMARY KEY,   -- hash(transcript, terms_version, prompt_version, model)
    response    TEXT NOT NULL,
    cost_minor  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE clips (
    id                  INTEGER PRIMARY KEY,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    opportunity_id      TEXT NOT NULL REFERENCES opportunities(id),
    start_s             REAL NOT NULL,
    end_s               REAL NOT NULL,
    hook                TEXT,
    payoff              TEXT,
    title               TEXT,
    caption             TEXT,
    hashtags            TEXT NOT NULL DEFAULT '[]',
    evidence            TEXT,
    scores              TEXT NOT NULL DEFAULT '{}',
    score_total         REAL NOT NULL DEFAULT 0,
    context_risk        INTEGER NOT NULL DEFAULT 0,
    terms_version       INTEGER NOT NULL,
    template_version    TEXT NOT NULL,
    model_version       TEXT,
    transcript_text     TEXT,
    stage               TEXT NOT NULL DEFAULT 'selected'
                        CHECK (stage IN ('selected','rendered','checked','ready','held','rejected','published')),
    hold_reason         TEXT,
    review_state        TEXT NOT NULL DEFAULT 'pending' CHECK (review_state IN ('pending','approved','rejected','not_required')),
    preapproval_state   TEXT NOT NULL DEFAULT 'not_required' CHECK (preapproval_state IN ('not_required','pending','approved','rejected')),
    preapproval_evidence TEXT,
    render_path         TEXT,
    render_hash         TEXT,
    subtitle_path       TEXT,
    public_token        TEXT UNIQUE,
    check_results       TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (source_id, start_s, end_s, template_version)
);
CREATE UNIQUE INDEX clips_render_hash ON clips(render_hash) WHERE render_hash IS NOT NULL AND stage != 'rejected';

CREATE TABLE publications (
    id                  INTEGER PRIMARY KEY,
    clip_id             INTEGER NOT NULL REFERENCES clips(id),
    opportunity_id      TEXT NOT NULL REFERENCES opportunities(id),
    publisher           TEXT NOT NULL,
    account_ref         TEXT NOT NULL,
    intent_key          TEXT NOT NULL UNIQUE,
    dry_run             INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'reserved'
                        CHECK (status IN ('reserved','sending','accepted','sent','failed','unknown','cancelled')),
    provider_id         TEXT,
    provider_state      TEXT,             -- adapter-private JSON (e.g. Instagram container id)
    caption             TEXT NOT NULL,
    media_url           TEXT NOT NULL,
    disclosure          TEXT NOT NULL DEFAULT '{}',
    request_started_at  TEXT,
    sent_at             TEXT,
    sent_at_estimated   INTEGER NOT NULL DEFAULT 0,
    live_url            TEXT UNIQUE,
    status_checks       INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
-- one live (non-failed) publication per clip per account: no duplicate posts
CREATE UNIQUE INDEX publications_clip_account ON publications(clip_id, account_ref)
    WHERE status NOT IN ('failed','cancelled');

CREATE TABLE operator_ready (
    id               INTEGER PRIMARY KEY,
    opportunity_id   TEXT NOT NULL REFERENCES opportunities(id),
    clip_id          INTEGER NOT NULL REFERENCES clips(id),
    checklist        TEXT NOT NULL,     -- JSON of the confirmations the owner ticked
    acknowledged_at  TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    used_at          TEXT,
    publication_id   INTEGER REFERENCES publications(id)
);

CREATE TABLE submissions (
    id               INTEGER PRIMARY KEY,
    opportunity_id   TEXT NOT NULL REFERENCES opportunities(id),
    publication_id   INTEGER NOT NULL UNIQUE REFERENCES publications(id),
    live_url         TEXT NOT NULL UNIQUE,
    campaign_url     TEXT,
    published_at     TEXT NOT NULL,
    published_at_estimated INTEGER NOT NULL DEFAULT 0,
    target_at        TEXT NOT NULL,
    deadline_at      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'manual_required'
                     CHECK (status IN ('manual_required','submitted','overdue','submitted_late','missed')),
    submitted_at     TEXT,
    receipt          TEXT,
    review_state     TEXT NOT NULL DEFAULT 'pending' CHECK (review_state IN ('pending','approved','rejected')),
    review_note      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE publish_slots (
    slot_key        TEXT PRIMARY KEY,   -- opportunity:YYYY-MM-DD:HH:MM
    opportunity_id  TEXT NOT NULL,
    publication_id  INTEGER,
    outcome         TEXT NOT NULL,      -- published / skipped:<reason>
    created_at      TEXT NOT NULL
);

CREATE TABLE ledger (
    id               INTEGER PRIMARY KEY,
    currency         TEXT NOT NULL CHECK (length(currency) = 3),
    amount_minor     INTEGER NOT NULL,
    event_type       TEXT NOT NULL CHECK (event_type IN (
                        'estimated_gross','pending','approved','settled','withdrawal',
                        'fee','operating_cost','fixed_commitment','adjustment','client_fee_invoiced')),
    opportunity_id   TEXT,
    clip_id          INTEGER,
    publication_id   INTEGER,
    external_id      TEXT,
    source_evidence  TEXT NOT NULL,
    effective_date   TEXT NOT NULL,
    note             TEXT,
    created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX ledger_external ON ledger(event_type, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE metrics (
    id               INTEGER PRIMARY KEY,
    publication_id   INTEGER REFERENCES publications(id),
    live_url         TEXT,
    platform_views   INTEGER,
    qualified_views  INTEGER,
    source           TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    external_key     TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL
);

CREATE TABLE operator_time (
    id              INTEGER PRIMARY KEY,
    opportunity_id  TEXT,
    minutes         INTEGER NOT NULL,
    activity        TEXT NOT NULL,
    work_date       TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE spend (
    id              INTEGER PRIMARY KEY,
    period          TEXT NOT NULL,     -- YYYY-MM
    category        TEXT NOT NULL,     -- claude / publisher / other
    optional        INTEGER NOT NULL DEFAULT 0,
    reserved_minor  INTEGER NOT NULL,  -- USD cents (worst case)
    actual_minor    INTEGER,           -- USD cents once known
    status          TEXT NOT NULL CHECK (status IN ('reserved','committed','released')),
    ref             TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE api_requests (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    purpose     TEXT NOT NULL,      -- create / status / housekeeping / metrics / setup
    status_code INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX api_requests_window ON api_requests(provider, channel, created_at);

CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY,
    queue         TEXT NOT NULL CHECK (queue IN ('heavy','light')),
    type          TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    dedupe_key    TEXT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed','dead','cancelled')),
    due_at        TEXT NOT NULL,
    lease_owner   TEXT,
    lease_until   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 5,
    last_error    TEXT,
    error_class   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX jobs_due ON jobs(queue, status, due_at);

CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    message     TEXT NOT NULL,
    dedupe_key  TEXT,
    delivered   INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX alerts_dedupe ON alerts(dedupe_key, created_at);

CREATE TABLE kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE capabilities (
    key          TEXT PRIMARY KEY,   -- e.g. buffer.automatic_publish
    status       TEXT NOT NULL CHECK (status IN ('unverified','verified','failed','not_supported')),
    evidence     TEXT,
    verified_at  TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
