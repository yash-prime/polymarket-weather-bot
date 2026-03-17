-- db/schema.sql — Full database schema for Polymarket Weather Bot
-- SQLite with WAL mode for concurrent dashboard reads + main.py writes.

PRAGMA journal_mode=WAL;

-- ─── Schema versioning ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ─── Market data ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS markets (
    id               TEXT PRIMARY KEY,
    question         TEXT NOT NULL,
    yes_price        REAL,
    end_date         TEXT,               -- ISO 8601 UTC timestamp
    volume           REAL,
    parsed           TEXT,               -- JSON string (LLM-parsed structured data)
    parse_status     TEXT DEFAULT 'pending',  -- pending|success|regex_fallback|failed
    resolution_risk  TEXT,               -- LOW|MEDIUM|HIGH|NULL
    last_seen        TEXT,               -- ISO 8601 UTC timestamp
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_markets_parse_status ON markets(parse_status);

-- ─── Signals ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id      TEXT NOT NULL,
    direction      TEXT NOT NULL,        -- YES | NO
    adjusted_edge  REAL NOT NULL,
    model_prob     REAL NOT NULL,
    market_price   REAL NOT NULL,
    raw_kelly_size REAL NOT NULL,        -- fraction of bankroll (0.0–1.0)
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_market_id ON signals(market_id);

-- ─── Live trades ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  TEXT NOT NULL,
    direction  TEXT NOT NULL,            -- YES | NO
    final_size REAL NOT NULL,            -- USDC amount
    price      REAL,
    order_id   TEXT,
    status     TEXT NOT NULL,            -- open|filled|cancelled|failed
    mode       TEXT NOT NULL DEFAULT 'live',
    rationale  TEXT,                     -- LLM commentary
    created_at TEXT DEFAULT (datetime('now')),
    closed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_status    ON trades(status);

-- ─── Paper trades ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_trades (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id            TEXT NOT NULL,
    direction            TEXT NOT NULL,
    final_size           REAL NOT NULL,
    simulated_fill_price REAL,
    status               TEXT NOT NULL,  -- open|filled|cancelled
    created_at           TEXT DEFAULT (datetime('now')),
    closed_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_market_id ON paper_trades(market_id);

-- ─── Live positions ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL UNIQUE,
    direction       TEXT NOT NULL,
    size            REAL NOT NULL,
    entry_price     REAL NOT NULL,
    current_price   REAL,
    unrealized_pnl  REAL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open|closed
    opened_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

-- ─── Paper positions ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL UNIQUE,
    direction       TEXT NOT NULL,
    size            REAL NOT NULL,
    entry_price     REAL NOT NULL,
    current_price   REAL,
    unrealized_pnl  REAL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open|closed
    opened_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status);

-- ─── Portfolio snapshots ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mode             TEXT NOT NULL,      -- live | paper
    total_equity     REAL,
    unrealized_pnl   REAL,
    realized_pnl     REAL,
    daily_pnl        REAL,
    daily_loss_pct   REAL,
    open_positions   INTEGER,
    snapshot_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_mode ON portfolio_snapshots(mode);

-- ─── Calibration weights ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calibration_weights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    region      TEXT NOT NULL,
    season      TEXT NOT NULL,
    brier_score REAL NOT NULL,
    weight      REAL NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(source, region, season)
);

-- ─── ECMWF batch snapshots ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ecmwf_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    metric        TEXT NOT NULL,
    forecast_date TEXT NOT NULL,         -- ISO 8601 UTC
    value         REAL NOT NULL,
    ingested_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ecmwf_snapshots_location ON ecmwf_snapshots(lat, lon, metric, forecast_date);

-- ─── API response cache ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_cache (
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,           -- JSON-encoded response
    fetched_at  TEXT NOT NULL,           -- ISO 8601 UTC
    ttl_seconds INTEGER NOT NULL,
    PRIMARY KEY (source, key)
);

CREATE INDEX IF NOT EXISTS idx_api_cache_source ON api_cache(source);

-- ─── LLM parse cache ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_cache (
    question_hash TEXT PRIMARY KEY,      -- sha256(question)
    question      TEXT NOT NULL,
    parsed        TEXT,                  -- JSON string
    parse_status  TEXT NOT NULL,         -- success|regex_fallback|failed
    model         TEXT,                  -- which model produced this result
    created_at    TEXT DEFAULT (datetime('now'))
);

-- ─── System configuration ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Default config rows (idempotent)
INSERT OR IGNORE INTO system_config VALUES ('bot_halted',          'false', datetime('now'));
INSERT OR IGNORE INTO system_config VALUES ('trading_mode',        'paper', datetime('now'));
INSERT OR IGNORE INTO system_config VALUES ('min_edge_threshold',  '0.08',  datetime('now'));
INSERT OR IGNORE INTO system_config VALUES ('max_position_usdc',   '50.0',  datetime('now'));

-- ─── Per-market overrides ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_overrides (
    market_id  TEXT PRIMARY KEY,
    action     TEXT NOT NULL,            -- skip | force
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ─── Record initial schema version ───────────────────────────────────────────
INSERT OR IGNORE INTO schema_version VALUES (1, datetime('now'));
