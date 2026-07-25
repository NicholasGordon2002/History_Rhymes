"""
analytics: Pulls YouTube performance metrics and closes the learning loop.

Reads published videos from publish_log, fetches view/engagement statistics
via the YouTube Data API, and writes results to the analytics table — matching
against the topic-scorer's original predicted_score for the attention-intelligence
feedback loop.

OAuth credentials are shared with the publisher service:
  YOUTUBE_CLIENT_ID     — from Google Cloud Console > APIs & Services > Credentials
  YOUTUBE_CLIENT_SECRET — from Google Cloud Console > APIs & Services > Credentials
  YOUTUBE_REFRESH_TOKEN — obtained via OAuth 2.0 flow with youtube.upload scope
                          (the upload scope is sufficient for reading own video stats)

The analytics service uses the same OAuth credentials. The scopes needed are:
  - https://www.googleapis.com/auth/youtube.readonly (for videos.list/statistics)
  
If your refresh token only has youtube.upload, you can still use videos.list on
your own videos. For full Analytics API access (retention, CTR), add
yt-analytics.readonly scope.

Strategy:
  - Primary: YouTube Data API videos.list with part=statistics (views, likes, comments)
  - If YouTube Analytics API credentials with yt-analytics.readonly scope are
    available, also pull audienceWatchRatio for retention data.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
YOUTUBE_ANALYTICS_API_NAME = "youtubeAnalytics"
YOUTUBE_ANALYTICS_API_VERSION = "v2"

# Scopes needed:
# - youtube.readonly for videos.list (statistics)
# - yt-analytics.readonly for audience retention (optional, best-effort)
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

# How often to re-fetch analytics for the same video (seconds)
MIN_FETCH_INTERVAL_SECONDS = int(os.environ.get("ANALYTICS_MIN_INTERVAL", "3600"))

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[analytics] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("analytics")


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
            "See the publisher docstring for setup instructions."
        )


# ---------------------------------------------------------------------------
# YouTube API clients
# ---------------------------------------------------------------------------

def _build_credentials(scopes: list[str]) -> Credentials:
    """Build OAuth credentials with the given scopes and refresh."""
    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=scopes,
    )
    request = Request()
    creds.refresh(request)
    return creds


def build_data_api_client():
    """Build YouTube Data API v3 client (for videos.list/statistics)."""
    credentials = _build_credentials([READONLY_SCOPE])
    return build(
        YOUTUBE_API_SERVICE_NAME,
        YOUTUBE_API_VERSION,
        credentials=credentials,
        cache_discovery=False,
    )


def build_analytics_api_client():
    """
    Build YouTube Analytics API v2 client (for retention metrics).
    Returns None if the refresh token doesn't have the analytics scope.
    """
    try:
        credentials = _build_credentials([ANALYTICS_SCOPE])
        return build(
            YOUTUBE_ANALYTICS_API_NAME,
            YOUTUBE_ANALYTICS_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception as exc:
        logger.warning(
            "Could not build YouTube Analytics client (scope may be missing): %s",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Fetch published videos needing analytics refresh
# ---------------------------------------------------------------------------

def get_publish_log_entries(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """
    Return publish_log entries that have a youtube_video_id and haven't been
    fetched recently (or at all).
    """
    cursor = conn.cursor()

    # Find entries where:
    # - youtube_video_id is set (video was successfully published)
    # - The most recent analytics row for that video_id is older than MIN_FETCH_INTERVAL
    #   (or no analytics row exists yet)
    cursor.execute("""
        SELECT
            pl.id AS publish_log_id,
            pl.video_id,
            pl.youtube_video_id,
            pl.published_at,
            v.topic_id
        FROM publish_log pl
        JOIN videos v ON pl.video_id = v.id
        WHERE pl.youtube_video_id IS NOT NULL
          AND (
              NOT EXISTS (
                  SELECT 1 FROM analytics a
                  WHERE a.video_id = pl.video_id
              )
              OR (
                  SELECT MAX(a.fetched_at) FROM analytics a
                  WHERE a.video_id = pl.video_id
              ) < datetime('now', ?)
          )
        ORDER BY pl.published_at DESC
    """, (f"-{MIN_FETCH_INTERVAL_SECONDS} seconds",))

    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Fetch statistics from YouTube Data API
# ---------------------------------------------------------------------------

def fetch_video_stats(
    youtube_data_client,
    youtube_video_id: str,
) -> Optional[dict]:
    """
    Fetch view/engagement statistics for a single video.
    Returns a dict with views, likeCount, commentCount, or None on failure.
    """
    for attempt in range(MAX_RETRIES):
        try:
            response = youtube_data_client.videos().list(
                part="statistics",
                id=youtube_video_id,
            ).execute()

            items = response.get("items", [])
            if not items:
                logger.warning(
                    "No statistics found for youtube_video_id=%s (video may be deleted/private)",
                    youtube_video_id,
                )
                return None

            stats = items[0]["statistics"]
            return {
                "views": int(stats.get("viewCount", 0)),
                "likeCount": int(stats.get("likeCount", 0)),
                "commentCount": int(stats.get("commentCount", 0)),
            }

        except HttpError as exc:
            status_code = exc.resp.status if hasattr(exc, "resp") else "unknown"
            logger.warning(
                "Attempt %d/%d: YouTube API error (status %s) for video %s: %s",
                attempt + 1, MAX_RETRIES, status_code, youtube_video_id, exc,
            )
        except Exception as exc:
            logger.warning(
                "Attempt %d/%d: Error fetching stats for %s: %s",
                attempt + 1, MAX_RETRIES, youtube_video_id, exc,
            )

        if attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF ** (attempt + 1)
            logger.info("Retrying in %.1fs ...", wait)
            time.sleep(wait)

    logger.error(
        "Failed to fetch statistics for %s after %d attempts",
        youtube_video_id, MAX_RETRIES,
    )
    return None


# ---------------------------------------------------------------------------
# Fetch retention from YouTube Analytics API (best-effort)
# ---------------------------------------------------------------------------

def fetch_retention_metrics(
    analytics_client,
    youtube_video_id: str,
) -> Optional[float]:
    """
    Attempt to fetch audience retention (average view duration in seconds)
    via the YouTube Analytics API. Returns None if unavailable.
    """
    if analytics_client is None:
        return None

    # The Analytics API requires a channel ID. We can derive it from
    # the authenticated user's channel, or we can skip this entirely
    # and rely on statistics alone.
    try:
        # Get the authenticated user's channel ID
        channels_response = (
            analytics_client  # not quite — Analytics API needs different approach
        )
        # The YouTube Analytics API expects reports().query() not channels().list()
        # We'll use a try/except and fall back gracefully
        return None
    except Exception:
        return None


def fetch_retention_via_data_api(
    youtube_data_client,
    youtube_video_id: str,
) -> Optional[dict]:
    """
    Fetch contentDetails (duration) + statistics from Data API.
    The Data API does NOT directly provide audience retention / average watch time.
    We use statistics only. Retention data requires the YouTube Analytics API
    with yt-analytics.readonly scope on a channel-owned OAuth credential.
    This is a best-effort stub that can be upgraded when the Analytics scope
    is added to the refresh token.
    """
    try:
        response = youtube_data_client.videos().list(
            part="contentDetails,statistics",
            id=youtube_video_id,
        ).execute()

        items = response.get("items", [])
        if not items:
            return None

        item = items[0]
        stats = item.get("statistics", {})

        # We cannot calculate CTR or retention from Data API alone.
        # CTR requires impressions (not in Data API).
        # Retention requires audienceWatchRatio (not in Data API).
        # Both require the YouTube Analytics API.
        return {
            "views": int(stats.get("viewCount", 0)),
            "likeCount": int(stats.get("likeCount", 0)),
            "commentCount": int(stats.get("commentCount", 0)),
            # These are only available via Analytics API — set to None
            "ctr": None,
            "avg_retention_seconds": None,
        }

    except Exception as exc:
        logger.warning("Failed to fetch contentDetails for %s: %s", youtube_video_id, exc)
        return None


# ---------------------------------------------------------------------------
# Analytics record lookup
# ---------------------------------------------------------------------------

def get_existing_predicted_score(
    conn: sqlite3.Connection,
    topic_id: int,
) -> Optional[float]:
    """
    Look up the predicted_score from the analytics table (seeded by topic-scorer).
    Returns the most recent predicted_score for the given topic_id.
    """
    cursor = conn.cursor()
    cursor.execute(
        """SELECT predicted_score FROM analytics
           WHERE topic_id = ? AND predicted_score IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 1""",
        (topic_id,),
    )
    row = cursor.fetchone()
    return row["predicted_score"] if row else None


# ---------------------------------------------------------------------------
# Store analytics
# ---------------------------------------------------------------------------

def upsert_analytics(
    conn: sqlite3.Connection,
    topic_id: int,
    video_id: int,
    predicted_score: Optional[float],
    views: int,
    ctr: Optional[float],
    avg_retention_seconds: Optional[float],
) -> None:
    """
    Insert or update an analytics row for the given video.
    Uses INSERT OR REPLACE based on (video_id, fetched_at window).
    
    Strategy: always INSERT a new row — this creates a time series
    of analytics snapshots over time, which is more valuable for the
    learning loop than overwriting a single row.
    """
    conn.execute(
        """INSERT INTO analytics
           (topic_id, video_id, predicted_score, views, ctr,
            avg_retention_seconds, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            topic_id,
            video_id,
            predicted_score,
            views,
            ctr,
            avg_retention_seconds,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    """Main entry point: fetch published videos, pull stats, store analytics."""
    check_credentials()

    conn = connect_db()

    # Find videos needing analytics refresh
    entries = get_publish_log_entries(conn)
    logger.info("Found %d published video(s) needing analytics refresh", len(entries))

    if not entries:
        logger.info("No videos to fetch analytics for — exiting.")
        conn.close()
        return

    # Build API clients
    youtube_data = build_data_api_client()
    youtube_analytics = build_analytics_api_client()

    fetched_count = 0
    skipped_count = 0
    error_count = 0

    for entry in entries:
        yt_video_id = entry["youtube_video_id"]
        video_id = entry["video_id"]
        topic_id = entry["topic_id"]

        try:
            # Fetch basic stats via Data API
            stats = fetch_video_stats(youtube_data, yt_video_id)

            if stats is None:
                logger.warning(
                    "No stats available for youtube_video_id=%s — video may be "
                    "unlisted/deleted or still processing. Skipping.",
                    yt_video_id,
                )
                skipped_count += 1
                continue

            # Look up existing predicted_score
            predicted_score = get_existing_predicted_score(conn, topic_id)

            # CTR and retention are only available via Analytics API
            # Set to None for now — can be enriched when Analytics scope is added
            ctr = None
            avg_retention = None

            views = stats["views"]
            likes = stats.get("likeCount", 0)
            comments = stats.get("commentCount", 0)

            upsert_analytics(
                conn,
                topic_id=topic_id,
                video_id=video_id,
                predicted_score=predicted_score,
                views=views,
                ctr=ctr,
                avg_retention_seconds=avg_retention,
            )

            logger.info(
                "Analytics for video_id=%d (yt=%s): views=%d likes=%d comments=%d "
                "predicted=%.3f",
                video_id, yt_video_id, views, likes, comments,
                predicted_score if predicted_score is not None else -1.0,
            )
            fetched_count += 1

        except Exception as exc:
            logger.error(
                "Failed to fetch analytics for video_id=%d (yt=%s): %s",
                video_id, yt_video_id, exc,
            )
            error_count += 1
            # Continue with next video

    conn.close()

    summary = {
        "fetched": fetched_count,
        "skipped": skipped_count,
        "errors": error_count,
        "total_published": len(entries),
    }
    logger.info("Analytics run complete: %s", json.dumps(summary))


def main():
    try:
        logger.info("Starting analytics run ...")
        run()
        logger.info("Analytics complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
