"""
review-ui: FastAPI dashboard for human review of draft videos.
Stage 8: full review dashboard — list pending, review detail, approve/reject.
"""

import os
import sqlite3
from datetime import datetime

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from jinja2 import Environment, FileSystemLoader

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
VIDEOS_DIR = os.environ.get("VIDEOS_DIR", "/data/videos")
HOST = os.environ.get("REVIEW_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("REVIEW_UI_PORT", "8000"))

app = FastAPI(title="History Rhymes — Review UI")

# Jinja2 templates using direct Environment (avoids Starlette Jinja2Templates cache issue)
_templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
jinja_env = Environment(loader=FileSystemLoader(_templates_dir), autoescape=True)


def render_template(name: str, context: dict) -> HTMLResponse:
    """Render a Jinja2 template and return an HTMLResponse."""
    template = jinja_env.get_template(name)
    html = template.render(**context)
    return HTMLResponse(content=html)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def format_date(date_str):
    """Format an ISO date string for display."""
    if not date_str:
        return "—"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return date_str


def format_duration(seconds):
    """Format seconds as MM:SS."""
    if seconds is None:
        return "—"
    try:
        s = float(seconds)
        m, s = divmod(int(s), 60)
        return f"{m}:{s:02d}"
    except (ValueError, TypeError):
        return str(seconds)


def script_preview(text, max_chars=100):
    """Truncate script text for preview display."""
    if not text:
        return "(no script)"
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def filename_from_path(file_path):
    """Extract just the filename from a full path."""
    if not file_path:
        return None
    return os.path.basename(file_path)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """List all videos pending review."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            SELECT v.id AS video_id, v.topic_id, v.file_path, v.duration_seconds,
                   v.review_status, v.created_at AS video_created_at,
                   t.historical_event_title, t.modern_event_title
            FROM videos v
            JOIN topics t ON t.id = v.topic_id
            WHERE v.review_status = 'pending'
            ORDER BY v.created_at DESC
            """
        )
        pending_videos = cursor.fetchall()
    finally:
        conn.close()

    # Enrich with script preview
    video_list = []
    conn2 = get_db_connection()
    try:
        for row in pending_videos:
            script_cursor = conn2.execute(
                "SELECT script_text FROM scripts WHERE topic_id = ? ORDER BY id DESC LIMIT 1",
                (row["topic_id"],),
            )
            script_row = script_cursor.fetchone()
            preview = script_preview(script_row["script_text"] if script_row else None)

            video_list.append({
                "video_id": row["video_id"],
                "topic_id": row["topic_id"],
                "historical_title": row["historical_event_title"] or "Untitled",
                "modern_title": row["modern_event_title"] or "",
                "script_preview": preview,
                "duration": format_duration(row["duration_seconds"]),
                "created_at": format_date(row["video_created_at"]),
                "file_path": row["file_path"],
            })
    finally:
        conn2.close()

    return render_template(
        "dashboard.html",
        {
            "request": request,
            "videos": video_list,
            "count": len(video_list),
        },
    )


