"""
topic-scorer: Scores candidates and selects the day's topics.
Stage 1 stub: reads candidates, writes a selected record, exits.
"""

import os
import sqlite3
import sys

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def main():
    try:
        print("[topic-scorer] Starting...")
        conn = connect_db()
        cursor = conn.cursor()

        # Read candidates
        cursor.execute("SELECT id, historical_event_title FROM topics WHERE status = 'candidate'")
        candidates = cursor.fetchall()
        print(f"[topic-scorer] Found {len(candidates)} candidate(s)")

        if candidates:
            # Mark first candidate as selected for testing
            topic_id = candidates[0][0]
            cursor.execute(
                "UPDATE topics SET status = 'selected', topic_score = 0.85, updated_at = datetime('now') WHERE id = ?",
                (topic_id,),
            )
            conn.commit()
            print(f"[topic-scorer] Selected topic id={topic_id}")

        conn.close()
        print("[topic-scorer] Complete.")
    except Exception as e:
        print(f"[topic-scorer] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
