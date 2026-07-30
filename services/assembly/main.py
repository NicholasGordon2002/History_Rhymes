"""
assembly: Combines narration audio + sourced visuals + text overlays into
a rendered YouTube Shorts video via ffmpeg.

For each topic with status='draft' that has:
  - A script with segments_json in the scripts table
  - Visual assets in the visual_assets table
  - An MP3 audio file at /data/audio/{topic_id}.mp3

  1. Loads the MP3 audio to get total duration
  2. Loads visual assets ordered by script_segment_index
  3. For each segment: creates a still-image frame with narration text overlay
  4. Concatenates all segments into one video
  5. Maps the narration audio as the video's audio track
  6. Outputs rendered MP4 to /data/videos/{topic_id}.mp4
  7. Inserts a row into the videos table with review_status='pending'

One-shot batch job: runs, assembles, stores, exits.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/data/history_rhymes.db")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
VIDEO_DIR = os.environ.get("VIDEO_DIR", "/data/videos")

# Output format: 1080x1920 (YouTube Shorts vertical)
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

# drawtext styling
FONT_SIZE = 52
FONT_COLOR = "white"
BOX_COLOR = "black@0.5"  # semi-transparent black background behind text
BOX_BORDER = 10
# Text wrapping: max characters per line at this font size on 1080-width
TEXT_MAX_CHARS_PER_LINE = 38

# Download timeout for image assets
DOWNLOAD_TIMEOUT = 20.0
# User-Agent for image downloads
USER_AGENT = (
    "HistoryRhymes/1.0 (https://github.com/NicholasGordon2002/History_Rhymes; "
    "assembly bot; contact via repo)"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[assembly] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("assembly")


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


def topic_has_video(conn: sqlite3.Connection, topic_id: int) -> bool:
    """Return True if this topic already has a video row (idempotency check)."""
    cursor = conn.execute(
        "SELECT COUNT(*) AS cnt FROM videos WHERE topic_id = ?", (topic_id,)
    )
    row = cursor.fetchone()
    return row["cnt"] > 0


def get_ready_topics(conn: sqlite3.Connection):
    """
    Return all topics with status='draft' that have:
      - a script with segments_json
      - visual_assets rows
    """
    cursor = conn.execute(
        """
        SELECT t.id AS topic_id, t.historical_event_title, t.modern_event_title,
               s.id AS script_id, s.segments_json, s.script_text
        FROM topics t
        JOIN scripts s ON s.topic_id = t.id
        WHERE t.status = 'draft'
          AND s.segments_json IS NOT NULL
          AND s.segments_json != ''
        ORDER BY t.id
        """
    )
    return cursor.fetchall()


def get_visual_assets(conn: sqlite3.Connection, topic_id: int) -> list[sqlite3.Row]:
    """Return visual assets for a topic, ordered by script_segment_index."""
    cursor = conn.execute(
        """
        SELECT * FROM visual_assets
        WHERE topic_id = ?
        ORDER BY script_segment_index
        """,
        (topic_id,),
    )
    return cursor.fetchall()


def insert_video(
    conn: sqlite3.Connection,
    topic_id: int,
    file_path: str,
    duration_seconds: float,
) -> int:
    """Insert a video row. Returns the new row id."""
    cursor = conn.execute(
        """
        INSERT INTO videos (topic_id, file_path, duration_seconds, review_status)
        VALUES (?, ?, ?, 'pending')
        """,
        (topic_id, file_path, round(duration_seconds, 2)),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def audio_file_path(topic_id: int) -> str:
    """Return the expected path for a topic's narration audio file."""
    return os.path.join(AUDIO_DIR, f"{topic_id}.mp3")


