"""
db-init: One-shot service that creates the SQLite database and runs schema migrations.
Exits after completion.
"""

import os
import sqlite3
import sys

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    historical_event_date TEXT NOT NULL,
    historical_event_title TEXT NOT NULL,
    historical_event_description TEXT,
    modern_event_title TEXT,
    modern_event_description TEXT,
    pairing_rationale TEXT,
    google_trends_momentum REAL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate', 'selected', 'in_progress', 'draft', 'approved', 'rejected', 'published')),
    topic_score REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    source_type TEXT NOT NULL CHECK(source_type IN ('historical', 'modern')),
    source_name TEXT NOT NULL,
    source_url TEXT,
    fact_text TEXT NOT NULL,
    citation_text TEXT NOT NULL,
    retrieved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    script_text TEXT NOT NULL,
    segments_json TEXT,
    structure_notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS visual_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    script_segment_index INTEGER NOT NULL,
    asset_url TEXT,
    asset_source TEXT NOT NULL,
    license_tier TEXT NOT NULL CHECK(license_tier IN ('public_domain', 'CC0', 'licensed_stock')),
    attribution_text TEXT,
    retrieved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    file_path TEXT,
    duration_seconds REAL,
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending', 'approved', 'rejected')),
    review_notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS publish_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    youtube_video_id TEXT,
    youtube_url TEXT,
    published_at TEXT,
    synthetic_content_disclosure INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    video_id INTEGER REFERENCES videos(id),
    predicted_score REAL,
    views INTEGER DEFAULT 0,
    ctr REAL DEFAULT 0.0,
    avg_retention_seconds REAL DEFAULT 0.0,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
CREATE INDEX IF NOT EXISTS idx_topics_historical_event_date ON topics(historical_event_date);
CREATE INDEX IF NOT EXISTS idx_sources_topic_id ON sources(topic_id);
CREATE INDEX IF NOT EXISTS idx_scripts_topic_id ON scripts(topic_id);
CREATE INDEX IF NOT EXISTS idx_visual_assets_topic_id ON visual_assets(topic_id);
CREATE INDEX IF NOT EXISTS idx_videos_topic_id ON videos(topic_id);
CREATE INDEX IF NOT EXISTS idx_videos_review_status ON videos(review_status);
CREATE INDEX IF NOT EXISTS idx_publish_log_video_id ON publish_log(video_id);
CREATE INDEX IF NOT EXISTS idx_analytics_topic_id ON analytics(topic_id);
CREATE INDEX IF NOT EXISTS idx_analytics_video_id ON analytics(video_id);
"""


def init_db():
    """Create the database directory and run schema migrations."""
    db_dir = os.path.dirname(DB_PATH)
    os.makedirs(db_dir, exist_ok=True)

    print(f"[db-init] Initializing database at {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Enable WAL mode for better concurrent read performance
    cursor.execute("PRAGMA journal_mode=WAL;")
    # Enable foreign keys
    cursor.execute("PRAGMA foreign_keys=ON;")

    # Run schema
    cursor.executescript(SCHEMA)
    conn.commit()

    # Verify tables were created
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"[db-init] Created tables: {tables}")

    conn.close()
    print("[db-init] Database initialization complete.")


def main():
    try:
        init_db()
    except Exception as e:
        print(f"[db-init] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
