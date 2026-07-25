"""
publisher: YouTube Data API upload for YouTube Shorts.

Reads approved videos from the shared DB, uploads them to YouTube via OAuth 2.0,
and logs the result in publish_log. Skips videos already published (idempotent).

OAuth credentials are supplied via environment variables:
  YOUTUBE_CLIENT_ID     — from Google Cloud Console > APIs & Services > Credentials
  YOUTUBE_CLIENT_SECRET — from Google Cloud Console > APIs & Services > Credentials
  YOUTUBE_REFRESH_TOKEN — obtained via the OAuth 2.0 playground or a one-time
                          auth flow; add https://www.googleapis.com/auth/youtube.upload
                          as the scope when generating it.

To get these credentials:
  1. Go to https://console.cloud.google.com/apis/credentials
  2. Create an OAuth 2.0 Client ID (Web application or Desktop)
  3. Use the OAuth 2.0 Playground (https://developers.google.com/oauthplayground)
     with scope "https://www.googleapis.com/auth/youtube.upload" to obtain a
     refresh token.
  4. Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, and YOUTUBE_REFRESH_TOKEN.
"""

import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from io import BufferedReader
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

CATEGORY_ID = 22  # People & Blogs — suitable for Shorts content
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, multiplied exponentially

# Synthetic content disclosure:
# YouTube requires disclosure ONLY for content that:
#   - Makes a real person appear to say or do something they didn't (deepfakes), OR
#   - Alters footage of real events/places to generate a realistic but fake scene.
# This project uses generic TTS (no real-voice cloning) and original/public-domain
# visuals only — so disclosure is NOT required per current YouTube policy.
# We store the decision per video for auditability.
SYNTHETIC_CONTENT_DISCLOSURE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[publisher] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("publisher")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def connect_db() -> sqlite3.Connection:
    """Open the shared SQLite database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------

def check_credentials() -> None:
    """Raise if any YouTube OAuth env var is missing or still a placeholder."""
    missing = []
    for var_name, var_val in [
        ("YOUTUBE_CLIENT_ID", YOUTUBE_CLIENT_ID),
        ("YOUTUBE_CLIENT_SECRET", YOUTUBE_CLIENT_SECRET),
        ("YOUTUBE_REFRESH_TOKEN", YOUTUBE_REFRESH_TOKEN),
    ]:
        if not var_val or var_val.startswith("placeholder"):
            missing.append(var_name)

    if missing:
        raise RuntimeError(
            "Missing YouTube OAuth credentials. "
            f"The following env vars are not set or are placeholders: {', '.join(missing)}. "
            "See the module docstring for setup instructions."
        )


# ---------------------------------------------------------------------------
# YouTube API client
# ---------------------------------------------------------------------------

def build_youtube_client():
    """
    Build an authenticated YouTube Data API v3 client using OAuth 2.0 with a
    refresh token. The access token is automatically refreshed if expired.
    """
    credentials = Credentials(
        token=None,  # will be populated by refresh
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=[YOUTUBE_UPLOAD_SCOPE],
    )

    # Force a refresh to get a fresh access token
    request = Request()
    credentials.refresh(request)

    return build(
        YOUTUBE_API_SERVICE_NAME,
        YOUTUBE_API_VERSION,
        credentials=credentials,
        cache_discovery=False,
    )


# ---------------------------------------------------------------------------
# Approved video retrieval
# ---------------------------------------------------------------------------

def get_approved_videos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return videos that are approved for publishing but haven't been published yet.
    Joins videos with topics for title/description data.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            v.id AS video_id,
            v.topic_id,
            v.file_path,
            v.duration_seconds,
            v.review_status,
            t.historical_event_title,
            t.historical_event_description,
            t.modern_event_title,
            t.modern_event_description,
            t.pairing_rationale
        FROM videos v
        JOIN topics t ON v.topic_id = t.id
        WHERE v.review_status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM publish_log pl WHERE pl.video_id = v.id
          )
        ORDER BY v.created_at ASC
    """)
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Title / Description / Tags builders
# ---------------------------------------------------------------------------

def build_title(topic: sqlite3.Row) -> str:
    """
    Build a YouTube Shorts title combining historical and modern events.
    Format: "Historical Event vs Modern Event | #Shorts"
    Truncates to keep under YouTube's 100-char title limit.
    """
    historical = topic["historical_event_title"] or "On This Day"
    modern = topic["modern_event_title"] or "Today"

    # Truncate each part to fit combined title
    base = f"{historical} vs {modern}"
    if len(base) > 85:
        # Keep more of historical, truncate modern
        max_modern = 85 - len(historical) - 4  # " vs "
        if max_modern < 10:
            base = historical[:82] + "..."
        else:
            base = f"{historical} vs {modern[:max_modern]}..."

    title = f"{base} | #Shorts"
    # Hard cap at 100 characters
    if len(title) > 100:
        title = title[:97] + "..."

    return title