def get_audio_duration(audio_path: str) -> float:
    """
    Use ffprobe to get the duration of an audio file in seconds.
    Returns 0.0 on failure.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if result.returncode != 0:
            logger.warning("  ffprobe failed for %s: %s", audio_path, result.stderr.strip())
            return 0.0
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("  ffprobe error for %s: %s", audio_path, exc)
        return 0.0


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def download_image(url: str, dest_path: str) -> bool:
    """
    Download an image from url to dest_path.
    Returns True on success, False on failure.
    """
    if not url or not url.startswith("http"):
        return False
    try:
        resp = requests.get(
            url, timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
        resp.raise_for_status()
        # Check content-type is image
        ct = resp.headers.get("content-type", "")
        if not ct.startswith("image/"):
            logger.warning("    URL returned non-image content-type: %s", ct)
            return False
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except requests.RequestException as exc:
        logger.warning("    Download failed for %s: %s", url[:80], exc)
        return False


def create_placeholder_image(path: str, width: int, height: int) -> bool:
    """
    Create a black placeholder image using ffmpeg.
    Returns True on success.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}:d=0.1",
                "-frames:v", "1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("    Failed to create placeholder image: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def wrap_text(text: str, max_chars: int = TEXT_MAX_CHARS_PER_LINE) -> str:
    """
    Wrap text to fit within max_chars per line.
    Breaks at word boundaries when possible.
    """
    words = text.split()
    lines = []
    current_line = []

    for word in words:
        # Check if adding this word exceeds the limit
        test_line = " ".join(current_line + [word])
        if len(test_line) <= max_chars:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]

    if current_line:
        lines.append(" ".join(current_line))

    return "\n".join(lines)


