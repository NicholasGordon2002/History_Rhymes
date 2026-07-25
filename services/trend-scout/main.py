"""
trend-scout: Polls historical/trend sources for candidate pairings.
Stage 1 stub: connects to DB, writes a test candidate record, exits.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def insert_test_candidate(conn):
    cursor = conn.cursor()
    today = datetime.now(timezone.utc).strftime("%m-%d")
    cursor.execute(
        """
        INSERT INTO topics (
            historical_event_date, historical_event_title, historical_event_description,
            modern_event_title, modern_event_description, pairing_rationale,
            google_trends_momentum, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate')
        """,
        (
            f"2026-{today}",
            "Test Historical Event",
            "A test historical event description for pipeline validation.",
            "Test Modern Event",
            "A test modern event description for pipeline validation.",
            "Test pairing rationale — history rhymes.",
            0.75,
        ),
    )
    conn.commit()
    print(f"[trend-scout] Inserted test candidate topic (id={cursor.lastrowid})")


def main():
    try:
        print("[trend-scout] Starting...")
        conn = connect_db()
        insert_test_candidate(conn)
        conn.close()
        print("[trend-scout] Complete.")
    except Exception as e:
        print(f"[trend-scout] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
