"""
voice-gen: Text-to-speech generation via licensed TTS API.
Stage 1 stub: connects to DB, reports success, exits.
"""

import os
import sqlite3
import sys

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")


def main():
    try:
        print("[voice-gen] Starting...")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.close()
        print("[voice-gen] DB connection verified. Stub complete.")
    except Exception as e:
        print(f"[voice-gen] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
