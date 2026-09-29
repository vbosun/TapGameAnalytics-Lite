CREATE TABLE IF NOT EXISTS owner_game (
    developer_id INTEGER NOT NULL,
    app_id INTEGER NOT NULL,
    app_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    icon_url TEXT,
    icon_color TEXT,
    icon_mime_type TEXT,
    icon_data BLOB,
    icon_sha256 TEXT,
    icon_collected_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (developer_id, app_id)
);
CREATE TABLE IF NOT EXISTS owner_metric_daily (
    developer_id INTEGER NOT NULL,
    app_id INTEGER NOT NULL,
    stat_date TEXT NOT NULL,
    impressions INTEGER,
    store_clicks INTEGER,
    store_page_views INTEGER,
    store_conversions INTEGER,
    store_click_rate REAL,
    store_conversion_rate REAL,
    active_devices INTEGER,
    new_devices INTEGER,
    mini_app_start_rate REAL,
    estimated_ad_revenue REAL,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (developer_id, app_id, stat_date),
    FOREIGN KEY (developer_id, app_id)
        REFERENCES owner_game(developer_id, app_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_owner_metric_daily_date
    ON owner_metric_daily(stat_date DESC);
CREATE TABLE IF NOT EXISTS owner_daily_report (
    developer_id INTEGER NOT NULL,
    app_id INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    markdown TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (developer_id, app_id, report_date),
    FOREIGN KEY (developer_id, app_id)
        REFERENCES owner_game(developer_id, app_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS collect_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    target TEXT NOT NULL,
    req_total INTEGER DEFAULT 0,
    req_ok INTEGER DEFAULT 0,
    req_failed INTEGER DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