def build_description(topic: sqlite3.Row) -> str:
    """
    Build a YouTube description with the pairing rationale and standard footer.
    Includes source citations from the sources table.
    """
    hist_title = topic["historical_event_title"] or "This day in history"
    modern_title = topic["modern_event_title"] or "today's headlines"
    rationale = topic["pairing_rationale"] or ""
    hist_desc = (topic["historical_event_description"] or "")[:300]
    modern_desc = (topic["modern_event_description"] or "")[:300]

    parts = [
        f"On this day: {hist_title}",
        "",
    ]
    if hist_desc.strip():
        parts.append(hist_desc.strip())
        parts.append("")

    parts.append(f"Today: {modern_title}")
    if modern_desc.strip():
        parts.append("")
        parts.append(modern_desc.strip())

    if rationale.strip():
        parts.append("")
        parts.append(f"The rhyme: {rationale.strip()}")

    parts.append("")
    parts.append("---")
    parts.append(
        "History doesn't repeat, but it rhymes. "
        "New #Shorts every day pairing the past with the present."
    )
    parts.append("")
    parts.append("#history #shorts #onthisday #historyrhymes")

    description = "\n".join(parts)
    # YouTube's description limit is 5000 chars; we won't come close
    return description


def build_tags(topic: sqlite3.Row) -> list[str]:
    """Build a tag list for the video based on topic keywords."""
    tags = ["history", "shorts", "onthisday", "historyrhymes"]

    hist_title = (topic["historical_event_title"] or "").lower()
    modern_title = (topic["modern_event_title"] or "").lower()

    # Extract potential keyword tags from titles (simple word-split approach)
    for phrase in [hist_title, modern_title]:
        for word in phrase.split():
            word = word.strip(".,;:!?\"'()[]{}").lower()
            if len(word) > 3 and word not in tags:
                tags.append(word)

    # Cap at YouTube's limit of ~500 characters total
    # We aim for 15-20 meaningful tags max
    return tags[:20]


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_video(
    youtube_client,
    video_row: sqlite3.Row,
    conn: sqlite3.Connection,
) -> Optional[dict]:
    """
    Upload a single video to YouTube as a Short.

    Returns a dict with youtube_video_id and youtube_url on success,
    or None if the video file is missing/invalid.
    """
    video_path = video_row["file_path"]
    if not video_path or not os.path.exists(video_path):
        logger.error(
            "Video file not found for video_id=%d: %s",
            video_row["video_id"], video_path,
        )
        return None

    title = build_title(video_row)
    description = build_description(video_row)
    tags = build_tags(video_row)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(CATEGORY_ID),
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    logger.info(
        "Uploading video_id=%d: %s (file: %s, size: %d bytes)",
        video_row["video_id"],
        title,
        video_path,
        os.path.getsize(video_path),
    )

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=256 * 1024,  # 256 KB chunks
    )

    request = youtube_client.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = request.execute()
            break
        except HttpError as exc:
            last_error = exc
            status_code = exc.resp.status if hasattr(exc, "resp") else "unknown"
            logger.warning(
                "Attempt %d/%d: YouTube API error (status %s): %s",
                attempt + 1, MAX_RETRIES, status_code, exc,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Attempt %d/%d: Upload error: %s",
                attempt + 1, MAX_RETRIES, exc,
            )

        if attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF ** (attempt + 1)
            logger.info("Retrying in %.1fs ...", wait)
            time.sleep(wait)

    if response is None:
        raise RuntimeError(
            f"Failed to upload video_id={video_row['video_id']} "
            f"after {MAX_RETRIES} attempts. Last error: {last_error}"
        )

    youtube_video_id = response["id"]
    youtube_url = f"https://www.youtube.com/shorts/{youtube_video_id}"

    logger.info(
        "Published video_id=%d → %s",
        video_row["video_id"], youtube_url,
    )

    return {
        "youtube_video_id": youtube_video_id,
        "youtube_url": youtube_url,
    }


# ---------------------------------------------------------------------------
# Publish log
# ---------------------------------------------------------------------------

def log_publish(
    conn: sqlite3.Connection,
    video_id: int,
    youtube_video_id: str,
    youtube_url: str,
) -> None:
    """Insert a row into publish_log after successful upload."""
    conn.execute(
        """INSERT INTO publish_log
           (video_id, youtube_video_id, youtube_url, published_at,
            synthetic_content_disclosure)
           VALUES (?, ?, ?, datetime('now'), ?)""",
        (video_id, youtube_video_id, youtube_url, int(SYNTHETIC_CONTENT_DISCLOSURE)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    """Main entry point: find approved videos, upload, log."""
    check_credentials()

    conn = connect_db()

    # Find approved, unpublished videos
    approved = get_approved_videos(conn)
    logger.info("Found %d approved video(s) ready for publishing", len(approved))

    if not approved:
        logger.info("No approved videos to publish — exiting.")
        conn.close()
        return

    # Build authenticated YouTube client
    youtube = build_youtube_client()

    published_count = 0
    skipped_count = 0
    error_count = 0

    for video_row in approved:
        try:
            result = upload_video(youtube, video_row, conn)
            if result is None:
                skipped_count += 1
                continue

            log_publish(
                conn,
                video_row["video_id"],
                result["youtube_video_id"],
                result["youtube_url"],
            )
            published_count += 1

        except Exception as exc:
            logger.error(
                "Failed to publish video_id=%d: %s",
                video_row["video_id"], exc,
            )
            error_count += 1
            # Continue with next video — don't let one failure block the batch

    conn.close()

    summary = {
        "published": published_count,
        "skipped": skipped_count,
        "errors": error_count,
        "total_approved": len(approved),
    }
    logger.info("Publisher run complete: %s", json.dumps(summary))


def main():
    try:
        logger.info("Starting publisher run ...")
        run()
        logger.info("Publisher complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