@app.get("/review/{video_id}", response_class=HTMLResponse)
def review_detail(request: Request, video_id: int):
    """Review detail page for a single video."""
    conn = get_db_connection()
    try:
        # Get video + topic
        cursor = conn.execute(
            """
            SELECT v.id AS video_id, v.topic_id, v.file_path, v.duration_seconds,
                   v.review_status, v.review_notes, v.created_at AS video_created_at,
                   t.historical_event_title, t.historical_event_description,
                   t.modern_event_title, t.modern_event_description,
                   t.historical_event_date, t.pairing_rationale, t.status
            FROM videos v
            JOIN topics t ON t.id = v.topic_id
            WHERE v.id = ?
            """,
            (video_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Video not found")

        # Get script
        script_cursor = conn.execute(
            "SELECT script_text, segments_json, structure_notes FROM scripts WHERE topic_id = ? ORDER BY id DESC LIMIT 1",
            (row["topic_id"],),
        )
        script_row = script_cursor.fetchone()

        # Get sources grouped by source_type
        sources_cursor = conn.execute(
            """
            SELECT source_type, source_name, source_url, fact_text, citation_text
            FROM sources WHERE topic_id = ?
            ORDER BY source_type, id
            """,
            (row["topic_id"],),
        )
        all_sources = sources_cursor.fetchall()
        historical_sources = [s for s in all_sources if s["source_type"] == "historical"]
        modern_sources = [s for s in all_sources if s["source_type"] == "modern"]

        # Get visual assets
        assets_cursor = conn.execute(
            """
            SELECT script_segment_index, asset_url, asset_source, license_tier, attribution_text
            FROM visual_assets WHERE topic_id = ?
            ORDER BY script_segment_index
            """,
            (row["topic_id"],),
        )
        visual_assets = assets_cursor.fetchall()

    finally:
        conn.close()

    video_filename = filename_from_path(row["file_path"])
    has_video_file = video_filename and os.path.isfile(
        os.path.join(VIDEOS_DIR, video_filename)
    )

    return render_template(
        "review.html",
        {
            "request": request,
            "video_id": row["video_id"],
            "topic_id": row["topic_id"],
            "historical_title": row["historical_event_title"] or "Untitled",
            "modern_title": row["modern_event_title"] or "",
            "historical_date": row["historical_event_date"] or "",
            "pairing_rationale": row["pairing_rationale"] or "",
            "video_filename": video_filename,
            "has_video_file": has_video_file,
            "duration": format_duration(row["duration_seconds"]),
            "duration_seconds": row["duration_seconds"],
            "created_at": format_date(row["video_created_at"]),
            "review_status": row["review_status"],
            "review_notes": row["review_notes"] or "",
            "script_text": script_row["script_text"] if script_row else "",
            "segments_json": script_row["segments_json"] if script_row else "",
            "structure_notes": script_row["structure_notes"] if script_row else "",
            "historical_sources": historical_sources,
            "modern_sources": modern_sources,
            "visual_assets": visual_assets,
        },
    )


@app.get("/videos/{filename}")
def serve_video(filename: str):
    """Serve a rendered video file from the videos directory."""
    # Security: only allow .mp4 files
    if not filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=404, detail="Not found")

    file_path = os.path.join(VIDEOS_DIR, filename)
    # Prevent path traversal
    real_path = os.path.realpath(file_path)
    real_videos = os.path.realpath(VIDEOS_DIR)
    if not real_path.startswith(real_videos):
        raise HTTPException(status_code=404, detail="Not found")

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Video file not found")

    return FileResponse(file_path, media_type="video/mp4")


@app.post("/api/approve/{video_id}")
def approve_video(video_id: int, review_notes: str = Form(default="")):
    """Approve a video for publishing."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE videos SET review_status = 'approved', review_notes = ? WHERE id = ?",
            (review_notes or "", video_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Video not found")

        # Also update topic status
        topic_cursor = conn.execute("SELECT topic_id FROM videos WHERE id = ?", (video_id,))
        topic_row = topic_cursor.fetchone()
        if topic_row:
            conn.execute(
                "UPDATE topics SET status = 'approved', updated_at = datetime('now') WHERE id = ?",
                (topic_row["topic_id"],),
            )
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.post("/api/reject/{video_id}")
def reject_video(video_id: int, review_notes: str = Form(default="")):
    """Reject a video."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE videos SET review_status = 'rejected', review_notes = ? WHERE id = ?",
            (review_notes or "", video_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Video not found")

        # Also update topic status
        topic_cursor = conn.execute("SELECT topic_id FROM videos WHERE id = ?", (video_id,))
        topic_row = topic_cursor.fetchone()
        if topic_row:
            conn.execute(
                "UPDATE topics SET status = 'rejected', updated_at = datetime('now') WHERE id = ?",
                (topic_row["topic_id"],),
            )
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(url="/", status_code=303)


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