def write_textfile(text: str, path: str) -> None:
    """
    Write text to a file for use with ffmpeg drawtext's textfile option.
    This avoids all shell/filter escaping issues.
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Video assembly
# ---------------------------------------------------------------------------

def build_segment_video(
    segment: dict,
    asset: sqlite3.Row | None,
    segment_index: int,
    work_dir: str,
) -> str | None:
    """
    Create a single-segment video clip (image + text overlay, silent).

    Args:
        segment: dict with 'narration', 'duration_seconds', 'visual_cue'
        asset: visual_assets DB row (or None if unavailable)
        segment_index: integer index for temp file naming
        work_dir: temporary working directory

    Returns path to the segment MP4, or None on failure.
    """
    narration = segment.get("narration", "")
    duration = segment.get("duration_seconds", 5)
    # Clamp duration to sane range
    duration = max(2.0, min(30.0, float(duration)))

    # --- Get or create image ---
    image_path = os.path.join(work_dir, f"seg_{segment_index}_img.png")

    if asset and asset["asset_url"] and asset["asset_url"].startswith("http"):
        # Download the actual image
        success = download_image(asset["asset_url"], image_path)
        if not success:
            logger.warning(
                "  Segment %d: image download failed for %s, using placeholder",
                segment_index, asset["asset_url"][:60],
            )
            if not create_placeholder_image(image_path, VIDEO_WIDTH, VIDEO_HEIGHT):
                return None
    else:
        # No asset or no URL — use black placeholder
        logger.info("  Segment %d: no image URL, using black placeholder", segment_index)
        if not create_placeholder_image(image_path, VIDEO_WIDTH, VIDEO_HEIGHT):
            return None

    # --- Build drawtext filter ---
    # Wrap the narration text for readability
    wrapped = wrap_text(narration)
    # Use textfile to avoid escaping issues with special characters
    textfile_path = os.path.join(work_dir, f"seg_{segment_index}_text.txt")
    write_textfile(wrapped, textfile_path)

    # drawtext: white text with semi-transparent black box at bottom of frame
    drawtext_filter = (
        f"drawtext="
        f"textfile='{textfile_path}':"
        f"fontcolor={FONT_COLOR}:"
        f"fontsize={FONT_SIZE}:"
        f"box=1:"
        f"boxcolor={BOX_COLOR}:"
        f"boxborderw={BOX_BORDER}:"
        f"x=(w-text_w)/2:"          # center horizontally
        f"y=h-text_h-60:"            # near bottom
        f"line_spacing=8"
    )

    # --- Render segment ---
    output_path = os.path.join(work_dir, f"seg_{segment_index}.mp4")

    # Build ffmpeg command: loop image for duration, apply drawtext, output H.264
    cmd = [
        "ffmpeg",
        "-y",
        "-loop", "1",
        "-i", image_path,
        "-t", str(duration),
        "-vf", drawtext_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an",  # no audio in segment — will be added later
        output_path,
    ]

    logger.debug("  Segment %d ffmpeg: %s", segment_index, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
        if result.returncode != 0:
            logger.error(
                "  Segment %d: ffmpeg failed: %s",
                segment_index, result.stderr.strip()[-300:],
            )
            return None

        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            logger.error("  Segment %d: output file empty or missing", segment_index)
            return None

        logger.info(
            "  Segment %d: rendered %.1fs video (%d bytes)",
            segment_index, duration, os.path.getsize(output_path),
        )
        return output_path

    except subprocess.TimeoutExpired:
        logger.error("  Segment %d: ffmpeg timed out", segment_index)
        return None
    except OSError as exc:
        logger.error("  Segment %d: ffmpeg OS error: %s", segment_index, exc)
        return None


def concat_segments(segment_paths: list[str], output_path: str) -> bool:
    """
    Concatenate multiple video files into one using the ffmpeg concat demuxer.
    """
    if not segment_paths:
        logger.error("concat_segments: no segments to concatenate")
        return False

    if len(segment_paths) == 1:
        # Single segment — just copy it
        try:
            result = subprocess.run(
                ["cp", segment_paths[0], output_path],
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            return result.returncode == 0
        except OSError:
            return False

    # Create concat file list
    concat_dir = os.path.dirname(output_path)
    concat_list_path = os.path.join(concat_dir, "concat_list.txt")
    with open(concat_list_path, "w") as f:
        for sp in segment_paths:
            # Use relative paths or absolute — ffmpeg concat demuxer needs
            # either absolute paths or paths relative to the concat file
            f.write(f"file '{sp}'\n")

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_path,
    ]

    logger.debug("Concat ffmpeg: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120.0,
        )
        if result.returncode != 0:
            logger.error("Concat ffmpeg failed: %s", result.stderr.strip()[-300:])
            return False
        return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    except subprocess.TimeoutExpired:
        logger.error("Concat ffmpeg timed out")
        return False
    except OSError as exc:
        logger.error("Concat ffmpeg OS error: %s", exc)
        return False


def add_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    """
    Mux the narration audio track into a silent video.
    The audio determines the final duration (shortest=0 means video fills audio).
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        output_path,
    ]

    logger.debug("Add audio ffmpeg: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120.0,
        )
        if result.returncode != 0:
            logger.error("Add audio ffmpeg failed: %s", result.stderr.strip()[-300:])
            return False
        return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    except subprocess.TimeoutExpired:
        logger.error("Add audio ffmpeg timed out")
        return False
    except OSError as exc:
        logger.error("Add audio ffmpeg OS error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Per-topic assembly
# ---------------------------------------------------------------------------

def assemble_topic(
    conn: sqlite3.Connection,
    topic_row: sqlite3.Row,
) -> bool:
    """
    Assemble a full video for one topic.

    Steps:
      1. Verify audio file exists
      2. Parse segments_json
      3. Load visual assets
      4. Build each segment as a temp video clip (image + text)
      5. Concatenate segments
      6. Mux in the narration audio
      7. Write final MP4 to /data/videos/{topic_id}.mp4
      8. Insert videos table row

    Returns True on success.
    """
    topic_id = topic_row["topic_id"]
    title = topic_row["historical_event_title"] or f"topic {topic_id}"
    logger.info("=== Assembling video for topic #%d: %s ===", topic_id, title[:80])

    # --- Idempotency ---
    if topic_has_video(conn, topic_id):
        logger.info("Topic #%d already has a video — skipping (idempotent)", topic_id)
        return True

    # --- Audio check ---
    audio_path = audio_file_path(topic_id)
    if not os.path.isfile(audio_path):
        logger.warning(
            "Topic #%d: audio file not found at %s — skipping",
            topic_id, audio_path,
        )
        return False

    audio_duration = get_audio_duration(audio_path)
    if audio_duration <= 0:
        logger.warning(
            "Topic #%d: could not determine audio duration — skipping", topic_id,
        )
        return False
    logger.info("Topic #%d: audio duration = %.1fs", topic_id, audio_duration)

    # --- Parse segments ---
    segments_json = topic_row["segments_json"]
    try:
        parsed = json.loads(segments_json)
        segments = parsed.get("segments", [])
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Topic #%d: failed to parse segments_json: %s", topic_id, exc)
        return False

    if not segments:
        logger.error("Topic #%d: empty segments list", topic_id)
        return False

    logger.info("Topic #%d: %d segment(s) to render", topic_id, len(segments))

    # --- Load visual assets ---
    assets = get_visual_assets(conn, topic_id)
    has_assets = len(assets) > 0
    if not has_assets:
        logger.warning(
            "Topic #%d: no visual assets found — using placeholders for all segments",
            topic_id,
        )
    else:
        logger.info("Topic #%d: %d visual asset(s) loaded", topic_id, len(assets))

    # Map assets by segment index for quick lookup
    asset_by_segment: dict[int, sqlite3.Row] = {}
    for a in assets:
        asset_by_segment[a["script_segment_index"]] = a

    # --- Build segments ---
    os.makedirs(VIDEO_DIR, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="assembly_") as work_dir:
        segment_paths = []

        for i, segment in enumerate(segments):
            seg_order = segment.get("order", i + 1)
            asset = asset_by_segment.get(seg_order)

            seg_path = build_segment_video(
                segment, asset, seg_order, work_dir,
            )
            if seg_path:
                segment_paths.append(seg_path)
            else:
                logger.error(
                    "Topic #%d: failed to render segment %d — aborting assembly",
                    topic_id, seg_order,
                )
                return False

        if not segment_paths:
            logger.error("Topic #%d: no segments rendered", topic_id)
            return False

        # --- Concatenate ---
        concat_path = os.path.join(work_dir, "concat_silent.mp4")
        if not concat_segments(segment_paths, concat_path):
            logger.error("Topic #%d: segment concatenation failed", topic_id)
            return False
        logger.info("Topic #%d: concatenated %d segments", topic_id, len(segment_paths))

        # --- Add audio ---
        output_path = os.path.join(VIDEO_DIR, f"{topic_id}.mp4")
        if not add_audio(concat_path, audio_path, output_path):
            logger.error("Topic #%d: audio muxing failed", topic_id)
            return False

        output_size = os.path.getsize(output_path)
        logger.info(
            "Topic #%d: final video rendered — %s (%.1f MB)",
            topic_id, output_path, output_size / (1024 * 1024),
        )

    # --- Get final duration from the rendered video ---
    final_duration = get_audio_duration(output_path)  # ffprobe works on video too
    if final_duration <= 0:
        final_duration = audio_duration  # fallback to audio duration

    # --- Insert videos table row ---
    video_id = insert_video(conn, topic_id, output_path, final_duration)
    logger.info("Topic #%d: inserted video row id=%d (status=pending)", topic_id, video_id)

    return True


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run():
    """Main entry point: find ready topics, assemble videos, store results."""
    conn = connect_db()
    cursor = conn.cursor()

    # --- Find ready topics ---
    topics = get_ready_topics(conn)
    logger.info("Found %d topic(s) ready for assembly", len(topics))

    if not topics:
        logger.info("No topics ready for assembly — exiting cleanly.")
        conn.close()
        return

    # --- Process each topic ---
    successes = 0
    failures = 0
    skipped = 0

    for topic in topics:
        try:
            result = assemble_topic(conn, topic)
            if result:
                successes += 1
            else:
                skipped += 1  # missing audio/assets already logged with reason
        except Exception as exc:
            failures += 1
            logger.error(
                "Unexpected error assembling topic #%d: %s",
                topic["topic_id"], exc, exc_info=True,
            )
            # Continue with next topic — don't let one failure block the batch

    conn.close()

    # --- Summary ---
    summary = {
        "topics_found": len(topics),
        "videos_rendered": successes,
        "skipped": skipped,
        "failures": failures,
    }
    logger.info("Assembly run complete: %s", json.dumps(summary))

    if successes == 0 and len(topics) > 0:
        logger.error("All %d topics failed assembly — check input data.", len(topics))
        sys.exit(1)


def main():
    try:
        logger.info("Starting assembly run ...")
        run()
        logger.info("assembly complete.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
