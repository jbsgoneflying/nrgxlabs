-- NRGX Equity Repricing Lab — PIT store schema (migration v1).
--
-- Conventions:
--   * `*_at`  columns are UTC ISO-8601 timestamps ("2026-07-15T12:00:00Z").
--   * `*_date` / `*_session` columns are YYYY-MM-DD session dates.
--   * Every vendor-derived row carries `source`, `ingested_at`, and (where
--     meaningful) `available_at` — the earliest time NRGX could legitimately
--     have used the fact. `available_at` is the research clock.
--   * Applied by backend/repricing_lab/store.py; version tracked in
--     `schema_migration`. Statements must stay idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS schema_migration (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Instruments
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS instrument_master (
    instrument_id    TEXT PRIMARY KEY,          -- e.g. "eodhd:AAPL.US"
    symbol           TEXT NOT NULL,             -- current/last-known ticker
    exchange         TEXT,
    security_type    TEXT,
    country          TEXT,
    first_trade_date TEXT,
    last_trade_date  TEXT,
    delisted_at      TEXT,
    adr_flag         INTEGER NOT NULL DEFAULT 0,
    etf_flag         INTEGER NOT NULL DEFAULT 0,
    active_flag      INTEGER NOT NULL DEFAULT 1,
    source           TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instrument_symbol ON instrument_master(symbol);

CREATE TABLE IF NOT EXISTS symbol_map (
    instrument_id TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,                          -- NULL = current
    source        TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    PRIMARY KEY (instrument_id, symbol, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_symbol_map_symbol ON symbol_map(symbol);

-- ---------------------------------------------------------------------------
-- Prices and corporate actions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS daily_bar (
    instrument_id  TEXT NOT NULL,
    session_date   TEXT NOT NULL,
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,                         -- as-traded (raw)
    adjusted_close REAL,
    adj_factor     REAL,
    volume         REAL,
    ca_version     INTEGER NOT NULL DEFAULT 1,   -- bumped on re-adjustment
    source         TEXT NOT NULL,
    available_at   TEXT NOT NULL,
    ingested_at    TEXT NOT NULL,
    PRIMARY KEY (instrument_id, session_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar(session_date);

CREATE TABLE IF NOT EXISTS corporate_action (
    instrument_id     TEXT NOT NULL,
    action_type       TEXT NOT NULL,             -- split|dividend|symbol_change|delisting|merger
    effective_date    TEXT NOT NULL,
    announcement_date TEXT,
    ratio_or_amount   REAL,
    detail_json       TEXT,
    source            TEXT NOT NULL,
    available_at      TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    raw_uri           TEXT,
    content_hash      TEXT,
    PRIMARY KEY (instrument_id, action_type, effective_date)
);

-- ---------------------------------------------------------------------------
-- Universe
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS universe_snapshot (
    snapshot_date    TEXT NOT NULL,
    instrument_id    TEXT NOT NULL,
    universe_tier    TEXT NOT NULL,              -- tier1_liquid_core|tier2_satellite|tier3_short_eligible
    price            REAL,
    adv20_usd        REAL,
    adv60_usd        REAL,
    market_cap       REAL,
    eligible_long    INTEGER NOT NULL,
    eligible_short   INTEGER NOT NULL DEFAULT 0,
    exclusion_reasons TEXT,                      -- JSON array of reason codes
    builder_version  TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, instrument_id, universe_tier)
);
CREATE INDEX IF NOT EXISTS idx_universe_date_tier ON universe_snapshot(snapshot_date, universe_tier);

-- ---------------------------------------------------------------------------
-- Bronze (raw payload index; blobs live on disk under REPRICING_LAB_RAW_DIR)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw_payload (
    content_hash TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    params_json  TEXT NOT NULL,                  -- MUST exclude auth tokens
    retrieved_at TEXT NOT NULL,
    uri          TEXT NOT NULL                   -- data/lab_raw/{provider}/{yyyymm}/{hash}.json.gz
);

-- ---------------------------------------------------------------------------
-- Events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS event (
    event_id           TEXT PRIMARY KEY,         -- deterministic content-derived hash
    instrument_id      TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    event_subtype      TEXT,
    direction          TEXT,                     -- pos|neg|mixed|unknown
    title              TEXT,
    source             TEXT NOT NULL,
    source_document_id TEXT,
    effective_at       TEXT,
    published_at       TEXT,
    available_at       TEXT NOT NULL,
    session_bucket     TEXT NOT NULL,            -- premarket|regular|afterhours|nontrading
    decision_session   TEXT NOT NULL,            -- first session the event is actionable
    materiality        REAL,
    novelty            REAL,
    confidence         REAL,
    structured_json    TEXT,
    source_excerpt     TEXT,
    raw_uri            TEXT,
    content_hash       TEXT,
    llm_model          TEXT,                     -- NULL when fully deterministic
    llm_prompt_version TEXT,
    llm_validated      INTEGER,
    created_at         TEXT NOT NULL,
    revised_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_instrument ON event(instrument_id, decision_session);

CREATE TABLE IF NOT EXISTS event_cluster (
    cluster_id           TEXT PRIMARY KEY,
    instrument_id        TEXT NOT NULL,
    primary_event_id     TEXT NOT NULL,
    canonical_event_type TEXT NOT NULL,
    canonical_direction  TEXT,
    cluster_start        TEXT NOT NULL,
    cluster_end          TEXT NOT NULL,
    member_event_ids     TEXT NOT NULL,          -- JSON array
    dedup_method         TEXT NOT NULL,
    confidence           REAL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_cluster_instrument ON event_cluster(instrument_id);

CREATE TABLE IF NOT EXISTS earnings_event (
    instrument_id       TEXT NOT NULL,
    fiscal_period       TEXT,
    report_date         TEXT NOT NULL,
    timing              TEXT,                    -- bmo|amc|during|unknown
    available_at        TEXT NOT NULL,
    decision_session    TEXT NOT NULL,
    eps_actual          REAL,
    eps_estimate        REAL,
    eps_estimate_source TEXT,
    revenue_actual      REAL,
    revenue_estimate    REAL,
    estimate_is_pit     INTEGER NOT NULL DEFAULT 0,
    transcript_ref      TEXT,
    guidance_json       TEXT,
    source              TEXT NOT NULL,
    revision_version    INTEGER NOT NULL DEFAULT 1,
    content_hash        TEXT,
    ingested_at         TEXT NOT NULL,
    PRIMARY KEY (instrument_id, report_date, source)
);
CREATE INDEX IF NOT EXISTS idx_earnings_decision ON earnings_event(decision_session);

-- ---------------------------------------------------------------------------
-- Fundamentals / estimates (self-archived forward from first ingest)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS estimate_snapshot (
    instrument_id   TEXT NOT NULL,
    metric          TEXT NOT NULL,               -- eps|revenue
    fiscal_period   TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    consensus_value REAL,
    analyst_count   INTEGER,
    source          TEXT NOT NULL,
    available_at    TEXT NOT NULL,
    PRIMARY KEY (instrument_id, metric, fiscal_period, as_of, source)
);

CREATE TABLE IF NOT EXISTS fundamental_snapshot (
    instrument_id      TEXT NOT NULL,
    as_of              TEXT NOT NULL,
    shares_outstanding REAL,
    float_shares       REAL,
    market_cap         REAL,
    sector             TEXT,                     -- current classification; NOT point-in-time
    industry           TEXT,
    detail_json        TEXT,
    source             TEXT NOT NULL,
    available_at       TEXT NOT NULL,
    PRIMARY KEY (instrument_id, as_of, source)
);

-- ---------------------------------------------------------------------------
-- Gold: features, candidates, simulator audit, runs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS feature_snapshot (
    snapshot_id     TEXT PRIMARY KEY,            -- hash(instrument, as_of, feature_version)
    instrument_id   TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features_json   TEXT NOT NULL,               -- flat dict: name -> value | null
    quality_flags   TEXT,
    source_versions TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feature_instrument ON feature_snapshot(instrument_id, as_of);

CREATE TABLE IF NOT EXISTS research_candidate (
    candidate_id        TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    strategy_version    TEXT NOT NULL,
    archetype           TEXT NOT NULL,
    instrument_id       TEXT NOT NULL,
    side                TEXT NOT NULL,           -- long|short
    decision_time       TEXT NOT NULL,
    decision_session    TEXT NOT NULL,
    event_cluster_id    TEXT,
    feature_snapshot_id TEXT,
    entry_plan_json     TEXT NOT NULL,
    stop_plan_json      TEXT NOT NULL,
    reason_codes        TEXT,
    vetoes              TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_run ON research_candidate(run_id);

CREATE TABLE IF NOT EXISTS sim_order (
    run_id            TEXT NOT NULL,
    order_id          TEXT NOT NULL,
    candidate_id      TEXT,
    instrument_id     TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,             -- open|close|stop|limit|stop_gap
    submitted_session TEXT NOT NULL,
    filled_session    TEXT,
    intended_price    REAL,
    fill_price        REAL,
    shares            REAL,
    status            TEXT NOT NULL,             -- filled|partial|rejected_*|expired
    reject_reason     TEXT,
    PRIMARY KEY (run_id, order_id)
);

CREATE TABLE IF NOT EXISTS sim_position (
    run_id           TEXT NOT NULL,
    position_id      TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    instrument_id    TEXT NOT NULL,
    side             TEXT NOT NULL,
    entry_session    TEXT,
    entry_price      REAL,
    shares           REAL,
    stop_price       REAL,
    risk_per_share   REAL,
    planned_risk_pct REAL,
    exit_session     TEXT,
    exit_price       REAL,
    exit_reason      TEXT,
    realized_r       REAL,
    mfe_r            REAL,
    mae_r            REAL,
    holding_sessions INTEGER,
    lifecycle_json   TEXT,
    PRIMARY KEY (run_id, position_id)
);

CREATE TABLE IF NOT EXISTS research_run (
    run_id             TEXT PRIMARY KEY,         -- {yyyymmddHHMM}-{git_sha8}-{config_hash8}
    kind               TEXT NOT NULL,            -- backfill|qa|labels|bakeoff|benchmark|probe
    code_version       TEXT NOT NULL,            -- git SHA (or "unknown")
    config_json        TEXT NOT NULL,
    config_hash        TEXT NOT NULL,
    data_version       TEXT,
    feature_version    TEXT,
    strategy_version   TEXT,
    cost_model_version TEXT,
    seed               INTEGER,
    started_at         TEXT NOT NULL,
    completed_at       TEXT,
    status             TEXT NOT NULL,            -- running|ok|failed
    result_uri         TEXT
);

CREATE TABLE IF NOT EXISTS promotion_decision (
    decision_id      TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    archetype        TEXT NOT NULL,
    decision         TEXT NOT NULL,              -- promote|revise|kill|insufficient_data|shadow|live
    criteria_json    TEXT NOT NULL,
    decided_at       TEXT NOT NULL,
    decided_by       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_run (
    job_name    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    detail_json TEXT,
    PRIMARY KEY (job_name, started_at)
);
