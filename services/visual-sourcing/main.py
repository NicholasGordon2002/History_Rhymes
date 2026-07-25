"""
visual-sourcing: Matches script segments to public-domain/licensed visuals.
Stage 1 stub: connects to DB, reports success, exits.
"""

import os
import sqlite3
import sys

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")


def main():
    try:
        print("[visual-sourcing] Starting...")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.close()
        print("[visual-sourcing] DB connection verified. Stub complete.")
    except Exception as e:
        print(f"[visual-sourcing] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
