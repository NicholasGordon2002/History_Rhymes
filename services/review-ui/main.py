"""
review-ui: FastAPI dashboard for human review of draft videos.
Stage 1: serves /health endpoint on 0.0.0.0:8000.
"""

import os
import sqlite3

from fastapi import FastAPI

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
HOST = os.environ.get("REVIEW_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("REVIEW_UI_PORT", "8000"))

app = FastAPI(title="History Rhymes — Review UI")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/health")
def health_check():
    """Health check endpoint — verifies DB connectivity."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [row["name"] for row in cursor.fetchall()]
        conn.close()
        return {
            "status": "healthy",
            "database": DB_PATH,
            "tables": tables,
            "table_count": len(tables),
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": DB_PATH,
            "error": str(e),
        }


if __name__ == "__main__":
    import uvicorn

    print(f"[review-ui] Starting on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
